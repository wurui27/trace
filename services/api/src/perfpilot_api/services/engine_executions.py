"""Control-plane persistence and orchestration for external engine attempts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal, Protocol
from uuid import UUID, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import EngineExecution, GlobalJob, OutboxEvent
from perfpilot_api.config import Settings
from perfpilot_api.engines.contracts import (
    AnalysisProfile,
    EngineAdapter,
    EngineEventBatch,
    EngineInput,
    EngineRetryDirective,
    EngineRunRef,
    EngineStatus,
    EngineStepOutcome,
    ExecutionStateValue,
    SubmitConfig,
)
from perfpilot_api.engines.canonical_results import (
    EngineResultValidationError,
    EngineResultWrite,
    result_artifact_id,
)
from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.lock import EngineLock, EnginePin
from perfpilot_api.engines.registry import AdapterRegistry
from perfpilot_api.engines.smartperfetto import SmartPerfettoAdapter
from perfpilot_api.engines.smartperfetto_transport import (
    CredentialResolver,
    SmartPerfettoTransport,
    validate_external_id,
)
from perfpilot_api.engines.states import transition_engine_state
from perfpilot_api.services.engine_workspaces import (
    EngineWorkspaceService,
    SQLAlchemyEngineWorkspaceRepository,
)
from perfpilot_api.services.engine_result_artifacts import (
    EngineResultConflictError,
    EngineResultSink,
)


_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_CANONICAL_CURSOR = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_TERMINAL_STATES = {"completed", "insufficient_data", "failed", "canceled"}
_OBSERVABLE_STATES = {"running", "awaiting_user"}
_ANALYSIS_ENGINE_PAIRS = {
    "trace_upload": frozenset({"smartperfetto"}),
    "memory_upload": frozenset({"android_memory"}),
    "device": frozenset({"smartperfetto", "android_memory"}),
}
_NEW_ATTEMPT_CODES = frozenset({"capacity_exceeded", "worker_unavailable", "engine_timeout"})
_TERMINAL_ADAPTER_CODES = frozenset(
    {"integrity_mismatch", "incompatible_contract", "privacy_violation"}
)
_MEMORY_RUN_ID = re.compile(r"memory-[0-9a-f]{32}\Z")
_SYNTHESIS_EVENT_NAMESPACE = UUID("d4fa0d89-2a62-584a-9f04-79ba674512b9")


class EngineExecutionNotFoundError(RuntimeError):
    """The scoped execution does not exist."""


class StaleEngineExecutionVersionError(RuntimeError):
    """The execution changed after the caller loaded it."""


class EngineExecutionOwnershipError(RuntimeError):
    """A refreshed route no longer belongs to this execution."""


def engine_result_ready_event_id(execution_id: UUID) -> UUID:
    if not isinstance(execution_id, UUID):
        raise ValueError("engine execution id is invalid")
    return uuid5(_SYNTHESIS_EVENT_NAMESPACE, f"engine_result_ready:{execution_id}")


@dataclass(frozen=True, slots=True)
class EngineExecutionSeed:
    engine_id: str
    tenant_resource_version: int
    adapter_version: str
    engine_commit_sha: str
    engine_image_digest: str
    input_manifest_hash: str
    config_hash: str


@dataclass(frozen=True, slots=True)
class EngineExecutionRecord:
    id: UUID
    analysis_id: UUID
    team_id: UUID
    engine_id: str
    attempt_number: int
    tenant_resource_version: int
    adapter_version: str
    engine_commit_sha: str
    engine_image_digest: str
    input_manifest_hash: str
    config_hash: str
    external_workspace_id: str | None
    external_session_id: str | None
    external_run_id: str | None
    state: ExecutionStateValue
    last_event_cursor: str | None
    stable_error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    raw_result_artifact_id: UUID | None
    normalized_report_version_id: UUID | None
    version: int


@dataclass(frozen=True, slots=True)
class FinalizationClaim:
    record: EngineExecutionRecord
    is_owner: bool


@dataclass(frozen=True, slots=True)
class RetryReservation:
    current_attempt: EngineExecutionRecord
    next_attempt: EngineExecutionRecord | None


class EngineExecutionRepository(Protocol):
    async def allocate_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        seed: EngineExecutionSeed,
        now: datetime,
    ) -> EngineExecutionRecord: ...

    async def get(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineExecutionRecord: ...


def _validate_stable_code(value: str) -> str:
    if _STABLE_CODE.fullmatch(value) is None:
        raise ValueError("stable engine error code is invalid")
    return value


def _validate_tenant_resource_version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("tenant resource version is invalid")
    return value


def _validate_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    if _CANONICAL_CURSOR.fullmatch(value) is None:
        raise ValueError("engine cursor is invalid")
    return int(value)


class SQLAlchemyEngineExecutionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _record(row: EngineExecution) -> EngineExecutionRecord:
        return EngineExecutionRecord(
            id=row.id,
            analysis_id=row.analysis_id,
            team_id=row.team_id,
            engine_id=row.engine_id,
            attempt_number=row.attempt_number,
            tenant_resource_version=row.tenant_resource_version,
            adapter_version=row.adapter_version,
            engine_commit_sha=row.engine_commit_sha,
            engine_image_digest=row.engine_image_digest,
            input_manifest_hash=row.input_manifest_hash,
            config_hash=row.config_hash,
            external_workspace_id=row.external_workspace_id,
            external_session_id=row.external_session_id,
            external_run_id=row.external_run_id,
            state=row.state,  # type: ignore[arg-type]
            last_event_cursor=row.last_event_cursor,
            stable_error_code=row.stable_error_code,
            started_at=row.started_at,
            completed_at=row.completed_at,
            raw_result_artifact_id=row.raw_result_artifact_id,
            normalized_report_version_id=row.normalized_report_version_id,
            version=row.version,
        )

    @staticmethod
    async def _job(
        session: AsyncSession,
        *,
        team_id: UUID,
        analysis_id: UUID,
        for_update: bool,
    ) -> GlobalJob:
        statement = select(GlobalJob).where(
            GlobalJob.id == analysis_id,
            GlobalJob.team_id == team_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise EngineExecutionNotFoundError("analysis was not found")
        return row

    @staticmethod
    async def _execution(
        session: AsyncSession,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        for_update: bool,
    ) -> EngineExecution:
        statement = select(EngineExecution).where(
            EngineExecution.id == execution_id,
            EngineExecution.team_id == team_id,
            EngineExecution.analysis_id == analysis_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise EngineExecutionNotFoundError("engine execution was not found")
        return row

    async def allocate_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        seed: EngineExecutionSeed,
        now: datetime,
    ) -> EngineExecutionRecord:
        _validate_tenant_resource_version(seed.tenant_resource_version)
        async with self._session_factory.begin() as session:
            job = await self._job(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                for_update=True,
            )
            if seed.engine_id not in _ANALYSIS_ENGINE_PAIRS.get(
                job.analysis_mode, frozenset()
            ) or job.state in {
                "completed",
                "partially_completed",
                "failed",
                "canceled",
            }:
                raise EngineExecutionNotFoundError("analysis is not executable")
            latest = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.team_id == team_id,
                    EngineExecution.engine_id == seed.engine_id,
                )
                .order_by(EngineExecution.attempt_number.desc())
                .limit(1)
            )
            if latest is not None:
                if (
                    latest.tenant_resource_version != seed.tenant_resource_version
                    or latest.adapter_version != seed.adapter_version
                    or latest.engine_commit_sha != seed.engine_commit_sha
                    or latest.engine_image_digest != seed.engine_image_digest
                    or latest.input_manifest_hash != seed.input_manifest_hash
                    or latest.config_hash != seed.config_hash
                ):
                    raise EngineExecutionOwnershipError("engine execution seed changed")
                return self._record(latest)
            row = EngineExecution(
                analysis_id=analysis_id,
                team_id=team_id,
                engine_id=seed.engine_id,
                attempt_number=1,
                tenant_resource_version=seed.tenant_resource_version,
                adapter_version=seed.adapter_version,
                engine_commit_sha=seed.engine_commit_sha,
                engine_image_digest=seed.engine_image_digest,
                input_manifest_hash=seed.input_manifest_hash,
                config_hash=seed.config_hash,
                state="pending",
                started_at=None,
                completed_at=None,
                version=1,
            )
            session.add(row)
            await session.flush()
            return self._record(row)

    async def get(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineExecutionRecord:
        async with self._session_factory() as session:
            return self._record(
                await self._execution(
                    session,
                    team_id=team_id,
                    analysis_id=analysis_id,
                    execution_id=execution_id,
                    for_update=False,
                )
            )

    async def mark_submitted(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        run_ref: EngineRunRef,
        now: datetime,
    ) -> EngineExecutionRecord:
        workspace_id, session_id, run_id = self._validate_submitted_ref(
            run_ref,
            execution_id=execution_id,
        )
        async with self._session_factory.begin() as session:
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if row.version != expected_version or row.state != "pending":
                raise StaleEngineExecutionVersionError("engine execution version is stale")
            if row.engine_id != run_ref.engine_id:
                raise EngineExecutionOwnershipError("engine route ownership changed")
            transition_engine_state(row.state, "running")
            row.external_workspace_id = workspace_id
            row.external_session_id = session_id
            row.external_run_id = run_id
            row.state = "running"
            row.started_at = row.started_at or now
            row.stable_error_code = None
            row.version += 1
            row.updated_at = now
            await session.flush()
            return self._record(row)

    @staticmethod
    def _validate_submitted_ref(
        run_ref: EngineRunRef,
        *,
        execution_id: UUID,
    ) -> tuple[str | None, str | None, str]:
        try:
            run_id = validate_external_id(run_ref.external_run_id or "")
            if run_ref.engine_id == "smartperfetto":
                workspace_id = validate_external_id(run_ref.external_workspace_id or "")
                session_id = validate_external_id(run_ref.external_session_id or "")
            elif run_ref.engine_id == "android_memory":
                if (
                    run_ref.external_workspace_id is not None
                    or run_ref.external_session_id is not None
                    or _MEMORY_RUN_ID.fullmatch(run_id) is None
                    or run_id != f"memory-{execution_id.hex}"
                ):
                    raise EngineExecutionOwnershipError("engine route ownership changed")
                workspace_id = None
                session_id = None
            else:
                raise EngineExecutionOwnershipError("engine route ownership changed")
        except EngineAdapterError:
            raise EngineExecutionOwnershipError("engine route ownership changed") from None
        return workspace_id, session_id, run_id

    async def persist_observation(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        run_ref: EngineRunRef,
        target_state: str,
        stable_error_code: str | None,
        now: datetime,
    ) -> EngineExecutionRecord:
        if target_state not in _OBSERVABLE_STATES:
            raise ValueError("terminal execution state requires a dedicated CAS")
        if stable_error_code is not None:
            _validate_stable_code(stable_error_code)
        new_cursor = _validate_cursor(run_ref.cursor)
        workspace_id, session_id, run_id = self._validate_submitted_ref(
            run_ref,
            execution_id=execution_id,
        )
        async with self._session_factory.begin() as session:
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if row.version != expected_version:
                raise StaleEngineExecutionVersionError("engine execution version is stale")
            if row.state in _TERMINAL_STATES:
                return self._record(row)
            if row.raw_result_artifact_id is not None:
                return self._record(row)
            if (
                row.engine_id != run_ref.engine_id
                or row.external_workspace_id != workspace_id
                or row.external_session_id != session_id
                or (row.engine_id == "android_memory" and row.external_run_id != run_id)
            ):
                raise EngineExecutionOwnershipError("engine route ownership changed")
            current_cursor = _validate_cursor(row.last_event_cursor)
            if (
                new_cursor is not None
                and current_cursor is not None
                and new_cursor < current_cursor
            ):
                raise ValueError("engine cursor must be monotonic")
            if row.state != target_state:
                transition_engine_state(row.state, target_state)
            changed = (
                row.external_run_id != run_id
                or (new_cursor is not None and new_cursor != current_cursor)
                or row.state != target_state
                or row.stable_error_code != stable_error_code
            )
            if not changed:
                return self._record(row)
            row.external_run_id = run_id
            if new_cursor is not None:
                row.last_event_cursor = str(new_cursor)
            row.state = target_state
            row.stable_error_code = stable_error_code
            row.version += 1
            row.updated_at = now
            await session.flush()
            return self._record(row)

    async def claim_finalization(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        now: datetime,
    ) -> FinalizationClaim:
        async with self._session_factory.begin() as session:
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if row.state != "running":
                return FinalizationClaim(self._record(row), False)
            artifact_id = result_artifact_id(row.id)
            if row.raw_result_artifact_id is not None:
                if row.raw_result_artifact_id != artifact_id:
                    raise EngineExecutionOwnershipError("result artifact ownership changed")
                return FinalizationClaim(self._record(row), False)
            row.raw_result_artifact_id = artifact_id
            row.version += 1
            row.updated_at = now
            await session.flush()
            return FinalizationClaim(self._record(row), True)

    async def finalize(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        artifact_id: UUID | None,
        terminal_state: Literal["completed", "insufficient_data"],
        schedule_synthesis: bool = False,
        now: datetime,
    ) -> EngineExecutionRecord:
        if artifact_id is None:
            raise ValueError("result artifact is required")
        async with self._session_factory.begin() as session:
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if schedule_synthesis and row.engine_id != "smartperfetto":
                raise ValueError("only SmartPerfetto executions can schedule synthesis")
            if schedule_synthesis:
                job = await self._job(
                    session,
                    team_id=team_id,
                    analysis_id=analysis_id,
                    for_update=True,
                )
                if job.analysis_mode not in {"trace_upload", "device"}:
                    raise ValueError(
                        "only SmartPerfetto analysis executions can schedule synthesis"
                    )
            if row.state in _TERMINAL_STATES:
                if (
                    row.raw_result_artifact_id != artifact_id
                    or row.state != terminal_state
                    or row.version != expected_version + 1
                    or row.stable_error_code is not None
                ):
                    raise EngineExecutionOwnershipError("engine finalization authority changed")
                if schedule_synthesis:
                    await self._ensure_result_ready_event(session, row=row, now=now)
                return self._record(row)
            if (
                row.version != expected_version
                or row.state != "running"
                or row.raw_result_artifact_id != artifact_id
            ):
                raise StaleEngineExecutionVersionError("engine execution version is stale")
            transition_engine_state(row.state, terminal_state)
            row.state = terminal_state
            row.stable_error_code = None
            row.completed_at = now
            row.version += 1
            row.updated_at = now
            if schedule_synthesis:
                await self._ensure_result_ready_event(session, row=row, now=now)
            await session.flush()
            return self._record(row)

    @staticmethod
    async def _ensure_result_ready_event(
        session: AsyncSession, *, row: EngineExecution, now: datetime
    ) -> None:
        if (
            row.engine_id != "smartperfetto"
            or row.raw_result_artifact_id is None
            or row.state not in {"completed", "insufficient_data"}
        ):
            raise EngineExecutionOwnershipError("synthesis result authority changed")
        event_id = engine_result_ready_event_id(row.id)
        event = await session.get(OutboxEvent, event_id)
        if event is None:
            session.add(OutboxEvent(
                id=event_id, team_id=row.team_id, global_job_id=row.analysis_id,
                scenario_job_id=None, event_type="engine_result_ready",
                subject_type="engine_execution", subject_id=row.id,
                subject_version=row.version, ready_at=now, published_at=None,
                dead_lettered_at=None, retry_count=0, version=1,
            ))
            await session.flush()
            return
        if (
            event.team_id != row.team_id or event.global_job_id != row.analysis_id
            or event.scenario_job_id is not None or event.event_type != "engine_result_ready"
            or event.subject_type != "engine_execution" or event.subject_id != row.id
            or event.subject_version != row.version
        ):
            raise EngineExecutionOwnershipError("synthesis event authority changed")

    async def fail(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        stable_error_code: str,
        now: datetime,
    ) -> EngineExecutionRecord:
        _validate_stable_code(stable_error_code)
        return await self._terminalize(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
            expected_version=expected_version,
            target_state="failed",
            stable_error_code=stable_error_code,
            now=now,
        )

    async def cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> EngineExecutionRecord:
        return await self._terminalize(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
            expected_version=expected_version,
            target_state="canceled",
            stable_error_code=None,
            now=now,
        )

    async def _terminalize(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        target_state: Literal["failed", "canceled"],
        stable_error_code: str | None,
        now: datetime,
    ) -> EngineExecutionRecord:
        async with self._session_factory.begin() as session:
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if row.state in _TERMINAL_STATES:
                return self._record(row)
            if row.version != expected_version:
                raise StaleEngineExecutionVersionError("engine execution version is stale")
            transition_engine_state(row.state, target_state)
            row.state = target_state
            row.stable_error_code = stable_error_code
            row.completed_at = now
            row.version += 1
            row.updated_at = now
            await session.flush()
            return self._record(row)

    async def record_retryable(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        expected_version: int,
        stable_error_code: str,
        now: datetime,
    ) -> EngineExecutionRecord:
        _validate_stable_code(stable_error_code)
        async with self._session_factory.begin() as session:
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if row.state in _TERMINAL_STATES:
                return self._record(row)
            if row.version != expected_version:
                raise StaleEngineExecutionVersionError("engine execution version is stale")
            if row.stable_error_code == stable_error_code:
                return self._record(row)
            row.stable_error_code = stable_error_code
            row.version += 1
            row.updated_at = now
            await session.flush()
            return self._record(row)

    async def reserve_retry(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        stable_error_code: str,
        now: datetime,
        deadline_seconds: int,
    ) -> RetryReservation:
        _validate_stable_code(stable_error_code)
        async with self._session_factory.begin() as session:
            job = await self._job(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                for_update=True,
            )
            row = await self._execution(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution_id,
                for_update=True,
            )
            if row.state == "failed" and row.stable_error_code == stable_error_code:
                existing = await session.scalar(
                    select(EngineExecution).where(
                        EngineExecution.analysis_id == analysis_id,
                        EngineExecution.team_id == team_id,
                        EngineExecution.engine_id == row.engine_id,
                        EngineExecution.attempt_number == row.attempt_number + 1,
                    )
                )
                return RetryReservation(
                    self._record(row),
                    None if existing is None else self._record(existing),
                )
            if row.state in _TERMINAL_STATES:
                return RetryReservation(self._record(row), None)

            origin = job.started_at or job.created_at
            expired = now >= origin + timedelta(seconds=deadline_seconds)
            can_retry = not expired and job.retry_count < job.max_retries
            transition_engine_state(row.state, "failed")
            row.state = "failed"
            row.stable_error_code = "engine_timeout" if expired else stable_error_code
            row.completed_at = now
            row.version += 1
            row.updated_at = now
            if not can_retry:
                await session.flush()
                return RetryReservation(self._record(row), None)

            job.retry_count += 1
            job.version += 1
            job.updated_at = now
            next_row = EngineExecution(
                analysis_id=row.analysis_id,
                team_id=row.team_id,
                engine_id=row.engine_id,
                attempt_number=row.attempt_number + 1,
                tenant_resource_version=row.tenant_resource_version,
                adapter_version=row.adapter_version,
                engine_commit_sha=row.engine_commit_sha,
                engine_image_digest=row.engine_image_digest,
                input_manifest_hash=row.input_manifest_hash,
                config_hash=row.config_hash,
                state="pending",
                version=1,
            )
            session.add(next_row)
            await session.flush()
            return RetryReservation(self._record(row), self._record(next_row))

    async def deadline_expired(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
        deadline_seconds: int,
    ) -> bool:
        async with self._session_factory() as session:
            job = await self._job(
                session,
                team_id=team_id,
                analysis_id=analysis_id,
                for_update=False,
            )
            return now >= (job.started_at or job.created_at) + timedelta(seconds=deadline_seconds)


class EngineExecutionService:
    def __init__(
        self,
        *,
        repository: EngineExecutionRepository,
        workspace_service: EngineWorkspaceService,
        registry: AdapterRegistry,
        engine_lock: EngineLock,
        result_sink: EngineResultSink,
        now: Callable[[], datetime],
        deadline_seconds: int = 1_800,
        reconnect_after_seconds: int = 5,
        schedule_synthesis: bool = False,
    ) -> None:
        if result_sink is None:
            raise ValueError("engine result sink is required")
        self._repository = repository
        self._workspace_service = workspace_service
        self._registry = registry
        self._engine_lock = engine_lock
        self._result_sink = result_sink
        self._now = now
        self._deadline_seconds = deadline_seconds
        self._reconnect_after_seconds = reconnect_after_seconds
        self._schedule_synthesis = schedule_synthesis

    def _pin(self, engine_id: str) -> EnginePin:
        if engine_id == "smartperfetto":
            return self._engine_lock.smartperfetto
        if engine_id == "android_memory":
            return self._engine_lock.android_memory
        raise ValueError("engine is not locked")

    async def create_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        engine_id: str,
        tenant_resource_version: int,
        input_manifest_hash: str,
        config_hash: str,
    ) -> EngineExecutionRecord:
        _validate_tenant_resource_version(tenant_resource_version)
        adapter = self._registry.require(engine_id)
        pin = self._pin(engine_id)
        if pin.image_digest is None:
            raise ValueError("engine image digest is required")
        return await self._repository.allocate_attempt(
            team_id=team_id,
            analysis_id=analysis_id,
            seed=EngineExecutionSeed(
                engine_id=engine_id,
                tenant_resource_version=tenant_resource_version,
                adapter_version=adapter.descriptor.adapter_version,
                engine_commit_sha=pin.commit,
                engine_image_digest=pin.image_digest,
                input_manifest_hash=input_manifest_hash,
                config_hash=config_hash,
            ),
            now=self._now(),
        )

    async def submit_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        inputs: tuple[EngineInput, ...],
        profile: AnalysisProfile,
        question: str | None,
        timeout_seconds: int,
    ) -> EngineExecutionRecord | EngineStepOutcome:
        record = await self._repository.get(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
        )
        adapter = self._registry.require(record.engine_id)
        external_workspace_id: str | None = None
        if adapter.descriptor.resource_profile == "network_service":
            workspace = await self._workspace_service.ensure_workspace(team_id=team_id)
            if workspace.external_workspace_id is None:
                raise EngineExecutionOwnershipError("workspace is not active")
            external_workspace_id = workspace.external_workspace_id
        try:
            run_ref = await adapter.submit(
                inputs,
                SubmitConfig(
                    execution_id=record.id,
                    analysis_id=analysis_id,
                    profile=profile,
                    question=question,
                    external_workspace_id=external_workspace_id,
                    timeout_seconds=timeout_seconds,
                ),
            )
        except EngineAdapterError as error:
            return await self._handle_adapter_error(record, error)
        return await self._repository.mark_submitted(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
            expected_version=record.version,
            run_ref=run_ref,
            now=self._now(),
        )

    async def step(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineStepOutcome:
        record = await self._repository.get(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
        )
        if record.state in _TERMINAL_STATES:
            return EngineStepOutcome(record.id, record.state, None)
        if await self._repository.deadline_expired(
            team_id=team_id,
            analysis_id=analysis_id,
            now=self._now(),
            deadline_seconds=self._deadline_seconds,
        ):
            failure_code = (
                "result_persistence_failed"
                if record.raw_result_artifact_id is not None
                else "engine_timeout"
            )
            failed = await self._repository.fail(
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=record.id,
                expected_version=record.version,
                stable_error_code=failure_code,
                now=self._now(),
            )
            return EngineStepOutcome(failed.id, failed.state, None)
        if record.raw_result_artifact_id is not None:
            return await self._finalize(record)

        adapter = self._registry.require(record.engine_id)
        run_ref = self._run_ref(record)
        try:
            batch = await adapter.stream(run_ref, record.last_event_cursor)
        except EngineAdapterError as error:
            return await self._handle_adapter_error(record, error)
        if not batch.events or batch.events[-1].message_code == "stream_end":
            try:
                status = await adapter.status(batch.run_ref)
            except EngineAdapterError as error:
                return await self._handle_adapter_error(record, error)
            return await self._handle_status(record, status)
        return await self._handle_batch(record, batch)

    async def _handle_batch(
        self,
        record: EngineExecutionRecord,
        batch: EngineEventBatch,
    ) -> EngineStepOutcome:
        last = batch.events[-1]
        if last.state == "completed":
            observed = await self._observe(record, batch.run_ref, "running", None)
            return await self._finalize(observed)
        if last.state == "awaiting_user":
            observed = await self._observe(
                record,
                batch.run_ref,
                "awaiting_user",
                "engine_interaction_required",
            )
            adapter = self._registry.require(record.engine_id)
            try:
                await adapter.cancel(batch.run_ref)
            except EngineAdapterError:
                pass
            failed = await self._repository.fail(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                expected_version=observed.version,
                stable_error_code="engine_interaction_required",
                now=self._now(),
            )
            return EngineStepOutcome(failed.id, failed.state, None)
        if last.state == "canceled":
            observed = await self._observe(record, batch.run_ref, "running", None)
            canceled = await self._repository.cancel(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                expected_version=observed.version,
                now=self._now(),
            )
            return EngineStepOutcome(canceled.id, canceled.state, None)
        if last.state == "failed":
            observed = await self._observe(record, batch.run_ref, "running", None)
            error = EngineAdapterError(
                stable_code=(
                    "capacity_exceeded"
                    if last.message_code == "capacity_exceeded"
                    else "engine_failed"
                ),
                retryable=last.message_code == "capacity_exceeded",
                terminal_state=None if last.message_code == "capacity_exceeded" else "failed",
            )
            return await self._handle_adapter_error(observed, error)
        observed = await self._observe(record, batch.run_ref, last.state, None)
        return EngineStepOutcome(observed.id, observed.state, None)

    async def _handle_status(
        self,
        record: EngineExecutionRecord,
        status: EngineStatus,
    ) -> EngineStepOutcome:
        if status.state in {"completed", "insufficient_data"}:
            observed = await self._observe(record, status.run_ref, "running", None)
            return await self._finalize(observed)
        if status.state == "awaiting_user":
            batch = EngineEventBatch(
                status.run_ref,
                (),
            )
            observed = await self._observe(
                record,
                batch.run_ref,
                "awaiting_user",
                "engine_interaction_required",
            )
            adapter = self._registry.require(record.engine_id)
            try:
                await adapter.cancel(status.run_ref)
            except EngineAdapterError:
                pass
            failed = await self._repository.fail(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                expected_version=observed.version,
                stable_error_code="engine_interaction_required",
                now=self._now(),
            )
            return EngineStepOutcome(failed.id, failed.state, None)
        if status.state == "canceled":
            observed = await self._observe(record, status.run_ref, "running", None)
            canceled = await self._repository.cancel(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                expected_version=observed.version,
                now=self._now(),
            )
            return EngineStepOutcome(canceled.id, canceled.state, None)
        if status.stable_error_code is not None:
            observed = await self._observe(record, status.run_ref, "running", None)
            return await self._handle_adapter_error(
                observed,
                EngineAdapterError(
                    stable_code=status.stable_error_code,
                    retryable=status.retryable,
                    terminal_state=None if status.retryable else "failed",
                ),
            )
        target_state = "running" if status.state == "pending" else status.state
        observed = await self._observe(record, status.run_ref, target_state, None)
        return EngineStepOutcome(observed.id, observed.state, None)

    async def _observe(
        self,
        record: EngineExecutionRecord,
        run_ref: EngineRunRef,
        state: str,
        stable_error_code: str | None,
    ) -> EngineExecutionRecord:
        return await self._repository.persist_observation(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            execution_id=record.id,
            expected_version=record.version,
            run_ref=run_ref,
            target_state=state,
            stable_error_code=stable_error_code,
            now=self._now(),
        )

    async def _finalize(self, record: EngineExecutionRecord) -> EngineStepOutcome:
        claim = await self._repository.claim_finalization(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            execution_id=record.id,
            now=self._now(),
        )
        claimed = claim.record
        if claimed.state != "running" or claimed.raw_result_artifact_id is None:
            return EngineStepOutcome(claimed.id, claimed.state, None)
        adapter = self._registry.require(claimed.engine_id)
        try:
            result = await adapter.fetch_result(self._run_ref(claimed))
        except EngineAdapterError as error:
            return await self._handle_adapter_error(claimed, error)
        write = EngineResultWrite(
            team_id=claimed.team_id,
            analysis_id=claimed.analysis_id,
            execution_id=claimed.id,
            expected_execution_version=claimed.version,
            tenant_resource_version=claimed.tenant_resource_version,
            artifact_id=claimed.raw_result_artifact_id,
            engine_id=claimed.engine_id,  # type: ignore[arg-type]
            adapter_version=claimed.adapter_version,
            engine_commit_sha=claimed.engine_commit_sha,
            engine_image_digest=claimed.engine_image_digest,
            attempt_number=claimed.attempt_number,
            input_manifest_hash=claimed.input_manifest_hash,
            config_hash=claimed.config_hash,
            result=result,
        )
        try:
            written_id = await self._result_sink.write(write)
            if written_id != claimed.raw_result_artifact_id:
                raise EngineResultConflictError
        except EngineResultValidationError:
            failed = await self._repository.fail(
                team_id=claimed.team_id,
                analysis_id=claimed.analysis_id,
                execution_id=claimed.id,
                expected_version=claimed.version,
                stable_error_code="invalid_output",
                now=self._now(),
            )
            return EngineStepOutcome(failed.id, failed.state, None)
        except EngineResultConflictError:
            failed = await self._repository.fail(
                team_id=claimed.team_id,
                analysis_id=claimed.analysis_id,
                execution_id=claimed.id,
                expected_version=claimed.version,
                stable_error_code="result_integrity_mismatch",
                now=self._now(),
            )
            return EngineStepOutcome(failed.id, failed.state, None)
        except Exception:
            if await self._repository.deadline_expired(
                team_id=claimed.team_id,
                analysis_id=claimed.analysis_id,
                now=self._now(),
                deadline_seconds=self._deadline_seconds,
            ):
                failed = await self._repository.fail(
                    team_id=claimed.team_id,
                    analysis_id=claimed.analysis_id,
                    execution_id=claimed.id,
                    expected_version=claimed.version,
                    stable_error_code="result_persistence_failed",
                    now=self._now(),
                )
                return EngineStepOutcome(failed.id, failed.state, None)
            retained = await self._repository.record_retryable(
                team_id=claimed.team_id,
                analysis_id=claimed.analysis_id,
                execution_id=claimed.id,
                expected_version=claimed.version,
                stable_error_code="result_persistence_failed",
                now=self._now(),
            )
            return self._reconnect(retained, "result_persistence_failed")
        completed = await self._repository.finalize(
            team_id=claimed.team_id,
            analysis_id=claimed.analysis_id,
            execution_id=claimed.id,
            expected_version=claimed.version,
            artifact_id=claimed.raw_result_artifact_id,
            terminal_state=result.state,
            schedule_synthesis=self._schedule_synthesis and claimed.engine_id == "smartperfetto",
            now=self._now(),
        )
        return EngineStepOutcome(completed.id, completed.state, None)

    async def _handle_adapter_error(
        self,
        record: EngineExecutionRecord,
        error: EngineAdapterError,
    ) -> EngineStepOutcome:
        if error.stable_code in _TERMINAL_ADAPTER_CODES:
            failed = await self._repository.fail(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                expected_version=record.version,
                stable_error_code=error.stable_code,
                now=self._now(),
            )
            return EngineStepOutcome(failed.id, failed.state, None)
        if error.stable_code in _NEW_ATTEMPT_CODES:
            reservation = await self._repository.reserve_retry(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                stable_error_code=error.stable_code,
                now=self._now(),
                deadline_seconds=self._deadline_seconds,
            )
            if reservation.next_attempt is None:
                return EngineStepOutcome(
                    reservation.current_attempt.id,
                    reservation.current_attempt.state,
                    None,
                )
            next_attempt = reservation.next_attempt
            return EngineStepOutcome(
                next_attempt.id,
                next_attempt.state,
                EngineRetryDirective(
                    mode="new_attempt",
                    execution_id=next_attempt.id,
                    attempt_number=next_attempt.attempt_number,
                    stable_error_code=error.stable_code,
                    retry_after_seconds=self._reconnect_after_seconds,
                ),
            )
        if error.retryable:
            retained = await self._repository.record_retryable(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                expected_version=record.version,
                stable_error_code=error.stable_code,
                now=self._now(),
            )
            return self._reconnect(retained, error.stable_code)
        failed = await self._repository.fail(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            execution_id=record.id,
            expected_version=record.version,
            stable_error_code=error.stable_code,
            now=self._now(),
        )
        return EngineStepOutcome(failed.id, failed.state, None)

    def _reconnect(
        self,
        record: EngineExecutionRecord,
        stable_error_code: str,
    ) -> EngineStepOutcome:
        return EngineStepOutcome(
            record.id,
            record.state,
            EngineRetryDirective(
                mode="reconnect",
                execution_id=record.id,
                attempt_number=record.attempt_number,
                stable_error_code=stable_error_code,
                retry_after_seconds=self._reconnect_after_seconds,
            ),
        )

    @staticmethod
    def _run_ref(record: EngineExecutionRecord) -> EngineRunRef:
        return EngineRunRef(
            engine_id=record.engine_id,
            external_session_id=record.external_session_id,
            external_run_id=record.external_run_id,
            cursor=record.last_event_cursor,
            external_workspace_id=record.external_workspace_id,
        )

    async def cancel_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineStepOutcome:
        record = await self._repository.get(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
        )
        if record.state in _TERMINAL_STATES:
            return EngineStepOutcome(record.id, record.state, None)
        adapter = self._registry.require(record.engine_id)
        has_submitted_route = record.external_run_id is not None and (
            (
                adapter.descriptor.resource_profile == "network_service"
                and record.external_workspace_id is not None
                and record.external_session_id is not None
            )
            or (
                adapter.descriptor.resource_profile == "isolated_worker"
                and record.external_workspace_id is None
                and record.external_session_id is None
            )
        )
        if has_submitted_route:
            try:
                await adapter.cancel(self._run_ref(record))
            except EngineAdapterError as error:
                if error.retryable:
                    return self._reconnect(record, error.stable_code)
        canceled = await self._repository.cancel(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
            expected_version=record.version,
            now=self._now(),
        )
        return EngineStepOutcome(canceled.id, canceled.state, None)


def build_smartperfetto_execution_service(
    *,
    settings: Settings,
    control_session_factory: async_sessionmaker[AsyncSession],
    credential_resolver: CredentialResolver,
    engine_client: httpx.AsyncClient,
    artifact_client: httpx.AsyncClient,
    engine_lock: EngineLock,
    result_sink: EngineResultSink,
    now: Callable[[], datetime] | None = None,
) -> EngineExecutionService:
    if result_sink is None:
        raise ValueError("engine result sink is required")
    timeout = httpx.Timeout(
        timeout=settings.smartperfetto_read_timeout_seconds,
        connect=settings.smartperfetto_connect_timeout_seconds,
        read=settings.smartperfetto_read_timeout_seconds,
        write=settings.smartperfetto_write_timeout_seconds,
        pool=settings.smartperfetto_pool_timeout_seconds,
    )
    transport = SmartPerfettoTransport(
        base_url=settings.smartperfetto_base_url.get_secret_value(),
        credential_reference=settings.smartperfetto_credential_reference,
        credential_resolver=credential_resolver,
        max_json_bytes=settings.smartperfetto_max_json_bytes,
        client=engine_client,
        timeout=timeout,
    )
    adapter = SmartPerfettoAdapter(
        transport=transport,
        artifact_client=artifact_client,
        max_trace_bytes=settings.smartperfetto_max_trace_bytes,
        max_timeout_seconds=SmartPerfettoAdapter.descriptor.default_timeout_seconds,
        max_sse_event_bytes=settings.smartperfetto_max_sse_event_bytes,
        max_sse_batch_events=settings.smartperfetto_sse_batch_events,
        max_sse_batch_seconds=settings.smartperfetto_sse_batch_seconds,
    )
    workspace_service = EngineWorkspaceService(
        SQLAlchemyEngineWorkspaceRepository(control_session_factory),
        transport,
    )
    return build_engine_execution_service(
        control_session_factory=control_session_factory,
        workspace_service=workspace_service,
        adapters=(adapter,),
        engine_lock=engine_lock,
        result_sink=result_sink,
        now=now or (lambda: datetime.now(UTC)),
        schedule_synthesis=settings.ai_enabled,
    )


def build_engine_execution_service(
    *,
    control_session_factory: async_sessionmaker[AsyncSession],
    workspace_service: EngineWorkspaceService,
    adapters: Iterable[EngineAdapter | None],
    engine_lock: EngineLock,
    result_sink: EngineResultSink,
    now: Callable[[], datetime] | None = None,
    schedule_synthesis: bool = False,
) -> EngineExecutionService:
    """Compose execution orchestration from only explicitly supplied adapters."""

    if result_sink is None:
        raise ValueError("engine result sink is required")
    return EngineExecutionService(
        repository=SQLAlchemyEngineExecutionRepository(control_session_factory),
        workspace_service=workspace_service,
        registry=AdapterRegistry(adapter for adapter in adapters if adapter is not None),
        engine_lock=engine_lock,
        result_sink=result_sink,
        now=now or (lambda: datetime.now(UTC)),
        schedule_synthesis=schedule_synthesis,
    )


__all__ = [
    "EngineExecutionNotFoundError",
    "EngineExecutionOwnershipError",
    "EngineExecutionRecord",
    "EngineExecutionRepository",
    "EngineExecutionSeed",
    "EngineExecutionService",
    "EngineResultSink",
    "FinalizationClaim",
    "RetryReservation",
    "SQLAlchemyEngineExecutionRepository",
    "StaleEngineExecutionVersionError",
    "build_engine_execution_service",
    "build_smartperfetto_execution_service",
    "engine_result_ready_event_id",
    "result_artifact_id",
]
