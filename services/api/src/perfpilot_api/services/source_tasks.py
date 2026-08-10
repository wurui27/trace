from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import Agent, AgentLease, SourceTask

_LEASE_TTL = timedelta(seconds=60)
_MAX_COMPLETION_BYTES = 128 * 1024
_ACTIVE_STATES = frozenset({"queued", "leased", "running", "cancel_requested"})
_LEASED_STATES = frozenset({"leased", "running", "cancel_requested"})
_RULE_ID = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_COMPLETION_CONTRACT = (
    Path(__file__).resolve().parents[5]
    / "contracts"
    / "v1"
    / "agents"
    / "source-task-completion.schema.json"
)


class SourceTaskError(RuntimeError):
    pass


class SourceTaskNotFound(SourceTaskError):
    pass


class StaleSourceTaskLease(SourceTaskError):
    pass


class SourceTaskConflict(SourceTaskError):
    pass


class SourceTaskInvalid(SourceTaskError):
    pass


class SourceTaskTooLarge(SourceTaskInvalid):
    pass


@dataclass(frozen=True, slots=True)
class SourceCompletionArtifact:
    artifact_id: UUID
    checksum: str


@dataclass(frozen=True, slots=True)
class SourceTaskView:
    id: UUID
    execution_id: UUID
    team_id: UUID
    analysis_id: UUID
    agent_id: UUID
    workspace_id: UUID
    task_type: Literal["source_context", "patch_verification"]
    state: str
    lease_version: int
    expires_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SourceTaskDelivery:
    execution_id: UUID
    analysis_id: UUID
    agent_id: UUID
    task_type: Literal["source_context", "patch_verification"]
    lease_version: int
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    snapshot: dict[str, object] = field(repr=False)
    signature_b64: str = field(default="", repr=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SourceTaskMutation:
    execution_id: UUID
    analysis_id: UUID
    lease_version: int
    state: str
    occurred_at: datetime
    artifact_id: UUID | None = None
    checksum: str | None = None


@dataclass(slots=True)
class _MemorySourceTask:
    id: UUID
    team_id: UUID
    analysis_id: UUID
    agent_id: UUID
    workspace_id: UUID
    task_type: Literal["source_context", "patch_verification"]
    state: str
    lease_version: int
    request_document: dict[str, object] = field(repr=False)
    request_sha256: str
    created_at: datetime
    updated_at: datetime
    lease_token_digest: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    completion_artifact_id: UUID | None = None
    completion_sha256: str | None = None
    failure_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SourceTaskCompletionRecorder(Protocol):
    async def record_completion(
        self,
        *,
        task: SourceTaskView,
        document: Mapping[str, object],
        checksum: str,
        now: datetime,
    ) -> SourceCompletionArtifact: ...


class SourceTaskRepository(Protocol):
    async def create_or_get(self, task: _MemorySourceTask) -> _MemorySourceTask: ...
    async def active_for_agent(self, *, agent_id: UUID, now: datetime) -> _MemorySourceTask | None: ...
    async def lease_oldest(
        self,
        *,
        agent_id: UUID,
        token_digest: str,
        expires_at: datetime,
        now: datetime,
    ) -> _MemorySourceTask | None: ...
    async def by_execution(self, execution_id: UUID) -> _MemorySourceTask | None: ...
    async def expire_stale(self, *, now: datetime) -> int: ...
    async def request_cancel_locked(
        self, *, team_id: UUID, analysis_id: UUID, now: datetime
    ) -> _MemorySourceTask: ...
    async def mutate_fenced(
        self,
        *,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        token_digest: str,
        operation: str,
        now: datetime,
        expires_at: datetime | None = None,
        completion_artifact_id: UUID | None = None,
        completion_sha256: str | None = None,
        completion_state: str | None = None,
        failure_code: str | None = None,
    ) -> tuple[_MemorySourceTask, bool]: ...


class InMemorySourceTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[UUID, _MemorySourceTask] = {}

    async def create_or_get(self, task: _MemorySourceTask) -> _MemorySourceTask:
        for existing in self.tasks.values():
            if existing.state not in _ACTIVE_STATES or existing.task_type != task.task_type:
                continue
            if task.task_type == "source_context" and existing.analysis_id == task.analysis_id:
                return existing
            if (
                task.task_type == "patch_verification"
                and existing.analysis_id == task.analysis_id
                and existing.request_document.get("fix_id") == task.request_document.get("fix_id")
            ):
                return existing
        self.tasks[task.id] = task
        return task

    async def active_for_agent(self, *, agent_id: UUID, now: datetime) -> _MemorySourceTask | None:
        candidates = [
            task
            for task in self.tasks.values()
            if task.agent_id == agent_id
            and task.state in _LEASED_STATES
            and task.expires_at is not None
            and task.expires_at > now
        ]
        return min(candidates, key=lambda item: (item.created_at, item.id), default=None)

    async def lease_oldest(
        self,
        *,
        agent_id: UUID,
        token_digest: str,
        expires_at: datetime,
        now: datetime,
    ) -> _MemorySourceTask | None:
        candidates = [
            task
            for task in self.tasks.values()
            if task.agent_id == agent_id and task.state == "queued"
        ]
        task = min(candidates, key=lambda item: (item.created_at, item.id), default=None)
        if task is None:
            return None
        task.lease_token_digest = token_digest
        task.expires_at = expires_at
        task.state = "leased"
        task.started_at = now
        task.updated_at = now
        return task

    async def by_execution(self, execution_id: UUID) -> _MemorySourceTask | None:
        return self.tasks.get(execution_id)

    async def expire_stale(self, *, now: datetime) -> int:
        expired = 0
        for task in self.tasks.values():
            if task.state in _LEASED_STATES and task.expires_at is not None and task.expires_at <= now:
                task.state = "expired"
                task.failure_code = "source_task_lease_expired"
                task.completed_at = now
                task.updated_at = now
                expired += 1
        return expired

    async def mutate_fenced(
        self,
        *,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        token_digest: str,
        operation: str,
        now: datetime,
        expires_at: datetime | None = None,
        completion_artifact_id: UUID | None = None,
        completion_sha256: str | None = None,
        completion_state: str | None = None,
        failure_code: str | None = None,
    ) -> tuple[_MemorySourceTask, bool]:
        task = self.tasks.get(execution_id)
        if task is None or task.agent_id != agent_id:
            raise SourceTaskNotFound
        if (
            task.lease_version != lease_version
            or task.lease_token_digest is None
            or not hmac.compare_digest(task.lease_token_digest, token_digest)
        ):
            raise StaleSourceTaskLease
        if operation == "complete" and task.completion_sha256 is not None:
            if completion_sha256 is None or not hmac.compare_digest(
                task.completion_sha256, completion_sha256
            ):
                raise SourceTaskConflict
            return task, False
        expected = {
            "renew": {"leased", "running", "cancel_requested"},
            "ack_cancel": {"cancel_requested"},
            "complete": {"leased", "running"},
        }.get(operation)
        if expected is None or task.state not in expected:
            raise SourceTaskConflict
        if operation == "renew":
            if task.state != "cancel_requested":
                task.state = "running"
            task.expires_at = expires_at
        elif operation == "ack_cancel":
            task.state = "canceled"
            task.completed_at = now
        else:
            task.state = completion_state or "failed"
            task.completion_artifact_id = completion_artifact_id
            task.completion_sha256 = completion_sha256
            task.failure_code = failure_code
            task.completed_at = now
        task.updated_at = now
        return task, True

    async def request_cancel_locked(
        self, *, team_id: UUID, analysis_id: UUID, now: datetime
    ) -> _MemorySourceTask:
        task = next(
            (
                item
                for item in self.tasks.values()
                if item.team_id == team_id
                and item.analysis_id == analysis_id
                and item.state in _ACTIVE_STATES
            ),
            None,
        )
        if task is None:
            raise SourceTaskNotFound
        if task.state == "queued":
            task.state = "canceled"
            task.completed_at = now
        elif task.state in {"leased", "running"}:
            task.state = "cancel_requested"
        task.updated_at = now
        return task


def _memory_task(row: SourceTask) -> _MemorySourceTask:
    return _MemorySourceTask(
        id=row.id,
        team_id=row.team_id,
        analysis_id=row.analysis_id,
        agent_id=row.agent_id,
        workspace_id=row.workspace_id,
        task_type=row.task_type,  # type: ignore[arg-type]
        state=row.state,
        lease_version=row.lease_version,
        request_document=dict(row.request_document),
        request_sha256=row.request_sha256,
        created_at=row.created_at,
        updated_at=row.updated_at,
        lease_token_digest=row.lease_token_digest,
        expires_at=row.expires_at,
        completion_artifact_id=row.completion_artifact_id,
        completion_sha256=row.completion_sha256,
        failure_code=row.failure_code,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


class SQLAlchemySourceTaskRepository:
    """Control-database persistence for metadata-only source task leases."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._sessions = session_factory

    async def create_or_get(self, task: _MemorySourceTask) -> _MemorySourceTask:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    row = SourceTask(
                        id=task.id,
                        team_id=task.team_id,
                        analysis_id=task.analysis_id,
                        agent_id=task.agent_id,
                        workspace_id=task.workspace_id,
                        task_type=task.task_type,
                        state=task.state,
                        lease_version=task.lease_version,
                        lease_token_digest=None,
                        expires_at=None,
                        request_document=dict(task.request_document),
                        request_sha256=task.request_sha256,
                        completion_artifact_id=None,
                        completion_sha256=None,
                        failure_code=None,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                        started_at=None,
                        completed_at=None,
                        version=1,
                    )
                    session.add(row)
                    await session.flush()
                    await session.refresh(row)
                    return _memory_task(row)
        except IntegrityError:
            async with self._sessions() as session:
                statement = select(SourceTask).where(
                    SourceTask.analysis_id == task.analysis_id,
                    SourceTask.task_type == task.task_type,
                    SourceTask.state.in_(_ACTIVE_STATES),
                )
                if task.task_type == "patch_verification":
                    statement = statement.where(
                        SourceTask.request_document["fix_id"].astext
                        == task.request_document["fix_id"]
                    )
                existing = await session.scalar(statement.limit(1))
                if existing is None:
                    raise
                return _memory_task(existing)

    async def active_for_agent(
        self, *, agent_id: UUID, now: datetime
    ) -> _MemorySourceTask | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SourceTask)
                .where(
                    SourceTask.agent_id == agent_id,
                    SourceTask.state.in_(_LEASED_STATES),
                    SourceTask.expires_at > now,
                )
                .order_by(SourceTask.created_at, SourceTask.id)
                .limit(1)
            )
            return None if row is None else _memory_task(row)

    async def lease_oldest(
        self,
        *,
        agent_id: UUID,
        token_digest: str,
        expires_at: datetime,
        now: datetime,
    ) -> _MemorySourceTask | None:
        async with self._sessions() as session:
            async with session.begin():
                agent = await session.scalar(
                    select(Agent).where(Agent.id == agent_id).with_for_update()
                )
                if agent is None:
                    return None
                active_device_lease = await session.scalar(
                    select(AgentLease.id)
                    .where(
                        AgentLease.agent_id == agent_id,
                        AgentLease.state.in_(("active", "cancel_requested")),
                        AgentLease.expires_at > now,
                    )
                    .limit(1)
                )
                if active_device_lease is not None:
                    return None
                active = await session.scalar(
                    select(SourceTask.id)
                    .where(
                        SourceTask.agent_id == agent_id,
                        SourceTask.state.in_(_LEASED_STATES),
                        SourceTask.expires_at > now,
                    )
                    .limit(1)
                )
                if active is not None:
                    return None
                row = await session.scalar(
                    select(SourceTask)
                    .where(SourceTask.agent_id == agent_id, SourceTask.state == "queued")
                    .order_by(SourceTask.created_at, SourceTask.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                row.state = "leased"
                row.lease_token_digest = token_digest
                row.expires_at = expires_at
                row.started_at = now
                row.updated_at = now
                row.version += 1
                await session.flush()
                await session.refresh(row)
                return _memory_task(row)

    async def by_execution(self, execution_id: UUID) -> _MemorySourceTask | None:
        async with self._sessions() as session:
            row = await session.get(SourceTask, execution_id)
            return None if row is None else _memory_task(row)

    async def expire_stale(self, *, now: datetime) -> int:
        async with self._sessions() as session:
            async with session.begin():
                result = await session.execute(
                    update(SourceTask)
                    .where(
                        SourceTask.state.in_(_LEASED_STATES),
                        SourceTask.expires_at <= now,
                    )
                    .values(
                        state="expired",
                        failure_code="source_task_lease_expired",
                        completed_at=now,
                        updated_at=now,
                        version=SourceTask.version + 1,
                    )
                )
                return int(result.rowcount or 0)

    async def mutate_fenced(
        self,
        *,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        token_digest: str,
        operation: str,
        now: datetime,
        expires_at: datetime | None = None,
        completion_artifact_id: UUID | None = None,
        completion_sha256: str | None = None,
        completion_state: str | None = None,
        failure_code: str | None = None,
    ) -> tuple[_MemorySourceTask, bool]:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SourceTask)
                    .where(SourceTask.id == execution_id)
                    .with_for_update()
                )
                if row is None or row.agent_id != agent_id:
                    raise SourceTaskNotFound
                if (
                    row.lease_version != lease_version
                    or row.lease_token_digest is None
                    or not hmac.compare_digest(row.lease_token_digest, token_digest)
                ):
                    raise StaleSourceTaskLease
                if operation == "complete" and row.completion_sha256 is not None:
                    if completion_sha256 is None or not hmac.compare_digest(
                        row.completion_sha256, completion_sha256
                    ):
                        raise SourceTaskConflict
                    return _memory_task(row), False
                expected = {
                    "renew": {"leased", "running", "cancel_requested"},
                    "ack_cancel": {"cancel_requested"},
                    "complete": {"leased", "running"},
                }.get(operation)
                if expected is None or row.state not in expected:
                    raise SourceTaskConflict
                if operation == "renew":
                    if row.state != "cancel_requested":
                        row.state = "running"
                    row.expires_at = expires_at
                elif operation == "ack_cancel":
                    row.state = "canceled"
                    row.completed_at = now
                else:
                    row.state = completion_state or "failed"
                    row.completion_artifact_id = completion_artifact_id
                    row.completion_sha256 = completion_sha256
                    row.failure_code = failure_code
                    row.completed_at = now
                row.updated_at = now
                row.version += 1
                await session.flush()
                await session.refresh(row)
                return _memory_task(row), True

    async def request_cancel_locked(
        self, *, team_id: UUID, analysis_id: UUID, now: datetime
    ) -> _MemorySourceTask:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SourceTask)
                    .where(
                        SourceTask.team_id == team_id,
                        SourceTask.analysis_id == analysis_id,
                        SourceTask.state.in_(_ACTIVE_STATES),
                    )
                    .order_by(SourceTask.created_at, SourceTask.id)
                    .limit(1)
                    .with_for_update()
                )
                if row is None:
                    raise SourceTaskNotFound
                if row.state == "queued":
                    row.state = "canceled"
                    row.completed_at = now
                elif row.state in {"leased", "running"}:
                    row.state = "cancel_requested"
                row.updated_at = now
                row.version += 1
                await session.flush()
                await session.refresh(row)
                return _memory_task(row)


def _canonical_json(document: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise SourceTaskInvalid from None


@lru_cache(maxsize=1)
def _completion_validator() -> Draft202012Validator:
    try:
        schema = json.loads(_COMPLETION_CONTRACT.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        raise SourceTaskInvalid from None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Source task clock must be timezone-aware")
    return now.astimezone(UTC)


class SourceTaskService:
    def __init__(
        self,
        *,
        repository: SourceTaskRepository,
        signer: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        execution_id_source: Callable[[], UUID] = uuid4,
        lease_token_source: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    ) -> None:
        self._repository = repository
        self._signer = signer
        self._clock = clock
        self._execution_id_source = execution_id_source
        self._lease_token_source = lease_token_source
        self._issued_tokens: dict[UUID, str] = {}

    async def create_context_task(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        agent_id: UUID,
        workspace_id: UUID,
        validation_profile_id: UUID | None,
        finding_hints: Sequence[Mapping[str, object]],
    ) -> SourceTaskView:
        self._validate_finding_hints(finding_hints)
        request = {
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": (
                None if validation_profile_id is None else str(validation_profile_id)
            ),
            "finding_hints": [dict(item) for item in finding_hints],
            "limits": {"max_findings": 3, "max_files": 12, "max_bytes": 98_304},
        }
        return await self._create(
            team_id=team_id,
            analysis_id=analysis_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            task_type="source_context",
            request=request,
        )

    async def create_patch_task(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        agent_id: UUID,
        workspace_id: UUID,
        validation_profile_id: UUID,
        snapshot_id: UUID,
        snapshot_hash: str,
        fix_id: UUID,
        patch: str,
    ) -> SourceTaskView:
        if (
            len(patch.encode("utf-8")) > 65_536
            or not patch
            or len(snapshot_hash) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_hash)
        ):
            raise SourceTaskInvalid
        request = {
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": str(validation_profile_id),
            "snapshot_id": str(snapshot_id),
            "snapshot_hash": snapshot_hash,
            "fix_id": str(fix_id),
            "patch": patch,
        }
        return await self._create(
            team_id=team_id,
            analysis_id=analysis_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            task_type="patch_verification",
            request=request,
        )

    async def _create(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        agent_id: UUID,
        workspace_id: UUID,
        task_type: Literal["source_context", "patch_verification"],
        request: dict[str, object],
    ) -> SourceTaskView:
        now = _aware(self._clock())
        canonical = _canonical_json(request)
        created = await self._repository.create_or_get(
            _MemorySourceTask(
                # SourceTask.id is the protocol execution_id. Keeping one identity
                # avoids a second control-plane identifier absent from the contract.
                id=self._execution_id_source(),
                team_id=team_id,
                analysis_id=analysis_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                task_type=task_type,
                state="queued",
                lease_version=1,
                request_document=request,
                request_sha256=hashlib.sha256(canonical).hexdigest(),
                created_at=now,
                updated_at=now,
            )
        )
        return self._view(created)

    async def lease_next(self, *, agent_id: UUID) -> SourceTaskDelivery | None:
        now = _aware(self._clock())
        await self._repository.expire_stale(now=now)
        task = await self._repository.active_for_agent(agent_id=agent_id, now=now)
        token: str | None = None
        if task is not None:
            token = self._issued_tokens.get(task.id)
        if task is None:
            raw = self._lease_token_source()
            if not isinstance(raw, bytes) or len(raw) < 16:
                raise SourceTaskInvalid
            token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            task = await self._repository.lease_oldest(
                agent_id=agent_id,
                token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
                expires_at=now + _LEASE_TTL,
                now=now,
            )
            if task is not None:
                self._issued_tokens[task.id] = token
        if task is None or task.expires_at is None or token is None:
            return None
        snapshot = self._snapshot(task)
        signature = ""
        if self._signer is not None:
            signature = self._signer.sign(snapshot)
        return SourceTaskDelivery(
            execution_id=task.id,
            analysis_id=task.analysis_id,
            agent_id=task.agent_id,
            task_type=task.task_type,
            lease_version=task.lease_version,
            lease_token=token,
            lease_expires_at=task.expires_at,
            snapshot=snapshot,
            signature_b64=signature,
            created_at=task.created_at,
        )

    async def renew(
        self,
        *,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        lease_token: str,
    ) -> SourceTaskMutation:
        now = _aware(self._clock())
        task, _ = await self._repository.mutate_fenced(
            execution_id=execution_id,
            agent_id=agent_id,
            lease_version=lease_version,
            token_digest=self._token_digest(lease_token),
            operation="renew",
            now=now,
            expires_at=now + _LEASE_TTL,
        )
        return self._mutation(task, now)

    async def request_cancel(self, *, team_id: UUID, analysis_id: UUID) -> SourceTaskMutation:
        now = _aware(self._clock())
        task = await self._repository.request_cancel_locked(
            team_id=team_id,
            analysis_id=analysis_id,
            now=now,
        )
        return self._mutation(task, now)

    async def ack_cancel(
        self,
        *,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        lease_token: str,
    ) -> SourceTaskMutation:
        now = _aware(self._clock())
        task, _ = await self._repository.mutate_fenced(
            execution_id=execution_id,
            agent_id=agent_id,
            lease_version=lease_version,
            token_digest=self._token_digest(lease_token),
            operation="ack_cancel",
            now=now,
        )
        self._issued_tokens.pop(execution_id, None)
        return self._mutation(task, now)

    async def complete(
        self,
        *,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        lease_token: str,
        completion_document: Mapping[str, object],
        recorder: SourceTaskCompletionRecorder,
    ) -> SourceTaskMutation:
        now = _aware(self._clock())
        canonical = _canonical_json(completion_document)
        if len(canonical) > _MAX_COMPLETION_BYTES:
            raise SourceTaskTooLarge
        try:
            _completion_validator().validate(completion_document)
        except ValidationError:
            raise SourceTaskInvalid from None
        checksum = hashlib.sha256(canonical).hexdigest()
        task = await self._fenced(
            execution_id,
            agent_id,
            lease_version,
            lease_token,
            allow_completed=True,
        )
        if task.completion_sha256 is not None:
            if not hmac.compare_digest(task.completion_sha256, checksum):
                raise SourceTaskConflict
            return self._mutation(task, task.completed_at or now)
        if task.state not in {"leased", "running"}:
            raise SourceTaskConflict
        self._validate_completion_identity(task, completion_document)
        artifact = await recorder.record_completion(
            task=self._view(task),
            document=dict(completion_document),
            checksum=checksum,
            now=now,
        )
        if not hmac.compare_digest(artifact.checksum, checksum):
            raise SourceTaskConflict
        state = completion_document.get("state")
        completion_state = "completed" if state == "completed" else "failed"
        if state in {"canceled", "expired"}:
            completion_state = str(state)
        failure_code = None
        result = completion_document.get("result")
        if completion_state != "completed" and isinstance(result, Mapping):
            failure = result.get("failure_code")
            failure_code = failure if isinstance(failure, str) else "source_task_failed"
        task, _ = await self._repository.mutate_fenced(
            execution_id=execution_id,
            agent_id=agent_id,
            lease_version=lease_version,
            token_digest=self._token_digest(lease_token),
            operation="complete",
            now=now,
            completion_artifact_id=artifact.artifact_id,
            completion_sha256=checksum,
            completion_state=completion_state,
            failure_code=failure_code,
        )
        self._issued_tokens.pop(execution_id, None)
        return self._mutation(task, now)

    async def expire_stale(self) -> int:
        return await self._repository.expire_stale(now=_aware(self._clock()))

    async def owns(self, *, execution_id: UUID, agent_id: UUID) -> bool:
        task = await self._repository.by_execution(execution_id)
        return task is not None and task.agent_id == agent_id

    async def _fenced(
        self,
        execution_id: UUID,
        agent_id: UUID,
        lease_version: int,
        lease_token: str,
        *,
        allow_completed: bool = False,
    ) -> _MemorySourceTask:
        task = await self._repository.by_execution(execution_id)
        if task is None or task.agent_id != agent_id:
            raise SourceTaskNotFound
        digest = hashlib.sha256(lease_token.encode("ascii")).hexdigest()
        if (
            task.lease_version != lease_version
            or task.lease_token_digest is None
            or not hmac.compare_digest(task.lease_token_digest, digest)
            or (not allow_completed and task.state not in _LEASED_STATES)
        ):
            raise StaleSourceTaskLease
        return task

    @staticmethod
    def _token_digest(lease_token: str) -> str:
        try:
            encoded = lease_token.encode("ascii")
        except UnicodeError:
            raise StaleSourceTaskLease from None
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_completion_identity(
        task: _MemorySourceTask, document: Mapping[str, object]
    ) -> None:
        if (
            document.get("task_type") != task.task_type
            or document.get("execution_id") != str(task.id)
            or document.get("analysis_id") != str(task.analysis_id)
            or document.get("workspace_id") != str(task.workspace_id)
            or document.get("lease_version") != task.lease_version
        ):
            raise SourceTaskConflict

    @staticmethod
    def _validate_finding_hints(hints: Sequence[Mapping[str, object]]) -> None:
        if len(hints) > 3:
            raise SourceTaskInvalid
        seen: set[str] = set()
        for hint in hints:
            if set(hint) != {"finding_id", "evidence_ids", "rule_id", "symbol_hints"}:
                raise SourceTaskInvalid
            finding_id = hint.get("finding_id")
            evidence_ids = hint.get("evidence_ids")
            rule_id = hint.get("rule_id")
            symbols = hint.get("symbol_hints")
            try:
                normalized_finding = str(UUID(str(finding_id)))
                normalized_evidence = [str(UUID(str(item))) for item in evidence_ids]  # type: ignore[union-attr]
            except (TypeError, ValueError):
                raise SourceTaskInvalid from None
            if (
                normalized_finding != finding_id
                or normalized_finding in seen
                or not isinstance(evidence_ids, (list, tuple))
                or len(evidence_ids) > 20
                or len(set(normalized_evidence)) != len(normalized_evidence)
                or normalized_evidence != list(evidence_ids)
                or not isinstance(rule_id, str)
                or _RULE_ID.fullmatch(rule_id) is None
                or not isinstance(symbols, (list, tuple))
                or len(symbols) > 8
                or len(set(symbols)) != len(symbols)
                or any(
                    not isinstance(symbol, str)
                    or not 1 <= len(symbol) <= 255
                    or any(ord(character) < 32 or ord(character) == 127 for character in symbol)
                    for symbol in symbols
                )
            ):
                raise SourceTaskInvalid
            seen.add(normalized_finding)

    @staticmethod
    def _snapshot(task: _MemorySourceTask) -> dict[str, object]:
        if task.expires_at is None:
            raise SourceTaskInvalid
        return {
            "schema_version": "1.0",
            "task_type": task.task_type,
            "execution_id": str(task.id),
            "analysis_id": str(task.analysis_id),
            "team_id": str(task.team_id),
            "agent_id": str(task.agent_id),
            "workspace_id": str(task.workspace_id),
            **task.request_document,
            "lease_version": task.lease_version,
            "expires_at": task.expires_at.isoformat(),
        }

    @staticmethod
    def _view(task: _MemorySourceTask) -> SourceTaskView:
        return SourceTaskView(
            id=task.id,
            execution_id=task.id,
            team_id=task.team_id,
            analysis_id=task.analysis_id,
            agent_id=task.agent_id,
            workspace_id=task.workspace_id,
            task_type=task.task_type,
            state=task.state,
            lease_version=task.lease_version,
            expires_at=task.expires_at,
            created_at=task.created_at,
        )

    @staticmethod
    def _mutation(task: _MemorySourceTask, now: datetime) -> SourceTaskMutation:
        return SourceTaskMutation(
            execution_id=task.id,
            analysis_id=task.analysis_id,
            lease_version=task.lease_version,
            state=task.state,
            occurred_at=now,
            artifact_id=task.completion_artifact_id,
            checksum=task.completion_sha256,
        )


__all__ = [
    "InMemorySourceTaskRepository",
    "SourceCompletionArtifact",
    "SourceTaskCompletionRecorder",
    "SourceTaskConflict",
    "SourceTaskDelivery",
    "SourceTaskError",
    "SourceTaskInvalid",
    "SourceTaskMutation",
    "SourceTaskNotFound",
    "SourceTaskService",
    "SourceTaskTooLarge",
    "SourceTaskView",
    "SQLAlchemySourceTaskRepository",
    "StaleSourceTaskLease",
]
