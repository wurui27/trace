from __future__ import annotations

import asyncio
import hmac
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import (
    Agent as StoredAgent,
    AgentLease,
    Device,
)
from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    AgentNonceStore,
    AgentProofRejected,
    decode_ed25519_public_key,
    verify_refresh_proof,
)
from perfpilot_api.services.source_workspaces import is_public_source_display_name

AgentPlatform = Literal["macos", "windows", "linux"]
AgentState = Literal["pending", "online", "offline", "revoked"]

_REGISTRATION_LIFETIME = timedelta(minutes=10)
_ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
_REFRESH_TOKEN_LIFETIME = timedelta(days=30)
_AGENT_NAME_MAX_LENGTH = 200
_AGENT_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_TASK_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PLATFORMS = frozenset({"macos", "windows", "linux"})


class AgentRegistrationRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Agent registration was rejected")


class AgentAuthenticationRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Agent authentication was rejected")


class AgentNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Agent was not found")


class AgentNameConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Agent name is already in use")


class AgentInvalidRequestError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Agent request is invalid")


@dataclass(frozen=True, slots=True)
class TaskSigningKey:
    kid: str
    public_key_b64: str = dataclass_field(repr=False)

    def __post_init__(self) -> None:
        if _TASK_KEY_ID_PATTERN.fullmatch(self.kid) is None:
            raise ValueError("Task signing key identifier is invalid")
        try:
            decode_ed25519_public_key(self.public_key_b64)
        except AgentProofRejected:
            raise ValueError("Task signing public key is invalid") from None


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    registration_code: str = dataclass_field(repr=False)
    public_key_b64: str = dataclass_field(repr=False)
    platform: AgentPlatform
    agent_version: str
    hostname: str
    os_version: str


