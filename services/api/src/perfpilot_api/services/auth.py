from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import AuthSession, Membership, Team, User
from perfpilot_api.security.csrf import (
    digest_csrf_token,
    generate_csrf_token,
    verify_csrf_token,
)
from perfpilot_api.security.passwords import normalize_username, verify_password
from perfpilot_api.security.sessions import digest_session_token, generate_session_token

SessionKind = Literal["pre_auth", "authenticated"]
_SESSION_CLEANUP_BATCH_SIZE = 100
_REVOKED_SESSION_RETENTION = timedelta(days=1)
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$F8Jnt7Ui9E14UxSBp3PMtg$"
    "iADaTYDtD8GD6uU5iapAOQ59D9mGpV0YT/7ltBu6UpA"
)


class InvalidSessionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("session is invalid")


class InvalidCredentialsError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("invalid login credentials")


class InvalidCsrfError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("csrf token is invalid")


class TeamAccessNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("team access was not found")


class RoleForbiddenError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("team role does not permit this action")


class TargetUserNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("active target user was not found")


class MemberNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("team member was not found")


class LastOwnerError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the final team owner cannot be removed or demoted")


class TokenSource(Protocol):
    def new_session_token(self) -> str: ...

    def new_csrf_token(self) -> str: ...


class SecureTokenSource:
    def new_session_token(self) -> str:
        return generate_session_token()

    def new_csrf_token(self) -> str:
        return generate_csrf_token()


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    kind: SessionKind
    user_id: UUID | None
    max_age: int


@dataclass(frozen=True)
class SessionContext:
    session_id: UUID
    kind: SessionKind
    user_id: UUID | None


@dataclass(frozen=True)
class MeMembership:
    id: UUID
    team_id: UUID
    team_name: str
    role: str


@dataclass(frozen=True)
class MeResult:
    user_id: UUID
    username: str
    is_platform_admin: bool
    memberships: tuple[MeMembership, ...]


@dataclass(frozen=True)
class TeamMemberResult:
    id: UUID
    user_id: UUID
    username: str
    role: str


@dataclass(frozen=True)
class TeamRequestContext:
    user_id: UUID
    team_id: UUID
    role: str


class LoginRateLimitedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("login rate limit exceeded")


class PreAuthSessionRateLimitedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("pre-auth session issuance rate limit exceeded")


@dataclass(frozen=True)
class LoginAttemptReservation:
    normalized_username: str
    client_address: str
    token: str


class LoginRateLimiter(Protocol):
    async def reserve_attempt(
        self,
        normalized_username: str,
        client_address: str,
    ) -> LoginAttemptReservation: ...

    async def finish_failure(
        self,
        reservation: LoginAttemptReservation,
    ) -> bool: ...

    async def finish_success(
        self,
        reservation: LoginAttemptReservation,
    ) -> None: ...

    async def release_attempt(
        self,
        reservation: LoginAttemptReservation,
    ) -> None: ...

    async def precheck(self, normalized_username: str, client_address: str) -> None: ...

    async def record_failure(
        self,
        normalized_username: str,
        client_address: str,
    ) -> bool: ...

    async def clear_username(self, normalized_username: str) -> None: ...


class RedisRateLimitClient(Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object: ...

    async def delete(self, key: str) -> object: ...


class PreAuthSessionLimiter(Protocol):
    async def check_and_record(self, client_address: str) -> None: ...


class InMemoryPreAuthSessionLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        window_seconds: int = 600,
        issuance_limit: int = 20,
    ) -> None:
        if window_seconds <= 0 or issuance_limit <= 0:
            raise ValueError("pre-auth issuance limit settings must be positive")
        self._clock = clock
        self._window_seconds = window_seconds
        self._issuance_limit = issuance_limit
        self._issuances: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check_and_record(self, client_address: str) -> None:
        async with self._lock:
            now = self._clock()
            cutoff = now - self._window_seconds
            entries = self._issuances.setdefault(client_address, deque())
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self._issuance_limit:
                raise PreAuthSessionRateLimitedError
            entries.append(now)


class InMemoryLoginRateLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        window_seconds: int = 900,
        failure_limit: int = 5,
        nonce_source: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if window_seconds <= 0 or failure_limit <= 0:
            raise ValueError("login rate limit settings must be positive")
        self._clock = clock
        self._window_seconds = window_seconds
        self._failure_limit = failure_limit
        self._nonce_source = nonce_source
        self._username_failures: dict[str, deque[float]] = {}
        self._address_failures: dict[str, deque[float]] = {}
        self._reservations: dict[str, tuple[str, str, float]] = {}
        self._reservation_sequence = 0
        self._lock = asyncio.Lock()

    def _active_failures(
        self,
        failures: dict[str, deque[float]],
        key: str,
        now: float,
    ) -> deque[float]:
        entries = failures.setdefault(key, deque())
        cutoff = now - self._window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        return entries

    def _prune_reservations(self, now: float) -> None:
        cutoff = now - self._window_seconds
        expired_tokens = [
            token
            for token, (_, _, created_at) in self._reservations.items()
            if created_at <= cutoff
        ]
        for token in expired_tokens:
            self._reservations.pop(token, None)

    def _reservation_count(self, *, username: str | None = None, address: str | None = None) -> int:
        return sum(
            1
            for reserved_username, reserved_address, _ in self._reservations.values()
            if (username is None or reserved_username == username)
            and (address is None or reserved_address == address)
        )

    def _pop_reservation(
        self,
        reservation: LoginAttemptReservation,
    ) -> None:
        stored = self._reservations.pop(reservation.token, None)
        if stored is None or stored[:2] != (
            reservation.normalized_username,
            reservation.client_address,
        ):
            raise RuntimeError("login attempt reservation is invalid")

    async def reserve_attempt(
        self,
        normalized_username: str,
        client_address: str,
    ) -> LoginAttemptReservation:
        async with self._lock:
            now = self._clock()
            self._prune_reservations(now)
            username_entries = self._active_failures(
                self._username_failures,
                normalized_username,
                now,
            )
            address_entries = self._active_failures(
                self._address_failures,
                client_address,
                now,
            )
            if (
                len(username_entries)
                + self._reservation_count(username=normalized_username)
                >= self._failure_limit
                or len(address_entries)
                + self._reservation_count(address=client_address)
                >= self._failure_limit
            ):
                raise LoginRateLimitedError
            self._reservation_sequence += 1
            token = f"{self._nonce_source()}:{self._reservation_sequence}"
            self._reservations[token] = (
                normalized_username,
                client_address,
                now,
            )
            return LoginAttemptReservation(
                normalized_username=normalized_username,
                client_address=client_address,
                token=token,
            )

    async def finish_failure(
        self,
        reservation: LoginAttemptReservation,
    ) -> bool:
        async with self._lock:
            now = self._clock()
            self._prune_reservations(now)
            self._pop_reservation(reservation)
            username_entries = self._active_failures(
                self._username_failures,
                reservation.normalized_username,
                now,
            )
            address_entries = self._active_failures(
                self._address_failures,
                reservation.client_address,
                now,
            )
            username_entries.append(now)
            address_entries.append(now)
            return (
                len(username_entries) >= self._failure_limit
                or len(address_entries) >= self._failure_limit
            )

    async def finish_success(
        self,
        reservation: LoginAttemptReservation,
    ) -> None:
        async with self._lock:
            self._prune_reservations(self._clock())
            self._pop_reservation(reservation)
            self._username_failures.pop(reservation.normalized_username, None)

    async def release_attempt(
        self,
        reservation: LoginAttemptReservation,
    ) -> None:
        async with self._lock:
            self._prune_reservations(self._clock())
            self._pop_reservation(reservation)

    async def precheck(self, normalized_username: str, client_address: str) -> None:
        async with self._lock:
            now = self._clock()
            self._prune_reservations(now)
            username_entries = self._active_failures(
                self._username_failures,
                normalized_username,
                now,
            )
            address_entries = self._active_failures(
                self._address_failures,
                client_address,
                now,
            )
            if (
                len(username_entries) >= self._failure_limit
                or len(address_entries) >= self._failure_limit
                or len(username_entries)
                + self._reservation_count(username=normalized_username)
                >= self._failure_limit
                or len(address_entries)
                + self._reservation_count(address=client_address)
                >= self._failure_limit
            ):
                raise LoginRateLimitedError

    async def record_failure(
        self,
        normalized_username: str,
        client_address: str,
    ) -> bool:
        async with self._lock:
            now = self._clock()
            username_entries = self._active_failures(
                self._username_failures,
                normalized_username,
                now,
            )
            address_entries = self._active_failures(
                self._address_failures,
                client_address,
                now,
            )
            username_entries.append(now)
            address_entries.append(now)
            return (
                len(username_entries) >= self._failure_limit
                or len(address_entries) >= self._failure_limit
            )

    async def clear_username(self, normalized_username: str) -> None:
        async with self._lock:
            self._username_failures.pop(normalized_username, None)


