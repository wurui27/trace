"""Private, atomic persistence for local-runtime identities and sessions.

The supplied root is intentionally a dedicated control-state directory.  It is
not shared with local analysis artifacts, which remain owned by
``LocalAnalysisStore``.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from perfpilot_api.security.passwords import (
    hash_password,
    normalize_username,
    verify_password,
)


LOCAL_SESSION_TTL = timedelta(hours=8)
_SCHEMA_VERSION = 1
_CONTROL_FILE_NAME = "control.json"
_LOCK_FILE_NAME = ".control.lock"
_MAX_CONTROL_BYTES = 2 * 1024 * 1024
_DUMMY_PASSWORD_HASH = hash_password("perfpilot-local-control-dummy-password")


class LocalControlStoreError(RuntimeError):
    """A redacted local-control persistence or credential error."""


class LocalControlStoreNotFoundError(LocalControlStoreError):
    """A deliberately redacted principal lookup failure."""


@dataclass(frozen=True, slots=True)
class LocalPrincipal:
    user_id: UUID
    username: str
    team_id: UUID
    team_name: str
    is_platform_admin: bool
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class EnsureUserResult:
    principal: LocalPrincipal
    created: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_token() -> str:
    return secrets.token_urlsafe(32)


def _reject_constant(_value: str) -> object:
    raise ValueError


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class LocalControlStore:
    """Filesystem-backed local users, one private team each, and sessions."""

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        uuid_factory: Callable[[], UUID] = uuid4,
        token_factory: Callable[[], str] = _default_token,
    ) -> None:
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        if state_root.is_symlink():
            raise LocalControlStoreError("unsafe local control path")
        try:
            state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if state_root.is_symlink() or not state_root.is_dir():
                raise LocalControlStoreError("unsafe local control path")
            os.chmod(state_root, 0o700)
        except LocalControlStoreError:
            raise
        except OSError:
            raise LocalControlStoreError("local control persistence failed") from None
        self._root = state_root.absolute()
        self._control_path = self._root / _CONTROL_FILE_NAME
        self._lock_path = self._root / _LOCK_FILE_NAME
        if self._control_path.is_symlink() or self._lock_path.is_symlink():
            raise LocalControlStoreError("unsafe local control path")
        try:
            if self._control_path.exists():
                if not self._control_path.is_file():
                    raise LocalControlStoreError("unsafe local control path")
                os.chmod(self._control_path, 0o600)
        except LocalControlStoreError:
            raise
        except OSError:
            raise LocalControlStoreError("local control persistence failed") from None
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._token_factory = token_factory

    def ensure_user(self, username: str, password: str, admin: bool) -> EnsureUserResult:
        normalized = self._validate_new_credentials(username, password)
        if not isinstance(admin, bool):
            raise LocalControlStoreError("invalid local credentials")
        result: EnsureUserResult | None = None
        with self._exclusive_lock():
            document = self._read_document()
            existing = self._find_user(document, normalized)
            if existing is not None:
                result = EnsureUserResult(self._principal_from_user(document, existing), False)
            else:
                user_id = str(self._new_uuid())
                team_id = str(self._new_uuid())
                team = {
                    "team_id": team_id,
                    "name": f"{normalized} local team",
                }
                user = {
                    "user_id": user_id,
                    "username": normalized,
                    "team_id": team["team_id"],
                    "password_hash": self._password_hash(password),
                    "is_platform_admin": admin,
                    "must_change_password": True,
                }
                document["teams"].append(team)
                document["users"].append(user)
                self._write_document(document)
                result = EnsureUserResult(self._principal_from_user(document, user), True)
        assert result is not None
        return result

    def authenticate(self, username: str, password: str) -> LocalPrincipal | None:
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        normalized = normalize_username(username)
        if not normalized or len(normalized) > 128:
            verify_password(_DUMMY_PASSWORD_HASH, password)
            return None
        with self._exclusive_lock():
            document = self._read_document()
            user = self._find_user(document, normalized)
            password_hash = str(user["password_hash"]) if user is not None else _DUMMY_PASSWORD_HASH
            verified = verify_password(password_hash, password)
            if user is None or not verified:
                return None
            return self._principal_from_user(document, user)

    def issue_session(self, user_id: UUID) -> tuple[str, str]:
        if not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        with self._exclusive_lock():
            document = self._read_document()
            if self._find_user_by_id(document, user_id) is None:
                raise LocalControlStoreNotFoundError("local principal not found")
            token = self._new_token()
            csrf_token = self._new_token()
            now = self._now()
            document["sessions"].append(
                {
                    "token_digest": self._digest(token),
                    "csrf_token_digest": self._digest(csrf_token),
                    "user_id": str(user_id),
                    "created_at": now.isoformat(),
                    "expires_at": (now + LOCAL_SESSION_TTL).isoformat(),
                }
            )
            self._write_document(document)
            return token, csrf_token

    def resolve_session(self, token: str) -> LocalPrincipal | None:
        if not isinstance(token, str) or not token:
            return None
        digest = self._digest(token)
        with self._exclusive_lock():
            document = self._read_document()
            now = self._now()
            for session in document["sessions"]:
                if not hmac.compare_digest(str(session["token_digest"]), digest):
                    continue
                if self._parse_timestamp(session["expires_at"]) <= now:
                    return None
                return self._principal_from_user_id(document, UUID(str(session["user_id"])))
        return None

    def change_password(
        self,
        user_id: UUID,
        current_password: str,
        new_password: str,
    ) -> LocalPrincipal:
        if not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        if not isinstance(current_password, str):
            raise LocalControlStoreError("invalid local credentials")
        with self._exclusive_lock():
            document = self._read_document()
            user = self._find_user_by_id(document, user_id)
            if user is None:
                raise LocalControlStoreNotFoundError("local principal not found")
            if not verify_password(str(user["password_hash"]), current_password):
                raise LocalControlStoreError("invalid local credentials")
            self._validate_password(new_password, username=str(user["username"]))
            user["password_hash"] = self._password_hash(new_password)
            user["must_change_password"] = False
            document["sessions"] = [
                session for session in document["sessions"] if session["user_id"] != str(user_id)
            ]
            self._write_document(document)
            return self._principal_from_user(document, user)

    def require_team(self, token: str, team_id: UUID) -> LocalPrincipal:
        if not isinstance(team_id, UUID):
            raise TypeError("team_id must be a UUID")
        principal = self.resolve_session(token)
        if principal is None or principal.team_id != team_id:
            raise LocalControlStoreNotFoundError("local principal not found")
        return principal

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        if self._lock_path.is_symlink() or self._control_path.is_symlink():
            raise LocalControlStoreError("unsafe local control path")
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.chmod(self._lock_path, 0o600)
            with os.fdopen(descriptor, "r+b", closefd=True) as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    if self._lock_path.is_symlink() or self._control_path.is_symlink():
                        raise LocalControlStoreError("unsafe local control path")
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except LocalControlStoreError:
            raise
        except OSError:
            raise LocalControlStoreError("local control persistence failed") from None

    def _read_document(self) -> dict[str, Any]:
        if self._control_path.is_symlink():
            raise LocalControlStoreError("unsafe local control path")
        if not self._control_path.exists():
            return {
                "schema_version": _SCHEMA_VERSION,
                "users": [],
                "teams": [],
                "sessions": [],
            }
        try:
            if not self._control_path.is_file():
                raise ValueError
            size = self._control_path.stat().st_size
            if not 0 < size <= _MAX_CONTROL_BYTES:
                raise ValueError
            raw = self._control_path.read_bytes()
            document = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
            self._validate_document(document)
            return document
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise LocalControlStoreError("invalid local control state") from None

    def _write_document(self, document: dict[str, Any]) -> None:
        self._validate_document(document)
        if self._control_path.is_symlink():
            raise LocalControlStoreError("unsafe local control path")
        try:
            payload = json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            temporary = self._root / f".{_CONTROL_FILE_NAME}.{uuid4().hex}.tmp"
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._control_path)
            os.chmod(self._control_path, 0o600)
            directory_descriptor = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise LocalControlStoreError("local control persistence failed") from None

    @staticmethod
    def _validate_document(document: object) -> None:
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "users",
            "teams",
            "sessions",
        }:
            raise ValueError
        if type(document["schema_version"]) is not int or document["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        users = document["users"]
        teams = document["teams"]
        sessions = document["sessions"]
        if not isinstance(users, list) or not isinstance(teams, list) or not isinstance(sessions, list):
            raise ValueError
        team_ids: set[str] = set()
        for team in teams:
            if not isinstance(team, dict) or set(team) != {"team_id", "name"}:
                raise ValueError
            team_id = LocalControlStore._validated_uuid(team["team_id"])
            if (
                not isinstance(team["name"], str)
                or not team["name"]
                or str(team_id) in team_ids
            ):
                raise ValueError
            team_ids.add(str(team_id))
        usernames: set[str] = set()
        user_ids: set[str] = set()
        referenced_team_ids: set[str] = set()
        for user in users:
            if not isinstance(user, dict) or set(user) != {
                "user_id",
                "username",
                "team_id",
                "password_hash",
                "is_platform_admin",
                "must_change_password",
            }:
                raise ValueError
            user_id = LocalControlStore._validated_uuid(user["user_id"])
            team_id = LocalControlStore._validated_uuid(user["team_id"])
            username = user["username"]
            if (
                not isinstance(username, str)
                or normalize_username(username) != username
                or not username
                or len(username) > 128
                or not isinstance(user["password_hash"], str)
                or not user["password_hash"]
                or not isinstance(user["is_platform_admin"], bool)
                or not isinstance(user["must_change_password"], bool)
                or username in usernames
                or str(user_id) in user_ids
                or str(team_id) not in team_ids
                or str(team_id) in referenced_team_ids
            ):
                raise ValueError
            usernames.add(username)
            user_ids.add(str(user_id))
            referenced_team_ids.add(str(team_id))
        if referenced_team_ids != team_ids:
            raise ValueError
        for session in sessions:
            if not isinstance(session, dict) or set(session) != {
                "token_digest",
                "csrf_token_digest",
                "user_id",
                "created_at",
                "expires_at",
            }:
                raise ValueError
            if (
                not isinstance(session["token_digest"], str)
                or len(session["token_digest"]) != 64
                or not isinstance(session["csrf_token_digest"], str)
                or len(session["csrf_token_digest"]) != 64
                or str(LocalControlStore._validated_uuid(session["user_id"])) not in user_ids
            ):
                raise ValueError
            created_at = LocalControlStore._parse_timestamp(session["created_at"])
            expires_at = LocalControlStore._parse_timestamp(session["expires_at"])
            if expires_at <= created_at:
                raise ValueError

    @staticmethod
    def _validated_uuid(value: object) -> UUID:
        if not isinstance(value, str):
            raise ValueError
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
        return parsed

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError
        return parsed.astimezone(UTC)

    def _find_user(self, document: dict[str, Any], username: str) -> dict[str, Any] | None:
        return next((user for user in document["users"] if user["username"] == username), None)

    @staticmethod
    def _find_user_by_id(document: dict[str, Any], user_id: UUID) -> dict[str, Any] | None:
        return next(
            (user for user in document["users"] if user["user_id"] == str(user_id)), None
        )

    def _principal_from_user_id(
        self, document: dict[str, Any], user_id: UUID
    ) -> LocalPrincipal | None:
        user = self._find_user_by_id(document, user_id)
        return self._principal_from_user(document, user) if user is not None else None

    @staticmethod
    def _principal_from_user(document: dict[str, Any], user: dict[str, Any]) -> LocalPrincipal:
        team = next(
            (team for team in document["teams"] if team["team_id"] == user["team_id"]), None
        )
        if team is None:
            raise LocalControlStoreError("invalid local control state")
        return LocalPrincipal(
            user_id=UUID(str(user["user_id"])),
            username=str(user["username"]),
            team_id=UUID(str(user["team_id"])),
            team_name=str(team["name"]),
            is_platform_admin=bool(user["is_platform_admin"]),
            must_change_password=bool(user["must_change_password"]),
        )

    def _new_uuid(self) -> UUID:
        candidate = self._uuid_factory()
        if not isinstance(candidate, UUID) or candidate.version != 4:
            raise LocalControlStoreError("local control persistence failed")
        return candidate

    def _new_token(self) -> str:
        token = self._token_factory()
        if not isinstance(token, str) or not token:
            raise LocalControlStoreError("local control persistence failed")
        return token

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise LocalControlStoreError("local control persistence failed")
        return now.astimezone(UTC)

    @staticmethod
    def _validate_new_credentials(username: str, password: str) -> str:
        if not isinstance(username, str):
            raise LocalControlStoreError("invalid local credentials")
        normalized = normalize_username(username)
        if not normalized or len(normalized) > 128:
            raise LocalControlStoreError("invalid local credentials")
        LocalControlStore._validate_password(password, username=normalized)
        return normalized

    @staticmethod
    def _validate_password(password: str, *, username: str | None = None) -> None:
        if (
            not isinstance(password, str)
            or len(password) < 12
            or (username is not None and normalize_username(password) == username)
        ):
            raise LocalControlStoreError("invalid local credentials")

    @staticmethod
    def _password_hash(password: str) -> str:
        try:
            return hash_password(password)
        except Exception:
            raise LocalControlStoreError("invalid local credentials") from None

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "EnsureUserResult",
    "LOCAL_SESSION_TTL",
    "LocalControlStore",
    "LocalControlStoreError",
    "LocalControlStoreNotFoundError",
    "LocalPrincipal",
]