@dataclass(frozen=True, slots=True)
class RegistrationCodeIssue:
    agent_id: UUID
    registration_code: str = dataclass_field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedAgentCredentials:
    agent_id: UUID
    team_id: UUID
    access_token: str = dataclass_field(repr=False)
    access_token_expires_at: datetime
    refresh_token: str = dataclass_field(repr=False)
    refresh_token_expires_at: datetime
    task_signing_key: TaskSigningKey = dataclass_field(repr=False)
    heartbeat_interval_seconds: int = 10


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    agent_id: UUID
    team_id: UUID
    token_version: int
    public_key_b64: str = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentView:
    agent_id: UUID
    name: str
    platform: AgentPlatform | None
    agent_version: str | None
    hostname: str | None
    os_version: str | None
    state: AgentState
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentRecord:
    id: UUID
    team_id: UUID
    owner_user_id: UUID
    name: str
    state: AgentState
    registration_code_digest: str | None = dataclass_field(repr=False)
    registration_code_expires_at: datetime | None
    registration_code_used_at: datetime | None
    access_token_digest: str | None = dataclass_field(repr=False)
    access_token_expires_at: datetime | None
    refresh_token_digest: str | None = dataclass_field(repr=False)
    refresh_token_expires_at: datetime | None
    token_version: int
    public_key_b64: str | None = dataclass_field(repr=False)
    platform: AgentPlatform | None
    agent_version: str | None
    hostname: str | None
    os_version: str | None
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentRepository(Protocol):
    async def create_pending(
        self,
        *,
        team_id: UUID,
        owner_user_id: UUID,
        name: str,
        registration_code_digest: str,
        registration_code_expires_at: datetime,
        now: datetime,
    ) -> AgentRecord: ...

    async def consume_registration(
        self,
        *,
        registration_code_digest: str,
        now: datetime,
        public_key_b64: str,
        platform: AgentPlatform,
        agent_version: str,
        hostname: str,
        os_version: str,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None: ...

    async def get_refresh_candidate(self, agent_id: UUID) -> AgentRecord | None: ...

    async def rotate_credentials(
        self,
        *,
        agent_id: UUID,
        expected_refresh_token_digest: str,
        expected_token_version: int,
        now: datetime,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None: ...

    async def find_access(
        self,
        *,
        access_token_digest: str,
        now: datetime,
    ) -> AgentRecord | None: ...

    async def list_team(self, team_id: UUID) -> tuple[AgentRecord, ...]: ...

    async def rename(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        name: str,
        now: datetime,
    ) -> AgentRecord | None: ...

    async def revoke(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        now: datetime,
    ) -> AgentRecord | None: ...


class InMemoryAgentRepository:
    def __init__(self, *, uuid_factory: Callable[[], UUID] = uuid4) -> None:
        self._uuid_factory = uuid_factory
        self._records: dict[UUID, AgentRecord] = {}
        self._lock = asyncio.Lock()

    async def create_pending(
        self,
        *,
        team_id: UUID,
        owner_user_id: UUID,
        name: str,
        registration_code_digest: str,
        registration_code_expires_at: datetime,
        now: datetime,
    ) -> AgentRecord:
        async with self._lock:
            if any(
                record.team_id == team_id and record.name == name
                for record in self._records.values()
            ):
                raise AgentNameConflictError
            record = AgentRecord(
                id=self._uuid_factory(),
                team_id=team_id,
                owner_user_id=owner_user_id,
                name=name,
                state="pending",
                registration_code_digest=registration_code_digest,
                registration_code_expires_at=registration_code_expires_at,
                registration_code_used_at=None,
                access_token_digest=None,
                access_token_expires_at=None,
                refresh_token_digest=None,
                refresh_token_expires_at=None,
                token_version=1,
                public_key_b64=None,
                platform=None,
                agent_version=None,
                hostname=None,
                os_version=None,
                last_heartbeat_at=None,
                created_at=now,
                updated_at=now,
            )
            self._records[record.id] = record
            return record

    async def consume_registration(
        self,
        *,
        registration_code_digest: str,
        now: datetime,
        public_key_b64: str,
        platform: AgentPlatform,
        agent_version: str,
        hostname: str,
        os_version: str,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None:
        async with self._lock:
            candidate = next(
                (
                    record
                    for record in self._records.values()
                    if record.registration_code_digest is not None
                    and hmac.compare_digest(
                        record.registration_code_digest,
                        registration_code_digest,
                    )
                ),
                None,
            )
            if (
                candidate is None
                or candidate.state != "pending"
                or candidate.registration_code_used_at is not None
                or candidate.registration_code_expires_at is None
                or candidate.registration_code_expires_at <= now
            ):
                return None
            registered = replace(
                candidate,
                state="offline",
                registration_code_digest=None,
                registration_code_used_at=now,
                public_key_b64=public_key_b64,
                platform=platform,
                agent_version=agent_version,
                hostname=hostname,
                os_version=os_version,
                access_token_digest=access_token_digest,
                access_token_expires_at=access_token_expires_at,
                refresh_token_digest=refresh_token_digest,
                refresh_token_expires_at=refresh_token_expires_at,
                updated_at=now,
            )
            self._records[registered.id] = registered
            return registered

    async def get_refresh_candidate(self, agent_id: UUID) -> AgentRecord | None:
        async with self._lock:
            return self._records.get(agent_id)

    async def rotate_credentials(
        self,
        *,
        agent_id: UUID,
        expected_refresh_token_digest: str,
        expected_token_version: int,
        now: datetime,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None:
        async with self._lock:
            candidate = self._records.get(agent_id)
            if (
                candidate is None
                or candidate.state == "revoked"
                or candidate.refresh_token_digest is None
                or not hmac.compare_digest(
                    candidate.refresh_token_digest,
                    expected_refresh_token_digest,
                )
                or candidate.refresh_token_expires_at is None
                or candidate.refresh_token_expires_at <= now
                or candidate.token_version != expected_token_version
            ):
                return None
            rotated = replace(
                candidate,
                access_token_digest=access_token_digest,
                access_token_expires_at=access_token_expires_at,
                refresh_token_digest=refresh_token_digest,
                refresh_token_expires_at=refresh_token_expires_at,
                token_version=candidate.token_version + 1,
                updated_at=now,
            )
            self._records[agent_id] = rotated
            return rotated

    async def find_access(
        self,
        *,
        access_token_digest: str,
        now: datetime,
    ) -> AgentRecord | None:
        async with self._lock:
            return next(
                (
                    record
                    for record in self._records.values()
                    if record.access_token_digest is not None
                    and hmac.compare_digest(
                        record.access_token_digest,
                        access_token_digest,
                    )
                    and record.access_token_expires_at is not None
                    and record.access_token_expires_at > now
                    and record.state != "revoked"
                ),
                None,
            )

    async def list_team(self, team_id: UUID) -> tuple[AgentRecord, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (record for record in self._records.values() if record.team_id == team_id),
                    key=lambda record: (record.name.casefold(), str(record.id)),
                )
            )

    async def rename(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        name: str,
        now: datetime,
    ) -> AgentRecord | None:
        async with self._lock:
            candidate = self._records.get(agent_id)
            if candidate is None or candidate.team_id != team_id:
                return None
            if any(
                record.id != agent_id and record.team_id == team_id and record.name == name
                for record in self._records.values()
            ):
                raise AgentNameConflictError
            renamed = replace(candidate, name=name, updated_at=now)
            self._records[agent_id] = renamed
            return renamed

    async def revoke(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        now: datetime,
    ) -> AgentRecord | None:
        async with self._lock:
            candidate = self._records.get(agent_id)
            if candidate is None or candidate.team_id != team_id:
                return None
            revoked = replace(
                candidate,
                state="revoked",
                registration_code_digest=None,
                access_token_digest=None,
                access_token_expires_at=None,
                refresh_token_digest=None,
                refresh_token_expires_at=None,
                public_key_b64=None,
                token_version=candidate.token_version + 1,
                updated_at=now,
            )
            self._records[agent_id] = revoked
            return revoked


class SQLAlchemyAgentRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def create_pending(
        self,
        *,
        team_id: UUID,
        owner_user_id: UUID,
        name: str,
        registration_code_digest: str,
        registration_code_expires_at: datetime,
        now: datetime,
    ) -> AgentRecord:
        try:
            async with self._session_factory() as session, session.begin():
                stored = StoredAgent(
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    name=name,
                    registration_code_digest=registration_code_digest,
                    registration_code_expires_at=registration_code_expires_at,
                    registration_code_used_at=None,
                    token_digest=None,
                    access_token_expires_at=None,
                    refresh_token_digest=None,
                    refresh_token_expires_at=None,
                    public_key_b64=None,
                    platform=None,
                    agent_version=None,
                    hostname=None,
                    os_version=None,
                    state="pending",
                    last_heartbeat_at=None,
                )
                session.add(stored)
                await session.flush()
                await session.refresh(stored)
                return _stored_agent_record(stored)
        except IntegrityError as error:
            if _integrity_constraint(error) == "uq_agents_team_name":
                raise AgentNameConflictError from None
            raise RuntimeError("Agent repository write failed") from None

    async def consume_registration(
        self,
        *,
        registration_code_digest: str,
        now: datetime,
        public_key_b64: str,
        platform: AgentPlatform,
        agent_version: str,
        hostname: str,
        os_version: str,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None:
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(StoredAgent)
                .where(
                    StoredAgent.registration_code_digest == registration_code_digest,
                    StoredAgent.registration_code_used_at.is_(None),
                    StoredAgent.state == "pending",
                )
                .with_for_update()
            )
            if (
                stored is None
                or stored.registration_code_digest is None
                or not hmac.compare_digest(
                    stored.registration_code_digest,
                    registration_code_digest,
                )
                or stored.registration_code_expires_at is None
                or stored.registration_code_expires_at <= now
            ):
                return None
            stored.registration_code_digest = None
            stored.registration_code_used_at = now
            stored.token_digest = access_token_digest
            stored.access_token_expires_at = access_token_expires_at
            stored.refresh_token_digest = refresh_token_digest
            stored.refresh_token_expires_at = refresh_token_expires_at
            stored.public_key_b64 = public_key_b64
            stored.platform = platform
            stored.agent_version = agent_version
            stored.hostname = hostname
            stored.os_version = os_version
            stored.state = "offline"
            stored.updated_at = now
            await session.flush()
            return _stored_agent_record(stored)

    async def get_refresh_candidate(self, agent_id: UUID) -> AgentRecord | None:
        async with self._session_factory() as session:
            stored = await session.get(StoredAgent, agent_id)
            return None if stored is None else _stored_agent_record(stored)

    async def rotate_credentials(
        self,
        *,
        agent_id: UUID,
        expected_refresh_token_digest: str,
        expected_token_version: int,
        now: datetime,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None:
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(StoredAgent).where(StoredAgent.id == agent_id).with_for_update()
            )
            if (
                stored is None
                or stored.state == "revoked"
                or stored.refresh_token_digest is None
                or not hmac.compare_digest(
                    stored.refresh_token_digest,
                    expected_refresh_token_digest,
                )
                or stored.refresh_token_expires_at is None
                or stored.refresh_token_expires_at <= now
                or stored.token_version != expected_token_version
            ):
                return None
            stored.token_digest = access_token_digest
            stored.access_token_expires_at = access_token_expires_at
            stored.refresh_token_digest = refresh_token_digest
            stored.refresh_token_expires_at = refresh_token_expires_at
            stored.token_version += 1
            stored.updated_at = now
            await session.flush()
            return _stored_agent_record(stored)

    async def find_access(
        self,
        *,
        access_token_digest: str,
        now: datetime,
    ) -> AgentRecord | None:
        async with self._session_factory() as session:
            stored = await session.scalar(
                select(StoredAgent).where(
                    StoredAgent.token_digest == access_token_digest,
                    StoredAgent.access_token_expires_at > now,
                    StoredAgent.state != "revoked",
                )
            )
            if (
                stored is None
                or stored.token_digest is None
                or not hmac.compare_digest(stored.token_digest, access_token_digest)
            ):
                return None
            return _stored_agent_record(stored)

    async def list_team(self, team_id: UUID) -> tuple[AgentRecord, ...]:
        async with self._session_factory() as session:
            stored_agents = (
                await session.scalars(
                    select(StoredAgent)
                    .where(StoredAgent.team_id == team_id)
                    .order_by(StoredAgent.name, StoredAgent.id)
                )
            ).all()
            return tuple(_stored_agent_record(stored) for stored in stored_agents)

    async def rename(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        name: str,
        now: datetime,
    ) -> AgentRecord | None:
        try:
            async with self._session_factory() as session, session.begin():
                stored = await session.scalar(
                    select(StoredAgent)
                    .where(
                        StoredAgent.id == agent_id,
                        StoredAgent.team_id == team_id,
                    )
                    .with_for_update()
                )
                if stored is None:
                    return None
                stored.name = name
                stored.updated_at = now
                await session.flush()
                return _stored_agent_record(stored)
        except IntegrityError as error:
            if _integrity_constraint(error) == "uq_agents_team_name":
                raise AgentNameConflictError from None
            raise RuntimeError("Agent repository write failed") from None

    async def revoke(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        now: datetime,
    ) -> AgentRecord | None:
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(StoredAgent)
                .where(
                    StoredAgent.id == agent_id,
                    StoredAgent.team_id == team_id,
                )
                .with_for_update()
            )
            if stored is None:
                return None
            if stored.state != "revoked":
                stored.state = "revoked"
                stored.registration_code_digest = None
                stored.token_digest = None
                stored.access_token_expires_at = None
                stored.refresh_token_digest = None
                stored.refresh_token_expires_at = None
                stored.public_key_b64 = None
                stored.token_version += 1
                stored.updated_at = now
                await session.execute(
                    update(AgentLease)
                    .where(
                        AgentLease.agent_id == agent_id,
                        AgentLease.state.in_(("active", "cancel_requested")),
                    )
                    .values(state="revoked", released_at=now, updated_at=now)
                )
                await session.execute(
                    update(Device)
                    .where(Device.agent_id == agent_id)
                    .values(state="offline", adb_state="offline", updated_at=now)
                )
                await session.flush()
                await session.refresh(stored)
            return _stored_agent_record(stored)


class AgentService:
    def __init__(
        self,
        *,
        repository: AgentRepository,
        credentials: AgentCredentialCodec,
        nonce_store: AgentNonceStore,
        task_signing_key: TaskSigningKey,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._credentials = credentials
        self._nonce_store = nonce_store
        self._task_signing_key = task_signing_key
        self._clock = clock

    async def create_registration_code(
        self,
        *,
        team_id: UUID,
        owner_user_id: UUID,
        name: str,
    ) -> RegistrationCodeIssue:
        normalized_name = _normalize_agent_name(name)
        now = _aware_utc(self._clock())
        code = self._credentials.issue_registration_code()
        expires_at = now + _REGISTRATION_LIFETIME
        record = await self._repository.create_pending(
            team_id=team_id,
            owner_user_id=owner_user_id,
            name=normalized_name,
            registration_code_digest=self._credentials.digest(code),
            registration_code_expires_at=expires_at,
            now=now,
        )
        return RegistrationCodeIssue(
            agent_id=record.id,
            registration_code=code,
            expires_at=expires_at,
        )

    async def register(
        self,
        registration: AgentRegistration,
    ) -> IssuedAgentCredentials:
        now = _aware_utc(self._clock())
        try:
            registration_digest = self._credentials.digest(registration.registration_code)
            decode_ed25519_public_key(registration.public_key_b64)
            _validate_registration_metadata(registration)
        except (AgentProofRejected, AgentInvalidRequestError, ValueError):
            raise AgentRegistrationRejected from None
        access_token = self._credentials.issue_access_token()
        refresh_token = self._credentials.issue_refresh_token()
        access_expires_at = now + _ACCESS_TOKEN_LIFETIME
        refresh_expires_at = now + _REFRESH_TOKEN_LIFETIME
        record = await self._repository.consume_registration(
            registration_code_digest=registration_digest,
            now=now,
            public_key_b64=registration.public_key_b64,
            platform=registration.platform,
            agent_version=registration.agent_version,
            hostname=registration.hostname,
            os_version=registration.os_version,
            access_token_digest=self._credentials.digest(access_token),
            access_token_expires_at=access_expires_at,
            refresh_token_digest=self._credentials.digest(refresh_token),
            refresh_token_expires_at=refresh_expires_at,
        )
        if record is None:
            raise AgentRegistrationRejected
        return self._issued_credentials(
            agent_id=record.id,
            team_id=record.team_id,
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
        )

    async def refresh(
        self,
        *,
        agent_id: UUID,
        refresh_token: str,
        nonce: str,
        timestamp: int,
        signature_b64: str,
    ) -> IssuedAgentCredentials:
        now = _aware_utc(self._clock())
        candidate = await self._repository.get_refresh_candidate(agent_id)
        if (
            candidate is None
            or candidate.state == "revoked"
            or candidate.public_key_b64 is None
            or candidate.refresh_token_expires_at is None
            or candidate.refresh_token_expires_at <= now
            or not self._credentials.matches(
                refresh_token,
                candidate.refresh_token_digest,
            )
        ):
            raise AgentAuthenticationRejected
        try:
            verify_refresh_proof(
                agent_id=agent_id,
                public_key_b64=candidate.public_key_b64,
                nonce=nonce,
                timestamp=timestamp,
                signature_b64=signature_b64,
                now=now,
            )
            if not await self._nonce_store.reserve(agent_id, nonce):
                raise AgentProofRejected
        except AgentProofRejected:
            raise AgentAuthenticationRejected from None

        access_token = self._credentials.issue_access_token()
        next_refresh_token = self._credentials.issue_refresh_token()
        access_expires_at = now + _ACCESS_TOKEN_LIFETIME
        refresh_expires_at = now + _REFRESH_TOKEN_LIFETIME
        rotated = await self._repository.rotate_credentials(
            agent_id=agent_id,
            expected_refresh_token_digest=candidate.refresh_token_digest or "",
            expected_token_version=candidate.token_version,
            now=now,
            access_token_digest=self._credentials.digest(access_token),
            access_token_expires_at=access_expires_at,
            refresh_token_digest=self._credentials.digest(next_refresh_token),
            refresh_token_expires_at=refresh_expires_at,
        )
        if rotated is None:
            raise AgentAuthenticationRejected
        return self._issued_credentials(
            agent_id=rotated.id,
            team_id=rotated.team_id,
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=next_refresh_token,
            refresh_expires_at=refresh_expires_at,
        )

    async def authenticate_access(self, access_token: str) -> AgentPrincipal:
        now = _aware_utc(self._clock())
        try:
            digest = self._credentials.digest(access_token)
        except ValueError:
            raise AgentAuthenticationRejected from None
        record = await self._repository.find_access(
            access_token_digest=digest,
            now=now,
        )
        if (
            record is None
            or record.public_key_b64 is None
            or not self._credentials.matches(access_token, record.access_token_digest)
        ):
            raise AgentAuthenticationRejected
        return AgentPrincipal(
            agent_id=record.id,
            team_id=record.team_id,
            token_version=record.token_version,
            public_key_b64=record.public_key_b64,
        )

    async def list_agents(self, *, team_id: UUID) -> tuple[AgentView, ...]:
        return tuple(_agent_view(record) for record in await self._repository.list_team(team_id))

    async def rename(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        name: str,
    ) -> AgentView:
        record = await self._repository.rename(
            team_id=team_id,
            agent_id=agent_id,
            name=_normalize_agent_name(name),
            now=_aware_utc(self._clock()),
        )
        if record is None:
            raise AgentNotFoundError
        return _agent_view(record)

    async def revoke(self, *, team_id: UUID, agent_id: UUID) -> AgentView:
        record = await self._repository.revoke(
            team_id=team_id,
            agent_id=agent_id,
            now=_aware_utc(self._clock()),
        )
        if record is None:
            raise AgentNotFoundError
        return _agent_view(record)

    def _issued_credentials(
        self,
        *,
        agent_id: UUID,
        team_id: UUID,
        access_token: str,
        access_expires_at: datetime,
        refresh_token: str,
        refresh_expires_at: datetime,
    ) -> IssuedAgentCredentials:
        return IssuedAgentCredentials(
            agent_id=agent_id,
            team_id=team_id,
            access_token=access_token,
            access_token_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_expires_at,
            task_signing_key=self._task_signing_key,
        )


def _normalize_agent_name(value: str) -> str:
    if not isinstance(value, str):
        raise AgentInvalidRequestError
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized) > _AGENT_NAME_MAX_LENGTH
        or any(unicodedata.category(character) == "Cc" for character in normalized)
        or not is_public_source_display_name(normalized)
    ):
        raise AgentInvalidRequestError
    return normalized


def _validate_display_string(value: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise AgentInvalidRequestError


def _validate_registration_metadata(registration: AgentRegistration) -> None:
    if registration.platform not in _PLATFORMS:
        raise AgentInvalidRequestError
    if (
        len(registration.agent_version) > 64
        or _AGENT_VERSION_PATTERN.fullmatch(registration.agent_version) is None
    ):
        raise AgentInvalidRequestError
    _validate_display_string(registration.hostname, maximum=200)
    _validate_display_string(registration.os_version, maximum=128)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Agent clock must return an aware datetime")
    return value.astimezone(UTC)


def _agent_view(record: AgentRecord) -> AgentView:
    return AgentView(
        agent_id=record.id,
        name=record.name,
        platform=record.platform,
        agent_version=record.agent_version,
        hostname=record.hostname,
        os_version=record.os_version,
        state=record.state,
        last_heartbeat_at=record.last_heartbeat_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _stored_agent_record(stored: StoredAgent) -> AgentRecord:
    return AgentRecord(
        id=stored.id,
        team_id=stored.team_id,
        owner_user_id=stored.owner_user_id,
        name=stored.name,
        state=cast(AgentState, stored.state),
        registration_code_digest=stored.registration_code_digest,
        registration_code_expires_at=stored.registration_code_expires_at,
        registration_code_used_at=stored.registration_code_used_at,
        access_token_digest=stored.token_digest,
        access_token_expires_at=stored.access_token_expires_at,
        refresh_token_digest=stored.refresh_token_digest,
        refresh_token_expires_at=stored.refresh_token_expires_at,
        token_version=stored.token_version,
        public_key_b64=stored.public_key_b64,
        platform=cast(AgentPlatform | None, stored.platform),
        agent_version=stored.agent_version,
        hostname=stored.hostname,
        os_version=stored.os_version,
        last_heartbeat_at=stored.last_heartbeat_at,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


def _integrity_constraint(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return constraint_name if isinstance(constraint_name, str) else None


__all__ = [
    "AgentAuthenticationRejected",
    "AgentCredentialCodec",
    "AgentInvalidRequestError",
    "AgentNameConflictError",
    "AgentNotFoundError",
    "AgentPrincipal",
    "AgentRecord",
    "AgentRegistration",
    "AgentRegistrationRejected",
    "AgentRepository",
    "AgentService",
    "AgentView",
    "InMemoryAgentRepository",
    "IssuedAgentCredentials",
    "RegistrationCodeIssue",
    "SQLAlchemyAgentRepository",
    "TaskSigningKey",
]