_RATE_LIMIT_PRECHECK_SCRIPT = """
local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
for index = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', cutoff)
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3])
   or redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
  return 1
end
return 0
"""

_RATE_LIMIT_RECORD_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cutoff = now - window
for index = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', cutoff)
  redis.call('ZADD', KEYS[index], now, ARGV[4] .. ':' .. index)
  redis.call('PEXPIRE', KEYS[index], window)
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3])
   or redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
  return 1
end
return 0
"""

_RATE_LIMIT_RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cutoff = now - window
for index = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', cutoff)
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3])
   or redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
  return 0
end
for index = 1, 2 do
  redis.call('ZADD', KEYS[index], now, 'p:' .. ARGV[4] .. ':' .. index)
  redis.call('PEXPIRE', KEYS[index], window)
end
return 1
"""

_RATE_LIMIT_FINISH_FAILURE_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cutoff = now - window
for index = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', cutoff)
  redis.call('ZREM', KEYS[index], 'p:' .. ARGV[4] .. ':' .. index)
  redis.call('ZADD', KEYS[index], now, 'f:' .. ARGV[4] .. ':' .. index)
  redis.call('PEXPIRE', KEYS[index], window)
end
local function failure_count(key)
  local count = 0
  local members = redis.call('ZRANGE', key, 0, -1)
  for _, member in ipairs(members) do
    if string.sub(member, 1, 2) == 'f:' then
      count = count + 1
    end
  end
  return count
end
if failure_count(KEYS[1]) >= tonumber(ARGV[3])
   or failure_count(KEYS[2]) >= tonumber(ARGV[3]) then
  return 1
end
return 0
"""

_RATE_LIMIT_FINISH_SUCCESS_SCRIPT = """
local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
for index = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', cutoff)
  redis.call('ZREM', KEYS[index], 'p:' .. ARGV[3] .. ':' .. index)
end
local username_members = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, member in ipairs(username_members) do
  if string.sub(member, 1, 2) == 'f:' then
    redis.call('ZREM', KEYS[1], member)
  end
end
for index = 1, 2 do
  if redis.call('ZCARD', KEYS[index]) == 0 then
    redis.call('DEL', KEYS[index])
  else
    redis.call('PEXPIRE', KEYS[index], tonumber(ARGV[2]))
  end
end
return 1
"""

_RATE_LIMIT_RELEASE_SCRIPT = """
local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
for index = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], '-inf', cutoff)
  redis.call('ZREM', KEYS[index], 'p:' .. ARGV[3] .. ':' .. index)
  if redis.call('ZCARD', KEYS[index]) == 0 then
    redis.call('DEL', KEYS[index])
  end
end
return 1
"""


class RedisLoginRateLimiter:
    def __init__(
        self,
        client: RedisRateLimitClient,
        *,
        key_secret: bytes,
        clock: Callable[[], float] = time.time,
        nonce_source: Callable[[], str],
        key_prefix: str = "perfpilot:login:",
        window_seconds: int = 900,
        failure_limit: int = 5,
    ) -> None:
        if not key_secret or window_seconds <= 0 or failure_limit <= 0:
            raise ValueError("login rate limit settings are invalid")
        self._client = client
        self._key_secret = key_secret
        self._clock = clock
        self._nonce_source = nonce_source
        self._key_prefix = key_prefix
        self._window_milliseconds = window_seconds * 1000
        self._failure_limit = failure_limit

    def _key(self, kind: str, value: str) -> str:
        digest = hmac.new(
            self._key_secret,
            f"{kind}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{self._key_prefix}{kind}:{digest}"

    def _keys(self, normalized_username: str, client_address: str) -> tuple[str, str]:
        return (
            self._key("username", normalized_username),
            self._key("address", client_address),
        )

    async def reserve_attempt(
        self,
        normalized_username: str,
        client_address: str,
    ) -> LoginAttemptReservation:
        username_key, address_key = self._keys(
            normalized_username,
            client_address,
        )
        token = self._nonce_source()
        reserved = await self._client.eval(
            _RATE_LIMIT_RESERVE_SCRIPT,
            2,
            username_key,
            address_key,
            int(self._clock() * 1000),
            self._window_milliseconds,
            self._failure_limit,
            token,
        )
        if not bool(reserved):
            raise LoginRateLimitedError
        return LoginAttemptReservation(
            normalized_username=normalized_username,
            client_address=client_address,
            token=token,
        )

    async def finish_failure(
        self,
        reservation: LoginAttemptReservation,
    ) -> bool:
        username_key, address_key = self._keys(
            reservation.normalized_username,
            reservation.client_address,
        )
        limited = await self._client.eval(
            _RATE_LIMIT_FINISH_FAILURE_SCRIPT,
            2,
            username_key,
            address_key,
            int(self._clock() * 1000),
            self._window_milliseconds,
            self._failure_limit,
            reservation.token,
        )
        return bool(limited)

    async def finish_success(
        self,
        reservation: LoginAttemptReservation,
    ) -> None:
        username_key, address_key = self._keys(
            reservation.normalized_username,
            reservation.client_address,
        )
        await self._client.eval(
            _RATE_LIMIT_FINISH_SUCCESS_SCRIPT,
            2,
            username_key,
            address_key,
            int(self._clock() * 1000),
            self._window_milliseconds,
            reservation.token,
        )

    async def release_attempt(
        self,
        reservation: LoginAttemptReservation,
    ) -> None:
        username_key, address_key = self._keys(
            reservation.normalized_username,
            reservation.client_address,
        )
        await self._client.eval(
            _RATE_LIMIT_RELEASE_SCRIPT,
            2,
            username_key,
            address_key,
            int(self._clock() * 1000),
            self._window_milliseconds,
            reservation.token,
        )

    async def precheck(self, normalized_username: str, client_address: str) -> None:
        username_key, address_key = self._keys(normalized_username, client_address)
        limited = await self._client.eval(
            _RATE_LIMIT_PRECHECK_SCRIPT,
            2,
            username_key,
            address_key,
            int(self._clock() * 1000),
            self._window_milliseconds,
            self._failure_limit,
        )
        if bool(limited):
            raise LoginRateLimitedError

    async def record_failure(
        self,
        normalized_username: str,
        client_address: str,
    ) -> bool:
        username_key, address_key = self._keys(normalized_username, client_address)
        limited = await self._client.eval(
            _RATE_LIMIT_RECORD_SCRIPT,
            2,
            username_key,
            address_key,
            int(self._clock() * 1000),
            self._window_milliseconds,
            self._failure_limit,
            self._nonce_source(),
        )
        return bool(limited)

    async def clear_username(self, normalized_username: str) -> None:
        await self._client.delete(self._key("username", normalized_username))


_PRE_AUTH_ISSUANCE_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], window)
return 1
"""


class RedisPreAuthSessionLimiter:
    def __init__(
        self,
        client: RedisRateLimitClient,
        *,
        key_secret: bytes,
        nonce_source: Callable[[], str],
        clock: Callable[[], float] = time.time,
        key_prefix: str = "perfpilot:preauth:",
        window_seconds: int = 600,
        issuance_limit: int = 20,
    ) -> None:
        if not key_secret or window_seconds <= 0 or issuance_limit <= 0:
            raise ValueError("pre-auth issuance limit settings are invalid")
        self._client = client
        self._key_secret = key_secret
        self._nonce_source = nonce_source
        self._clock = clock
        self._key_prefix = key_prefix
        self._window_milliseconds = window_seconds * 1000
        self._issuance_limit = issuance_limit

    def _key(self, client_address: str) -> str:
        digest = hmac.new(
            self._key_secret,
            f"address\0{client_address}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{self._key_prefix}address:{digest}"

    async def check_and_record(self, client_address: str) -> None:
        accepted = await self._client.eval(
            _PRE_AUTH_ISSUANCE_SCRIPT,
            1,
            self._key(client_address),
            int(self._clock() * 1000),
            self._window_milliseconds,
            self._issuance_limit,
            self._nonce_source(),
        )
        if not bool(accepted):
            raise PreAuthSessionRateLimitedError


class AuthService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        rate_limiter: LoginRateLimiter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_source: TokenSource | None = None,
        password_verifier: Callable[[str, str], bool] = verify_password,
        password_verify_concurrency: int = 4,
        pre_auth_session_limiter: PreAuthSessionLimiter | None = None,
    ) -> None:
        if password_verify_concurrency <= 0:
            raise ValueError("password verification concurrency must be positive")
        self._session_factory = session_factory
        self._rate_limiter = rate_limiter
        self._clock = clock
        self._token_source = token_source or SecureTokenSource()
        self._password_verifier = password_verifier
        self._password_verify_slots = asyncio.Semaphore(
            password_verify_concurrency
        )
        self._pre_auth_session_limiter = (
            pre_auth_session_limiter or InMemoryPreAuthSessionLimiter()
        )

    async def create_pre_auth_session(self) -> IssuedSession:
        now = self._clock()
        token = self._token_source.new_session_token()
        csrf_token = self._token_source.new_csrf_token()
        expires_at = now + timedelta(minutes=10)
        stored = AuthSession(
            user_id=None,
            token_digest=digest_session_token(token),
            kind="pre_auth",
            csrf_secret_hash=digest_csrf_token(csrf_token),
            last_seen_at=now,
            absolute_expires_at=expires_at,
            revoked_at=None,
        )
        async with self._session_factory() as session, session.begin():
            stale_session_ids = (
                select(AuthSession.id)
                .where(
                    or_(
                        AuthSession.absolute_expires_at <= now,
                        AuthSession.revoked_at
                        <= now - _REVOKED_SESSION_RETENTION,
                    )
                )
                .order_by(AuthSession.absolute_expires_at, AuthSession.id)
                .limit(_SESSION_CLEANUP_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            await session.execute(
                delete(AuthSession).where(
                    AuthSession.id.in_(stale_session_ids)
                )
            )
            session.add(stored)
        return IssuedSession(
            token=token,
            csrf_token=csrf_token,
            kind="pre_auth",
            user_id=None,
            max_age=600,
        )

    async def get_or_create_csrf(
        self,
        session_token: str | None,
        *,
        client_address: str,
    ) -> IssuedSession:
        if session_token is None:
            await self._pre_auth_session_limiter.check_and_record(
                client_address
            )
            return await self.create_pre_auth_session()

        now = self._clock()
        issued: IssuedSession | None = None
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            valid = (
                stored is not None
                and stored.kind in {"pre_auth", "authenticated"}
                and stored.revoked_at is None
                and stored.absolute_expires_at > now
                and not (stored.kind == "pre_auth" and stored.user_id is not None)
                and not (stored.kind == "authenticated" and stored.user_id is None)
            )
            if valid and stored is not None and stored.kind == "authenticated":
                user = await session.get(User, stored.user_id)
                valid = (
                    user is not None
                    and user.state == "active"
                    and stored.last_seen_at + timedelta(hours=12) >= now
                )
            if valid and stored is not None:
                csrf_token = self._token_source.new_csrf_token()
                stored.csrf_secret_hash = digest_csrf_token(csrf_token)
                stored.last_seen_at = now
                issued = IssuedSession(
                    token=session_token,
                    csrf_token=csrf_token,
                    kind=stored.kind,
                    user_id=stored.user_id,
                    max_age=int((stored.absolute_expires_at - now).total_seconds()),
                )

        if issued is not None:
            return issued
        await self._pre_auth_session_limiter.check_and_record(client_address)
        return await self.create_pre_auth_session()

    async def authenticate_session(
        self,
        token: str,
        *,
        required_kind: SessionKind,
    ) -> SessionContext:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(AuthSession.token_digest == digest_session_token(token))
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != required_kind
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or (stored.kind == "pre_auth" and stored.user_id is not None)
                or (stored.kind == "authenticated" and stored.user_id is None)
            ):
                raise InvalidSessionError
            if stored.kind == "authenticated":
                user = await session.get(User, stored.user_id)
                if (
                    user is None
                    or user.state != "active"
                    or stored.last_seen_at + timedelta(hours=12) < now
                ):
                    raise InvalidSessionError
            stored.last_seen_at = now
            return SessionContext(
                session_id=stored.id,
                kind=required_kind,
                user_id=stored.user_id,
            )

    async def login(
        self,
        *,
        pre_auth_token: str,
        csrf_token: str,
        username: str,
        password: str,
        client_address: str,
    ) -> IssuedSession:
        normalized_username = normalize_username(username)
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            pre_auth = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(pre_auth_token)
                )
            )
            if (
                pre_auth is None
                or pre_auth.kind != "pre_auth"
                or pre_auth.user_id is not None
                or pre_auth.revoked_at is not None
                or pre_auth.absolute_expires_at <= now
            ):
                raise InvalidSessionError
            if not verify_csrf_token(csrf_token, pre_auth.csrf_secret_hash):
                raise InvalidCsrfError

            user = await session.scalar(
                select(User).where(User.username == normalized_username)
            )
            password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
            user_id = user.id if user is not None else None
            user_was_active = user is not None and user.state == "active"

        reservation = await self._rate_limiter.reserve_attempt(
            normalized_username,
            client_address,
        )
        reservation_open = True
        try:
            async with self._password_verify_slots:
                password_matches = await asyncio.to_thread(
                    self._password_verifier,
                    password_hash,
                    password,
                )
            if user_id is None or not user_was_active or not password_matches:
                limited = await self._rate_limiter.finish_failure(reservation)
                reservation_open = False
                if limited:
                    raise LoginRateLimitedError
                raise InvalidCredentialsError

            commit_now = self._clock()
            issued: IssuedSession | None = None
            credentials_changed = False
            async with self._session_factory() as session, session.begin():
                pre_auth = await session.scalar(
                    select(AuthSession)
                    .where(
                        AuthSession.token_digest
                        == digest_session_token(pre_auth_token)
                    )
                    .with_for_update()
                )
                if (
                    pre_auth is None
                    or pre_auth.kind != "pre_auth"
                    or pre_auth.user_id is not None
                    or pre_auth.revoked_at is not None
                    or pre_auth.absolute_expires_at <= commit_now
                ):
                    raise InvalidSessionError
                if not verify_csrf_token(
                    csrf_token,
                    pre_auth.csrf_secret_hash,
                ):
                    raise InvalidCsrfError
                current_user = await session.get(User, user_id)
                credentials_changed = (
                    current_user is None
                    or current_user.state != "active"
                    or not hmac.compare_digest(
                        current_user.password_hash,
                        password_hash,
                    )
                )
                if not credentials_changed and current_user is not None:
                    pre_auth.revoked_at = commit_now
                    session_token = self._token_source.new_session_token()
                    authenticated_csrf_token = self._token_source.new_csrf_token()
                    authenticated_session = AuthSession(
                        user_id=current_user.id,
                        token_digest=digest_session_token(session_token),
                        kind="authenticated",
                        csrf_secret_hash=digest_csrf_token(
                            authenticated_csrf_token
                        ),
                        last_seen_at=commit_now,
                        absolute_expires_at=commit_now + timedelta(days=7),
                        revoked_at=None,
                    )
                    session.add(authenticated_session)
                    issued = IssuedSession(
                        token=session_token,
                        csrf_token=authenticated_csrf_token,
                        kind="authenticated",
                        user_id=current_user.id,
                        max_age=7 * 24 * 60 * 60,
                    )

            if credentials_changed:
                limited = await self._rate_limiter.finish_failure(reservation)
                reservation_open = False
                if limited:
                    raise LoginRateLimitedError
                raise InvalidCredentialsError
            if issued is None:
                raise RuntimeError("authenticated session was not issued")
            await self._rate_limiter.finish_success(reservation)
            reservation_open = False
            return issued
        finally:
            if reservation_open:
                try:
                    await asyncio.shield(
                        self._rate_limiter.release_attempt(reservation)
                    )
                except Exception:
                    pass

    async def logout(self, *, session_token: str, csrf_token: str) -> None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            user = await session.get(User, stored.user_id)
            if user is None or user.state != "active":
                raise InvalidSessionError
            if not verify_csrf_token(csrf_token, stored.csrf_secret_hash):
                raise InvalidCsrfError
            stored.csrf_secret_hash = digest_csrf_token(
                self._token_source.new_csrf_token()
            )
            stored.revoked_at = now
            stored.last_seen_at = now

    async def get_me(self, session_token: str) -> MeResult:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            user = await session.get(User, stored.user_id)
            if user is None or user.state != "active":
                raise InvalidSessionError
            rows = (
                await session.execute(
                    select(Membership, Team)
                    .join(Team, Team.id == Membership.team_id)
                    .where(
                        Membership.user_id == user.id,
                        Team.state == "active",
                    )
                    .order_by(Team.id, Membership.id)
                )
            ).all()
            stored.last_seen_at = now
            memberships = tuple(
                MeMembership(
                    id=membership.id,
                    team_id=team.id,
                    team_name=team.name,
                    role=membership.role,
                )
                for membership, team in rows
            )
            return MeResult(
                user_id=user.id,
                username=user.username,
                is_platform_admin=user.is_platform_admin,
                memberships=memberships,
            )

    async def list_team_members(
        self,
        *,
        session_token: str,
        team_id: UUID,
    ) -> tuple[TeamMemberResult, ...]:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            actor = await session.get(User, stored.user_id)
            if actor is None or actor.state != "active":
                raise InvalidSessionError
            actor_membership = await session.scalar(
                select(Membership)
                .join(Team, Team.id == Membership.team_id)
                .where(
                    Membership.team_id == team_id,
                    Membership.user_id == actor.id,
                    Team.state == "active",
                )
            )
            if actor_membership is None:
                raise TeamAccessNotFoundError
            rows = (
                await session.execute(
                    select(Membership, User)
                    .join(User, User.id == Membership.user_id)
                    .where(Membership.team_id == team_id)
                    .order_by(User.username, Membership.id)
                )
            ).all()
            stored.last_seen_at = now
            return tuple(
                TeamMemberResult(
                    id=membership.id,
                    user_id=user.id,
                    username=user.username,
                    role=membership.role,
                )
                for membership, user in rows
            )

    async def authorize_team_request(
        self,
        *,
        session_token: str,
        csrf_token: str,
        team_id: UUID,
        access: Literal["read", "write"],
    ) -> TeamRequestContext:
        if access not in {"read", "write"}:
            raise ValueError("team access mode is invalid")
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            if not verify_csrf_token(csrf_token, stored.csrf_secret_hash):
                raise InvalidCsrfError
            actor = await session.get(User, stored.user_id)
            if actor is None or actor.state != "active":
                raise InvalidSessionError
            membership = await session.scalar(
                select(Membership)
                .join(Team, Team.id == Membership.team_id)
                .where(
                    Membership.team_id == team_id,
                    Membership.user_id == actor.id,
                    Team.state == "active",
                )
            )
            if membership is None:
                raise TeamAccessNotFoundError
            if access == "write" and membership.role not in {
                "team_owner",
                "team_member",
            }:
                raise RoleForbiddenError
            stored.last_seen_at = now
            return TeamRequestContext(
                user_id=actor.id,
                team_id=team_id,
                role=membership.role,
            )

    async def add_team_member(
        self,
        *,
        session_token: str,
        csrf_token: str,
        team_id: UUID,
        user_id: UUID,
        role: str,
    ) -> TeamMemberResult:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            actor = await session.get(User, stored.user_id)
            if actor is None or actor.state != "active":
                raise InvalidSessionError
            if not verify_csrf_token(csrf_token, stored.csrf_secret_hash):
                raise InvalidCsrfError
            actor_membership = await session.scalar(
                select(Membership)
                .join(Team, Team.id == Membership.team_id)
                .where(
                    Membership.team_id == team_id,
                    Membership.user_id == actor.id,
                    Team.state == "active",
                )
                .with_for_update()
            )
            if actor_membership is None:
                raise TeamAccessNotFoundError
            if actor_membership.role != "team_owner":
                raise RoleForbiddenError
            target_user = await session.scalar(
                select(User).where(
                    User.id == user_id,
                    User.state == "active",
                )
            )
            if target_user is None:
                raise TargetUserNotFoundError
            membership = Membership(
                team_id=team_id,
                user_id=target_user.id,
                role=role,
            )
            session.add(membership)
            await session.flush()
            stored.last_seen_at = now
            return TeamMemberResult(
                id=membership.id,
                user_id=target_user.id,
                username=target_user.username,
                role=membership.role,
            )

    async def update_team_member(
        self,
        *,
        session_token: str,
        csrf_token: str,
        team_id: UUID,
        member_id: UUID,
        role: str,
    ) -> TeamMemberResult:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            actor = await session.get(User, stored.user_id)
            if actor is None or actor.state != "active":
                raise InvalidSessionError
            if not verify_csrf_token(csrf_token, stored.csrf_secret_hash):
                raise InvalidCsrfError
            memberships = (
                await session.scalars(
                    select(Membership)
                    .join(Team, Team.id == Membership.team_id)
                    .where(
                        Membership.team_id == team_id,
                        Team.state == "active",
                    )
                    .order_by(Membership.id)
                    .with_for_update()
                )
            ).all()
            actor_membership = next(
                (
                    membership
                    for membership in memberships
                    if membership.user_id == actor.id
                ),
                None,
            )
            if actor_membership is None:
                raise TeamAccessNotFoundError
            if actor_membership.role != "team_owner":
                raise RoleForbiddenError
            target_membership = next(
                (
                    membership
                    for membership in memberships
                    if membership.id == member_id
                ),
                None,
            )
            if target_membership is None:
                raise MemberNotFoundError
            target_user = await session.get(User, target_membership.user_id)
            if target_user is None or target_user.state != "active":
                raise MemberNotFoundError
            if (
                target_membership.role == "team_owner"
                and role != "team_owner"
                and sum(
                    membership.role == "team_owner"
                    for membership in memberships
                )
                == 1
            ):
                raise LastOwnerError
            if target_membership.role != role:
                target_membership.role = role
                await session.execute(
                    update(AuthSession)
                    .where(
                        AuthSession.user_id == target_user.id,
                        AuthSession.kind == "authenticated",
                        AuthSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            stored.last_seen_at = now
            return TeamMemberResult(
                id=target_membership.id,
                user_id=target_user.id,
                username=target_user.username,
                role=target_membership.role,
            )

    async def delete_team_member(
        self,
        *,
        session_token: str,
        csrf_token: str,
        team_id: UUID,
        member_id: UUID,
    ) -> None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_digest
                    == digest_session_token(session_token)
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.kind != "authenticated"
                or stored.user_id is None
                or stored.revoked_at is not None
                or stored.absolute_expires_at <= now
                or stored.last_seen_at + timedelta(hours=12) < now
            ):
                raise InvalidSessionError
            actor = await session.get(User, stored.user_id)
            if actor is None or actor.state != "active":
                raise InvalidSessionError
            if not verify_csrf_token(csrf_token, stored.csrf_secret_hash):
                raise InvalidCsrfError
            memberships = (
                await session.scalars(
                    select(Membership)
                    .join(Team, Team.id == Membership.team_id)
                    .where(
                        Membership.team_id == team_id,
                        Team.state == "active",
                    )
                    .order_by(Membership.id)
                    .with_for_update()
                )
            ).all()
            actor_membership = next(
                (
                    membership
                    for membership in memberships
                    if membership.user_id == actor.id
                ),
                None,
            )
            if actor_membership is None:
                raise TeamAccessNotFoundError
            if actor_membership.role != "team_owner":
                raise RoleForbiddenError
            target_membership = next(
                (
                    membership
                    for membership in memberships
                    if membership.id == member_id
                ),
                None,
            )
            if target_membership is None:
                raise MemberNotFoundError
            target_user = await session.get(User, target_membership.user_id)
            if target_user is None or target_user.state != "active":
                raise MemberNotFoundError
            if (
                target_membership.role == "team_owner"
                and sum(
                    membership.role == "team_owner"
                    for membership in memberships
                )
                == 1
            ):
                raise LastOwnerError
            await session.delete(target_membership)
            await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == target_user.id,
                    AuthSession.kind == "authenticated",
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            stored.last_seen_at = now
