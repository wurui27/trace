"""Control-only coordinator and renewable work claims for AI synthesis."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.ai.openai_compatible import AIProviderError, SynthesisCandidate
from perfpilot_api.ai.synthesis import SynthesisValidationError, validate_synthesis_output
from perfpilot_api.db.control.models import (
    EngineExecution,
    GlobalJob,
    OutboxEvent,
    ScenarioJob,
    SynthesisExecution,
    WorkerClaim,
)
from perfpilot_api.db.tenant.models import Analysis, Artifact, ReportVersion, ScenarioResult
from perfpilot_api.db.tenant.router import TenantRouteError, TenantRouter
from perfpilot_api.domain.states import AnalysisState
from perfpilot_api.domain.transitions import (
    InvalidSynthesisProjection,
    remediate_failed_synthesis,
    transition,
)
from perfpilot_api.reports.contracts import validate_contract
from perfpilot_api.reports.memory_join import (
    AndroidMemoryNormalizationError,
    MemoryUnavailableReason,
    join_android_memory_result,
    join_unavailable_android_memory,
)
from perfpilot_api.reports.normalizer import (
    NormalizedTraceReport,
    SmartPerfettoNormalizationError,
    normalize_smartperfetto_result,
)
from perfpilot_api.reports.privacy import ProjectionPrivacyError
from perfpilot_api.reports.projection import (
    AIProjection,
    ProjectionQuestionError,
    ProjectionSizeError,
    build_ai_projection,
)
from perfpilot_api.reports.writer import (
    AnalysisReportWriteRequest,
    AnalysisReportWriter,
    ReportIntegrityError,
    ReportSourceError,
    report_version_id,
)
from perfpilot_api.services.canonical_result_reader import (
    CanonicalResultIntegrityError,
    CanonicalResultUnavailableError,
)
from perfpilot_api.services.synthesis_artifacts import (
    SynthesisArtifactConflictError,
    SynthesisArtifactUnavailableError,
    SynthesisArtifactWrite,
    projection_artifact_id,
    synthesis_artifact_id,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
    SynthesisExecutionNotFoundError,
    SynthesisExecutionRecord,
    SynthesisIdempotencyConflictError,
    SynthesisLeaseLostError,
    SynthesisMutationFence,
    SynthesisRequest,
    SynthesisSourceRecord,
)


_WORKER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_EVENT_NAMESPACE = UUID("9bf739f1-eafc-5ba6-a95b-09fe18c4c315")
_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_ENGINE_TERMINAL_STATES = frozenset(
    {"completed", "insufficient_data", "failed", "canceled"}
)
_MEMORY_SCENARIO_ACTIVE_STATES = frozenset(
    {"queued", "scheduled", "running", "analyzing"}
)


def analysis_synthesis_requested_event_id(execution_id: UUID) -> UUID:
    return uuid5(_EVENT_NAMESPACE, f"analysis_synthesis_requested:{execution_id}")


class SynthesisClaimLostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesisWorkClaim:
    claim_id: UUID
    event_id: UUID
    team_id: UUID
    analysis_id: UUID
    synthesis_execution_id: UUID
    consumer_id: str
    token: SecretStr = field(repr=False)
    expires_at: datetime


class SQLAlchemyAutomaticSynthesisRequestFactory:
    """Builds generation-one metadata from the authoritative tenant database."""

    def __init__(
        self,
        *,
        tenant_router: TenantRouter,
        normalizer_version: str,
        prompt_template_version: str,
        prompt_template_sha256_b64: str,
        report_worker_image_digest: str,
        provider_name: str,
        model: str,
        inference_config_hash: str,
    ) -> None:
        self._tenant_router = tenant_router
        self._normalizer_version = normalizer_version
        self._prompt_template_version = prompt_template_version
        self._prompt_template_sha256_b64 = prompt_template_sha256_b64
        self._report_worker_image_digest = report_worker_image_digest
        self._provider_name = provider_name
        self._model = model
        self._inference_config_hash = inference_config_hash

    async def __call__(
        self, source: EngineExecution, generation: int
    ) -> SynthesisRequest:
        if source.raw_result_artifact_id is None:
            raise SynthesisClaimLostError("source artifact authority changed")
        async with self._tenant_router.session(source.team_id) as session:
            analysis = await session.get(Analysis, source.analysis_id)
            artifact = await session.get(Artifact, source.raw_result_artifact_id)
            if (
                session.info.get("tenant_resource_version")
                != source.tenant_resource_version
                or analysis is None
                or analysis.analysis_mode not in {"trace_upload", "device"}
                or analysis.tombstoned_at is not None
                or artifact is None
                or artifact.analysis_id != source.analysis_id
                or artifact.artifact_kind != "engine_result"
                or artifact.state != "finalized"
                or artifact.version_id is None
                or artifact.deleted_at is not None
                or not isinstance(artifact.sha256_b64, str)
            ):
                raise SynthesisClaimLostError("source artifact authority changed")
            question = analysis.question
            checksum = artifact.sha256_b64
        return SynthesisRequest(
            canonical_sha256_b64=checksum,
            tenant_resource_version=source.tenant_resource_version,
            question=question,
            normalizer_version=self._normalizer_version,
            prompt_template_version=self._prompt_template_version,
            prompt_template_sha256_b64=self._prompt_template_sha256_b64,
            report_worker_image_digest=self._report_worker_image_digest,
            provider_name=self._provider_name,
            model=self._model,
            inference_config_hash=self._inference_config_hash,
            # The worker replaces this placeholder with the content-derived value.
            projection_sha256_b64=checksum,
            generation=generation,
        )


class SynthesisCoordinator:
    """Turns verified source-result events into automatic generation one.

    `request_factory` receives metadata only; canonical artifact bytes are deliberately
    unavailable to this component.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        repository: SQLAlchemySynthesisExecutionRepository,
        request_factory: Callable[
            [EngineExecution, int], SynthesisRequest | Awaitable[SynthesisRequest]
        ],
        source_gate: Callable[[UUID], bool | Awaitable[bool]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retry_backoff_seconds: float = 5,
    ) -> None:
        if (
            isinstance(retry_backoff_seconds, bool)
            or not isinstance(retry_backoff_seconds, (int, float))
            or not 0 < retry_backoff_seconds <= 3600
        ):
            raise ValueError("synthesis coordinator backoff is invalid")
        self._sessions = session_factory
        self._repository = repository
        self._request_factory = request_factory
        self._source_gate = source_gate
        self._clock = clock
        self._retry_backoff = timedelta(seconds=retry_backoff_seconds)

    @staticmethod
    def _settle_source_event(
        event: OutboxEvent,
        *,
        now: datetime,
        dead_letter: bool,
    ) -> None:
        if dead_letter:
            event.dead_lettered_at = now
        else:
            event.published_at = now
        event.version += 1
        event.updated_at = now

    async def _dead_letter_source_event(self, event_id: UUID, now: datetime) -> None:
        async with self._sessions.begin() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.event_type == "engine_result_ready",
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.dead_lettered_at.is_(None),
                )
                .with_for_update()
            )
            if event is not None:
                self._settle_source_event(event, now=now, dead_letter=True)

    async def _reschedule_source_event(self, event_id: UUID, now: datetime) -> None:
        async with self._sessions.begin() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.event_type == "engine_result_ready",
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.dead_lettered_at.is_(None),
                )
                .with_for_update()
            )
            if event is not None:
                event.ready_at = now + self._retry_backoff
                event.retry_count += 1
                event.version += 1
                event.updated_at = now

    def _reschedule_locked_source_event(
        self,
        event: OutboxEvent,
        *,
        now: datetime,
    ) -> None:
        event.ready_at = now + self._retry_backoff
        event.retry_count += 1
        event.version += 1
        event.updated_at = now

    async def coordinate_next(self) -> SynthesisExecutionRecord | None:
        now = self._clock()
        async with self._sessions.begin() as session:
            event = await session.scalar(select(OutboxEvent).where(
                OutboxEvent.event_type == "engine_result_ready", OutboxEvent.subject_type == "engine_execution",
                OutboxEvent.ready_at.is_not(None), OutboxEvent.ready_at <= now, OutboxEvent.published_at.is_(None),
                OutboxEvent.dead_lettered_at.is_(None)).order_by(OutboxEvent.ready_at, OutboxEvent.id).with_for_update(skip_locked=True).limit(1))
            if event is None:
                return None
            source = await session.scalar(select(EngineExecution).where(
                EngineExecution.id == event.subject_id, EngineExecution.team_id == event.team_id,
                EngineExecution.analysis_id == event.global_job_id).with_for_update())
            if (source is None or source.engine_id != "smartperfetto" or source.state not in {"completed", "insufficient_data"}
                    or source.raw_result_artifact_id is None or event.subject_version != source.version):
                self._settle_source_event(event, now=now, dead_letter=True)
                return None
            latest = await session.scalar(select(EngineExecution.id).where(
                EngineExecution.team_id == source.team_id, EngineExecution.analysis_id == source.analysis_id,
                EngineExecution.engine_id == "smartperfetto").order_by(EngineExecution.attempt_number.desc()).limit(1))
            if latest != source.id:
                self._settle_source_event(event, now=now, dead_letter=False)
                return None
            analysis_mode = await session.scalar(
                select(GlobalJob.analysis_mode).where(
                    GlobalJob.id == source.analysis_id,
                    GlobalJob.team_id == source.team_id,
                )
            )
            if analysis_mode is None:
                self._settle_source_event(event, now=now, dead_letter=True)
                return None
            if analysis_mode == "device":
                memory_scenario_state = await session.scalar(
                    select(ScenarioJob.state).where(
                        ScenarioJob.analysis_id == source.analysis_id,
                        ScenarioJob.scenario_type == "memory_cycle",
                    )
                )
                if memory_scenario_state is None:
                    self._settle_source_event(event, now=now, dead_letter=True)
                    return None
                latest_memory_state = await session.scalar(
                    select(EngineExecution.state)
                    .where(
                        EngineExecution.team_id == source.team_id,
                        EngineExecution.analysis_id == source.analysis_id,
                        EngineExecution.engine_id == "android_memory",
                    )
                    .order_by(EngineExecution.attempt_number.desc())
                    .limit(1)
                )
                if (
                    memory_scenario_state in _MEMORY_SCENARIO_ACTIVE_STATES
                    and latest_memory_state not in _ENGINE_TERMINAL_STATES
                ):
                    self._reschedule_locked_source_event(event, now=now)
                    return None
            existing_execution_id = await session.scalar(
                select(SynthesisExecution.id).where(
                    SynthesisExecution.team_id == source.team_id,
                    SynthesisExecution.analysis_id == source.analysis_id,
                    SynthesisExecution.source_execution_id == source.id,
                    SynthesisExecution.generation == 1,
                )
            )
            # Close the control transaction before repository allocation to retain one
            # lock authority; retries reload by unique source/generation.
            team_id, analysis_id, source_id, source_event_id = source.team_id, source.analysis_id, source.id, event.id
        if self._source_gate is not None:
            try:
                source_ready = self._source_gate(analysis_id)
                if inspect.isawaitable(source_ready):
                    source_ready = await source_ready
            except Exception:
                await self._reschedule_source_event(source_event_id, now)
                return None
            if not source_ready:
                await self._reschedule_source_event(source_event_id, now)
                return None
        if existing_execution_id is not None:
            try:
                record = await self._repository.load(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    execution_id=existing_execution_id,
                )
            except SynthesisExecutionNotFoundError:
                await self._dead_letter_source_event(source_event_id, now)
                return None
        else:
            try:
                request = self._request_factory(source, 1)
                if inspect.isawaitable(request):
                    request = await request
                record = await self._repository.allocate(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    source_execution_id=source_id,
                    request=request,
                    now=now,
                    mode="auto",
                )
            except TenantRouteError:
                await self._reschedule_source_event(source_event_id, now)
                return None
            except SynthesisIdempotencyConflictError:
                record = await self._repository.load_generation(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    source_execution_id=source_id,
                    generation=1,
                )
                if record is None:
                    await self._dead_letter_source_event(source_event_id, now)
                    return None
            except (SynthesisClaimLostError, SynthesisExecutionNotFoundError):
                await self._dead_letter_source_event(source_event_id, now)
                return None
        async with self._sessions.begin() as session:
            event = await session.get(OutboxEvent, source_event_id)
            if event is None:
                raise SynthesisClaimLostError("source event disappeared")
            if event.dead_lettered_at is not None:
                event.dead_lettered_at = None
            requested_id = analysis_synthesis_requested_event_id(record.id)
            await session.execute(
                postgresql_insert(OutboxEvent)
                .values(
                    id=requested_id,
                    team_id=record.team_id,
                    global_job_id=record.analysis_id,
                    scenario_job_id=None,
                    event_type="analysis_synthesis_requested",
                    subject_type="synthesis_execution",
                    subject_id=record.id,
                    subject_version=record.version,
                    ready_at=now,
                    published_at=None,
                    dead_lettered_at=None,
                    retry_count=0,
                    version=1,
                )
                .on_conflict_do_nothing()
            )
            requested = await session.get(OutboxEvent, requested_id)
            if (requested is None or requested.team_id != record.team_id or requested.global_job_id != record.analysis_id
                  or requested.subject_id != record.id or requested.subject_version > record.version):
                raise SynthesisClaimLostError("synthesis event authority changed")
            event.published_at, event.version, event.updated_at = event.published_at or now, event.version + 1, now
        return record


class SQLAlchemySynthesisWorkQueue:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, lease_seconds: int = 60, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("synthesis lease is invalid")
        self._sessions, self._lease, self._clock = session_factory, timedelta(seconds=lease_seconds), clock

    @staticmethod
    def _digest(token: SecretStr) -> str:
        return hashlib.sha256(token.get_secret_value().encode()).hexdigest()

    async def claim_next(self, *, consumer_id: str) -> SynthesisWorkClaim | None:
        if _WORKER.fullmatch(consumer_id) is None:
            raise ValueError("synthesis worker identity is invalid")
        now = self._clock()
        async with self._sessions.begin() as session:
            ids = select(OutboxEvent.id).where(OutboxEvent.event_type == "analysis_synthesis_requested")
            await session.execute(update(WorkerClaim).where(WorkerClaim.event_id.in_(ids), WorkerClaim.state == "active", WorkerClaim.expires_at <= now).values(state="expired", version=WorkerClaim.version + 1, updated_at=now))
            active = select(WorkerClaim.id).where(WorkerClaim.global_job_id == OutboxEvent.global_job_id, WorkerClaim.state == "active").exists()
            pair = (await session.execute(select(OutboxEvent, SynthesisExecution).join(SynthesisExecution, SynthesisExecution.id == OutboxEvent.subject_id).where(
                OutboxEvent.event_type == "analysis_synthesis_requested", OutboxEvent.subject_type == "synthesis_execution",
                OutboxEvent.ready_at.is_not(None), OutboxEvent.ready_at <= now, OutboxEvent.published_at.is_(None),
                OutboxEvent.dead_lettered_at.is_(None), SynthesisExecution.state.in_(("pending", "running", "succeeded", "failed", "canceled")),
                SynthesisExecution.team_id == OutboxEvent.team_id, SynthesisExecution.analysis_id == OutboxEvent.global_job_id,
                OutboxEvent.subject_version <= SynthesisExecution.version, ~active).order_by(OutboxEvent.ready_at, OutboxEvent.id).with_for_update(of=OutboxEvent, skip_locked=True).limit(1))).first()
            if pair is None:
                return None
            event, synthesis = pair
            token = SecretStr(secrets.token_urlsafe(32))
            claim_id = uuid4()
            expires = now + self._lease
            session.add(WorkerClaim(id=claim_id, global_job_id=synthesis.analysis_id, scenario_job_id=None, event_id=event.id,
                consumer_id=consumer_id, token_digest=self._digest(token), state="active", expires_at=expires,
                completed_at=None, retry_count=event.retry_count, report_id=None, version=1))
            return SynthesisWorkClaim(claim_id, event.id, synthesis.team_id, synthesis.analysis_id, synthesis.id, consumer_id, token, expires)

    async def _owned(
        self,
        session: AsyncSession,
        claim: SynthesisWorkClaim,
        *,
        allow_terminal: bool = False,
    ) -> tuple[WorkerClaim, OutboxEvent]:
        now = self._clock()
        row = await session.scalar(select(WorkerClaim).where(WorkerClaim.id == claim.claim_id, WorkerClaim.event_id == claim.event_id, WorkerClaim.global_job_id == claim.analysis_id, WorkerClaim.consumer_id == claim.consumer_id).with_for_update())
        event = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == claim.event_id, OutboxEvent.team_id == claim.team_id, OutboxEvent.global_job_id == claim.analysis_id).with_for_update())
        synthesis = await session.scalar(select(SynthesisExecution).where(
            SynthesisExecution.id == claim.synthesis_execution_id,
            SynthesisExecution.team_id == claim.team_id,
            SynthesisExecution.analysis_id == claim.analysis_id,
        ).with_for_update())
        if (row is None or event is None or row.state != "active" or row.expires_at <= now or not hmac.compare_digest(row.token_digest, self._digest(claim.token))
                or event.event_type != "analysis_synthesis_requested" or event.subject_type != "synthesis_execution" or event.subject_id != claim.synthesis_execution_id
                or event.published_at is not None or event.dead_lettered_at is not None):
            raise SynthesisClaimLostError("synthesis claim was lost")
        if synthesis is None or event.subject_version > synthesis.version or (
            not allow_terminal and synthesis.state not in {"pending", "running"}
        ):
            raise SynthesisClaimLostError("synthesis work authority was lost")
        return row, event

    async def renew(self, claim: SynthesisWorkClaim) -> None:
        async with self._sessions.begin() as session:
            row, _ = await self._owned(session, claim, allow_terminal=True)
            row.expires_at = self._clock() + self._lease
            row.version += 1

    async def complete(self, claim: SynthesisWorkClaim) -> None:
        now = self._clock()
        async with self._sessions.begin() as session:
            row, event = await self._owned(session, claim, allow_terminal=True)
            row.state, row.completed_at, row.version, row.updated_at = "completed", now, row.version + 1, now
            synthesis = await session.get(SynthesisExecution, claim.synthesis_execution_id)
            row.report_id = synthesis.report_version_id if synthesis is not None else None
            event.published_at, event.version, event.updated_at = now, event.version + 1, now

    @staticmethod
    def _delay(value: float) -> timedelta:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
            raise ValueError("synthesis queue delay is invalid")
        return timedelta(seconds=value)

    async def _reschedule(
        self,
        claim: SynthesisWorkClaim,
        *,
        delay_seconds: float,
        retry: bool,
    ) -> None:
        now = self._clock()
        async with self._sessions.begin() as session:
            # A pipeline boundary may make the synthesis execution terminal before
            # the following pass projects that terminal state to the parent job.
            # Keep the durable event runnable across that reconciliation boundary.
            row, event = await self._owned(session, claim, allow_terminal=True)
            row.state = "expired" if retry else "completed"
            row.completed_at = None if retry else now
            row.version += 1
            row.updated_at = now
            event.ready_at = now + self._delay(delay_seconds)
            event.version += 1
            event.updated_at = now
            if retry:
                event.retry_count += 1

    async def reschedule(
        self, claim: SynthesisWorkClaim, *, delay_seconds: float
    ) -> None:
        await self._reschedule(claim, delay_seconds=delay_seconds, retry=False)

    async def retry(
        self, claim: SynthesisWorkClaim, *, delay_seconds: float
    ) -> None:
        await self._reschedule(claim, delay_seconds=delay_seconds, retry=True)


@dataclass(frozen=True, slots=True)
class SynthesisAnalysisContext:
    analysis_profile: Literal["auto", "startup", "scroll"]
    question: str | None
    analysis_mode: Literal["trace_upload", "device"]


class SQLAlchemySynthesisAnalysisContextRepository:
    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    async def load(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        tenant_resource_version: int,
    ) -> SynthesisAnalysisContext:
        async with self._tenant_router.session(team_id) as session:
            if session.info.get("tenant_resource_version") != tenant_resource_version:
                raise SynthesisArtifactUnavailableError
            row = await session.get(Analysis, analysis_id)
            if (
                row is None
                or row.analysis_mode not in {"trace_upload", "device"}
                or row.analysis_mode == "trace_upload"
                and row.analysis_profile not in {"auto", "startup", "scroll"}
                or row.analysis_mode == "device"
                and (row.analysis_profile is not None or row.question is not None)
                or row.tombstoned_at is not None
            ):
                raise SynthesisArtifactConflictError
            return SynthesisAnalysisContext(
                analysis_profile=(
                    row.analysis_profile if row.analysis_mode == "trace_upload" else "auto"
                ),  # type: ignore[arg-type]
                question=row.question,
                analysis_mode=row.analysis_mode,  # type: ignore[arg-type]
            )


@dataclass(frozen=True, slots=True)
class SynthesisMemorySourceContext:
    scenario_state: Literal[
        "queued",
        "scheduled",
        "running",
        "analyzing",
        "completed",
        "failed",
        "canceled",
    ]
    execution: SynthesisSourceRecord | None


class SQLAlchemySynthesisMemorySourceRepository:
    """Resolve the latest Android Memory execution from control authority."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def load(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        tenant_resource_version: int,
    ) -> SynthesisMemorySourceContext:
        async with self._sessions() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                    GlobalJob.analysis_mode == "device",
                )
            )
            scenario = await session.scalar(
                select(ScenarioJob).where(
                    ScenarioJob.analysis_id == analysis_id,
                    ScenarioJob.scenario_type == "memory_cycle",
                )
            )
            execution = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.team_id == team_id,
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.engine_id == "android_memory",
                )
                .order_by(EngineExecution.attempt_number.desc())
                .limit(1)
            )
            if (
                job is None
                or scenario is None
                or scenario.state
                not in {
                    "queued",
                    "scheduled",
                    "running",
                    "analyzing",
                    "completed",
                    "failed",
                    "canceled",
                }
                or type(tenant_resource_version) is not int
                or tenant_resource_version < 1
                or execution is not None
                and execution.tenant_resource_version != tenant_resource_version
            ):
                raise SynthesisArtifactConflictError
            record = (
                None
                if execution is None
                else SynthesisSourceRecord(
                    id=execution.id,
                    team_id=execution.team_id,
                    analysis_id=execution.analysis_id,
                    engine_id=execution.engine_id,
                    attempt_number=execution.attempt_number,
                    tenant_resource_version=execution.tenant_resource_version,
                    adapter_version=execution.adapter_version,
                    engine_commit_sha=execution.engine_commit_sha,
                    engine_image_digest=execution.engine_image_digest,
                    state=execution.state,
                    raw_result_artifact_id=execution.raw_result_artifact_id,
                    normalized_report_version_id=execution.normalized_report_version_id,
                    version=execution.version,
                )
            )
            return SynthesisMemorySourceContext(
                scenario_state=scenario.state,  # type: ignore[arg-type]
                execution=record,
            )


class SQLAlchemySynthesisParentProjector:
    """Project a published report to tenant and control parents with remediation fencing."""

    def __init__(
        self,
        *,
        control_session_factory: async_sessionmaker[AsyncSession],
        tenant_router: TenantRouter,
    ) -> None:
        self._control_sessions = control_session_factory
        self._tenant_router = tenant_router

    @staticmethod
    def _target_from_report(report: Mapping[str, object]) -> str:
        validated = validate_contract("analysis-report", report)
        target = validated.get("state")
        if target not in {"completed", "partially_completed", "failed", "canceled"}:
            raise InvalidSynthesisProjection("unknown report state")
        return target

    @staticmethod
    def _device_scenario_targets(
        report: Mapping[str, object],
    ) -> dict[str, tuple[str, str | None]]:
        validated = validate_contract("analysis-report", report)
        if validated.get("analysis_mode") != "device":
            raise InvalidSynthesisProjection("device scenario projection is invalid")
        targets: dict[str, tuple[str, str | None]] = {
            "cold_start": ("failed", "scenario_evidence_unavailable"),
            "scroll": ("failed", "scenario_evidence_unavailable"),
            "memory_cycle": ("failed", "scenario_evidence_unavailable"),
        }
        scenarios = validated.get("scenario_reports")
        if not isinstance(scenarios, list):
            raise InvalidSynthesisProjection("device scenario projection is invalid")
        observed: set[str] = set()
        for item in scenarios:
            if not isinstance(item, Mapping):
                raise InvalidSynthesisProjection("device scenario projection is invalid")
            source_type = item.get("scenario_type")
            scenario_type = "cold_start" if source_type == "startup" else source_type
            if scenario_type not in targets or scenario_type in observed:
                raise InvalidSynthesisProjection("device scenario projection is invalid")
            observed.add(scenario_type)
            result_state = item.get("result_state")
            if result_state == "completed":
                targets[scenario_type] = ("completed", None)
            elif result_state == "canceled":
                targets[scenario_type] = ("canceled", None)
            elif result_state == "failed":
                failure = item.get("failure")
                code = failure.get("code") if isinstance(failure, Mapping) else None
                targets[scenario_type] = (
                    "failed",
                    code
                    if isinstance(code, str) and _FAILURE_CODE.fullmatch(code)
                    else "scenario_evidence_unavailable",
                )
            else:
                raise InvalidSynthesisProjection("device scenario projection is invalid")
        return targets

    @staticmethod
    def _project_device_scenarios(
        rows: list[ScenarioJob] | list[ScenarioResult],
        *,
        targets: Mapping[str, tuple[str, str | None]],
        now: datetime,
    ) -> None:
        by_type = {row.scenario_type: row for row in rows}
        if len(by_type) != len(rows) or set(by_type) != set(targets):
            raise InvalidSynthesisProjection("device scenario projection is invalid")
        for scenario_type, row in by_type.items():
            state, failure_code = targets[scenario_type]
            if row.state == state and row.failure_code == failure_code:
                continue
            row.state = state
            row.failure_code = failure_code
            row.completed_at = now
            row.version += 1
            row.updated_at = now

    @staticmethod
    def _values(
        *, current_started_at: datetime | None, target: str, failure_code: str | None, now: datetime
    ) -> dict[str, object]:
        return {
            "state": target,
            "started_at": current_started_at or now,
            "completed_at": now,
            "failure_code": failure_code if target == "failed" else None,
            "updated_at": now,
        }

    @staticmethod
    def _allows_change(
        *, current: str, target: str, allow_remediation: bool
    ) -> bool:
        if current == target:
            return False
        if current == "completed":
            # Manual reruns never demote an already complete analysis.
            return False
        if current == "partially_completed" and target == "completed":
            if not allow_remediation:
                raise InvalidSynthesisProjection("synthesis remediation is not allowed")
            return True
        if current in {"partially_completed", "failed", "canceled", "deleted"}:
            raise InvalidSynthesisProjection("terminal parent projection is not allowed")
        transition(current, target)
        return True

    async def project(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        tenant_resource_version: int,
        report_id: UUID | None,
        terminal: Literal["report", "failed", "canceled"],
        failure_code: str | None,
        now: datetime,
    ) -> str:
        allow_remediation = False
        device_scenario_targets: dict[str, tuple[str, str | None]] | None = None
        async with self._tenant_router.session(team_id) as session:
            if session.info.get("tenant_resource_version") != tenant_resource_version:
                raise SynthesisArtifactUnavailableError
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None or analysis.analysis_mode not in {"trace_upload", "device"}:
                raise SynthesisArtifactConflictError
            analysis_mode = analysis.analysis_mode
            if terminal == "report":
                if report_id is None or failure_code is not None:
                    raise InvalidSynthesisProjection("report projection is invalid")
                report_row = await session.scalar(
                    select(ReportVersion).where(
                        ReportVersion.id == report_id,
                        ReportVersion.analysis_id == analysis_id,
                        ReportVersion.scenario_result_id.is_(None),
                    )
                )
                if report_row is None or report_row.report is None:
                    raise ReportIntegrityError("immutable report conflict")
                target = self._target_from_report(report_row.report)
                if analysis.analysis_mode == "device":
                    device_scenario_targets = self._device_scenario_targets(
                        report_row.report
                    )
                if target == "completed":
                    previous = await session.scalar(
                        select(ReportVersion)
                        .where(
                            ReportVersion.analysis_id == analysis_id,
                            ReportVersion.scenario_result_id.is_(None),
                            ReportVersion.id != report_id,
                            ReportVersion.report.is_not(None),
                        )
                        .order_by(ReportVersion.report_version.desc())
                        .limit(1)
                    )
                    if previous is not None and previous.report is not None:
                        try:
                            allow_remediation = (
                                remediate_failed_synthesis(
                                    AnalysisState.PARTIALLY_COMPLETED,
                                    previous_report=previous.report,
                                    replacement_report=report_row.report,
                                )
                                == AnalysisState.COMPLETED
                            )
                        except InvalidSynthesisProjection:
                            allow_remediation = False
            elif terminal == "canceled":
                if report_id is not None or failure_code is not None:
                    raise InvalidSynthesisProjection("cancel projection is invalid")
                target = "canceled"
            else:
                if report_id is not None or not isinstance(failure_code, str):
                    raise InvalidSynthesisProjection("failure projection is invalid")
                target = "failed"
            if self._allows_change(
                current=analysis.state,
                target=target,
                allow_remediation=allow_remediation,
            ):
                changed = await session.scalar(
                    update(Analysis)
                    .where(
                        Analysis.id == analysis_id,
                        Analysis.version == analysis.version,
                        Analysis.state == analysis.state,
                    )
                    .values(
                        **self._values(
                            current_started_at=analysis.started_at,
                            target=target,
                            failure_code=failure_code,
                            now=now,
                        ),
                        version=Analysis.version + 1,
                    )
                    .returning(Analysis.id)
                )
                if changed is None:
                    raise SynthesisClaimLostError("parent projection authority changed")
            if device_scenario_targets is not None:
                scenario_results = list(
                    (
                        await session.scalars(
                            select(ScenarioResult)
                            .where(ScenarioResult.analysis_id == analysis_id)
                            .with_for_update()
                        )
                    ).all()
                )
                self._project_device_scenarios(
                    scenario_results,
                    targets=device_scenario_targets,
                    now=now,
                )

        async with self._control_sessions.begin() as session:
            job = await session.scalar(
                select(GlobalJob)
                .where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                    GlobalJob.analysis_mode == analysis_mode,
                )
                .with_for_update()
            )
            if job is None:
                raise SynthesisClaimLostError("parent projection authority changed")
            if self._allows_change(
                current=job.state,
                target=target,
                allow_remediation=allow_remediation,
            ):
                changed = await session.scalar(
                    update(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                        GlobalJob.version == job.version,
                        GlobalJob.state == job.state,
                    )
                    .values(
                        **self._values(
                            current_started_at=job.started_at,
                            target=target,
                            failure_code=failure_code,
                            now=now,
                        ),
                        version=GlobalJob.version + 1,
                    )
                    .returning(GlobalJob.id)
                )
                if changed is None:
                    raise SynthesisClaimLostError("parent projection authority changed")
            if device_scenario_targets is not None:
                scenario_jobs = list(
                    (
                        await session.scalars(
                            select(ScenarioJob)
                            .where(ScenarioJob.analysis_id == analysis_id)
                            .with_for_update()
                        )
                    ).all()
                )
                self._project_device_scenarios(
                    scenario_jobs,
                    targets=device_scenario_targets,
                    now=now,
                )
        return target


@dataclass(frozen=True, slots=True)
class SynthesisStepResult:
    state: Literal["pending", "running", "succeeded", "failed", "canceled"]
    retry_after_seconds: float | None = None


class _CanonicalReader(Protocol):
    async def read(self, execution: object) -> object: ...


class _ArtifactStore(Protocol):
    async def write(self, request: SynthesisArtifactWrite) -> object: ...

    async def read(self, **kwargs: object) -> object: ...


class _Provider(Protocol):
    async def synthesize(
        self, projection: AIProjection, *, retry_code: str | None = None
    ) -> SynthesisCandidate: ...


class _AnalysisContexts(Protocol):
    async def load(self, **kwargs: object) -> SynthesisAnalysisContext: ...


class _MemorySources(Protocol):
    async def load(self, **kwargs: object) -> SynthesisMemorySourceContext: ...


class _ParentProjector(Protocol):
    async def project(self, **kwargs: object) -> str: ...


Checkpoint = Callable[[str], Awaitable[None] | None]


class SynthesisPipeline:
    """Advance exactly one durable synthesis boundary from authoritative state."""

    def __init__(
        self,
        *,
        repository: SQLAlchemySynthesisExecutionRepository,
        canonical_reader: _CanonicalReader,
        artifact_store: _ArtifactStore,
        provider: _Provider,
        report_writer: AnalysisReportWriter,
        analysis_contexts: _AnalysisContexts,
        memory_sources: _MemorySources,
        parent_projector: _ParentProjector,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        checkpoint: Checkpoint | None = None,
        normalizer: Callable[..., NormalizedTraceReport] = normalize_smartperfetto_result,
        memory_result_joiner: Callable[
            [NormalizedTraceReport, object], NormalizedTraceReport
        ] = join_android_memory_result,
        memory_unavailable_joiner: Callable[..., NormalizedTraceReport] = (
            join_unavailable_android_memory
        ),
        projection_builder: Callable[..., AIProjection] = build_ai_projection,
        max_projection_bytes: int = 256 * 1024,
    ) -> None:
        if (
            type(max_projection_bytes) is not int
            or not 1024 <= max_projection_bytes <= 256 * 1024
        ):
            raise ValueError("projection byte limit is invalid")
        self._repository = repository
        self._canonical_reader = canonical_reader
        self._artifact_store = artifact_store
        self._provider = provider
        self._report_writer = report_writer
        self._analysis_contexts = analysis_contexts
        self._memory_sources = memory_sources
        self._parent_projector = parent_projector
        self._clock = clock
        self._checkpoint_callback = checkpoint
        self._normalizer = normalizer
        self._memory_result_joiner = memory_result_joiner
        self._memory_unavailable_joiner = memory_unavailable_joiner
        self._projection_builder = projection_builder
        self._max_projection_bytes = max_projection_bytes

    async def _checkpoint(self, name: str) -> None:
        if self._checkpoint_callback is None:
            return
        result = self._checkpoint_callback(name)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _fence(claim: SynthesisWorkClaim) -> SynthesisMutationFence:
        return SynthesisMutationFence(
            claim_id=claim.claim_id,
            event_id=claim.event_id,
            consumer_id=claim.consumer_id,
            token=claim.token,
        )

    async def _core(
        self,
        source: object,
        *,
        analysis_mode: Literal["trace_upload", "device"],
    ) -> tuple[object, NormalizedTraceReport]:
        loaded = await self._canonical_reader.read(source)
        await self._checkpoint("canonical_read")
        core = self._normalizer(loaded, analysis_mode=analysis_mode)
        if analysis_mode == "device":
            core = await self._join_device_memory(source=source, core=core)
        return loaded, core

    async def _join_device_memory(
        self,
        *,
        source: object,
        core: NormalizedTraceReport,
    ) -> NormalizedTraceReport:
        memory = await self._memory_sources.load(
            team_id=getattr(source, "team_id", None),
            analysis_id=getattr(source, "analysis_id", None),
            tenant_resource_version=getattr(source, "tenant_resource_version", None),
        )
        execution = memory.execution
        if execution is None:
            if memory.scenario_state in _MEMORY_SCENARIO_ACTIVE_STATES:
                raise CanonicalResultUnavailableError
            reason: MemoryUnavailableReason = (
                "execution_canceled"
                if memory.scenario_state == "canceled"
                else "execution_failed"
                if memory.scenario_state == "failed"
                else "result_unavailable"
            )
            return self._memory_unavailable_joiner(core, reason=reason)
        if execution.state in {"completed", "insufficient_data"}:
            try:
                loaded = await self._canonical_reader.read(execution)
                await self._checkpoint("memory_canonical_read")
                return self._memory_result_joiner(core, loaded)
            except CanonicalResultUnavailableError:
                raise
            except (CanonicalResultIntegrityError, AndroidMemoryNormalizationError):
                return self._memory_unavailable_joiner(core, reason="result_invalid")
        if execution.state in {"failed", "canceled"}:
            reason = (
                "execution_canceled"
                if execution.state == "canceled"
                else "execution_failed"
            )
            return self._memory_unavailable_joiner(core, reason=reason)
        raise CanonicalResultUnavailableError

    async def _projection(
        self,
        *,
        execution: SynthesisExecutionRecord,
        source: object,
    ) -> tuple[object, NormalizedTraceReport, AIProjection]:
        context = await self._analysis_contexts.load(
            team_id=execution.team_id,
            analysis_id=execution.analysis_id,
            tenant_resource_version=execution.tenant_resource_version,
        )
        loaded, core = await self._core(
            source,
            analysis_mode=context.analysis_mode,
        )
        projection = self._projection_builder(
            core,
            analysis_profile=context.analysis_profile,
            question=context.question,
            max_bytes=self._max_projection_bytes,
        )
        return loaded, core, projection

    async def _record_projection_failure(
        self,
        *,
        record: SynthesisExecutionRecord,
        now: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisStepResult:
        await self._repository.bind_preflight_failure(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            execution_id=record.id,
            stable_error_code="ai_projection_invalid",
            generated_at=now,
            fence=fence,
        )
        await self._checkpoint("projection_preflight_failure")
        return SynthesisStepResult("running")

    async def advance(self, claim: SynthesisWorkClaim) -> SynthesisStepResult:
        try:
            return await self._advance(claim)
        except SynthesisLeaseLostError:
            raise SynthesisClaimLostError("synthesis claim was lost") from None
        except (CanonicalResultUnavailableError, SynthesisArtifactUnavailableError):
            return SynthesisStepResult("pending", 5)
        except (
            CanonicalResultIntegrityError,
            AndroidMemoryNormalizationError,
            SmartPerfettoNormalizationError,
            SynthesisArtifactConflictError,
            ReportIntegrityError,
            ReportSourceError,
        ) as error:
            code = (
                "report_integrity_failed"
                if isinstance(error, (ReportIntegrityError, ReportSourceError))
                else "core_report_invalid"
            )
            now = self._clock()
            record = await self._repository.fail_without_report(
                team_id=claim.team_id,
                analysis_id=claim.analysis_id,
                execution_id=claim.synthesis_execution_id,
                stable_error_code=code,
                now=now,
                fence=self._fence(claim),
            )
            await self._parent_projector.project(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                tenant_resource_version=record.tenant_resource_version,
                report_id=None,
                terminal="failed",
                failure_code=code,
                now=now,
            )
            return SynthesisStepResult("failed")

    async def _advance(self, claim: SynthesisWorkClaim) -> SynthesisStepResult:
        now = self._clock()
        record = await self._repository.load(
            team_id=claim.team_id,
            analysis_id=claim.analysis_id,
            execution_id=claim.synthesis_execution_id,
        )
        source = await self._repository.load_source(
            team_id=claim.team_id,
            analysis_id=claim.analysis_id,
            execution_id=claim.synthesis_execution_id,
        )
        if record.state == "canceled":
            await self._parent_projector.project(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                tenant_resource_version=record.tenant_resource_version,
                report_id=None,
                terminal="canceled",
                failure_code=None,
                now=now,
            )
            return SynthesisStepResult("canceled")
        if record.state in {"succeeded", "failed"}:
            if record.report_version_id is None:
                await self._parent_projector.project(
                    team_id=record.team_id,
                    analysis_id=record.analysis_id,
                    tenant_resource_version=record.tenant_resource_version,
                    report_id=None,
                    terminal="failed",
                    failure_code=record.stable_error_code or "synthesis_failed",
                    now=now,
                )
                return SynthesisStepResult("failed")
            await self._parent_projector.project(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                tenant_resource_version=record.tenant_resource_version,
                report_id=record.report_version_id,
                terminal="report",
                failure_code=None,
                now=now,
            )
            await self._checkpoint("parent_terminalization")
            return SynthesisStepResult(record.state)

        fence = self._fence(claim)
        if record.projection_artifact_id is None and record.stable_error_code is None:
            try:
                loaded, _core, projection = await self._projection(
                    execution=record,
                    source=source,
                )
            except (ProjectionSizeError, ProjectionPrivacyError, ProjectionQuestionError):
                return await self._record_projection_failure(
                    record=record,
                    now=now,
                    fence=fence,
                )
            artifact_id = projection_artifact_id(
                loaded.artifact_id, record.normalizer_version
            )
            await self._artifact_store.write(
                SynthesisArtifactWrite(
                    team_id=record.team_id,
                    analysis_id=record.analysis_id,
                    tenant_resource_version=record.tenant_resource_version,
                    artifact_id=artifact_id,
                    kind="ai_projection",
                    canonical_bytes=projection.canonical_bytes,
                    sha256_b64=projection.sha256_b64,
                )
            )
            await self._checkpoint("projection_artifact_write")
            await self._repository.bind_projection(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                artifact_id=artifact_id,
                sha256_b64=projection.sha256_b64,
                now=now,
                fence=fence,
            )
            await self._checkpoint("projection_binding")
            return SynthesisStepResult("running")

        if record.candidate_artifact_id is None and record.stable_error_code is None:
            try:
                _loaded, _core, projection = await self._projection(
                    execution=record,
                    source=source,
                )
            except (ProjectionSizeError, ProjectionPrivacyError, ProjectionQuestionError):
                return await self._record_projection_failure(
                    record=record,
                    now=now,
                    fence=fence,
                )
            attempt = await self._repository.begin_invocation(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                now=now,
                fence=fence,
            )
            retry_code = (
                "ai_output_invalid"
                if record.last_invocation_error_code == "ai_output_invalid"
                else None
            )
            try:
                candidate = await self._provider.synthesize(
                    projection,
                    retry_code=retry_code,
                )
                await self._checkpoint("provider_response")
                validated = validate_synthesis_output(
                    projection=projection,
                    candidate=candidate.candidate_json,
                )
            except AIProviderError as error:
                retry = error.retryable and attempt < 2
                await self._repository.finish_invocation_failure(
                    team_id=record.team_id,
                    analysis_id=record.analysis_id,
                    execution_id=record.id,
                    attempt_number=attempt,
                    stable_error_code=error.stable_code,
                    latency_ms=None,
                    exhausted=not retry,
                    generated_at=None if retry else now,
                    now=now,
                    fence=fence,
                )
                return SynthesisStepResult("pending" if retry else "running", 1 if retry else None)
            except SynthesisValidationError:
                retry = attempt < 2
                await self._repository.finish_invocation_failure(
                    team_id=record.team_id,
                    analysis_id=record.analysis_id,
                    execution_id=record.id,
                    attempt_number=attempt,
                    stable_error_code="ai_output_invalid",
                    latency_ms=candidate.latency_ms,
                    exhausted=not retry,
                    generated_at=None if retry else now,
                    now=now,
                    fence=fence,
                )
                return SynthesisStepResult("pending" if retry else "running", 1 if retry else None)
            artifact_id = synthesis_artifact_id(record.id, validated.sha256_b64)
            await self._artifact_store.write(
                SynthesisArtifactWrite(
                    team_id=record.team_id,
                    analysis_id=record.analysis_id,
                    tenant_resource_version=record.tenant_resource_version,
                    artifact_id=artifact_id,
                    kind="ai_synthesis_result",
                    canonical_bytes=validated.canonical_bytes,
                    sha256_b64=validated.sha256_b64,
                )
            )
            await self._checkpoint("candidate_artifact_write")
            await self._repository.bind_candidate_result(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                attempt_number=attempt,
                artifact_id=artifact_id,
                sha256_b64=validated.sha256_b64,
                prompt_tokens=candidate.prompt_tokens,
                completion_tokens=candidate.completion_tokens,
                total_tokens=candidate.prompt_tokens + candidate.completion_tokens,
                latency_ms=candidate.latency_ms,
                generated_at=now,
                now=now,
                fence=fence,
            )
            await self._checkpoint("candidate_binding")
            return SynthesisStepResult("running")

        if record.report_generated_at is None:
            await self._repository.bind_report_timestamp(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                generated_at=now,
                fence=fence,
            )
            return SynthesisStepResult("running")

        context = await self._analysis_contexts.load(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            tenant_resource_version=record.tenant_resource_version,
        )
        loaded, core = await self._core(
            source,
            analysis_mode=context.analysis_mode,
        )
        synthesis_document: Mapping[str, object] | None = None
        if record.candidate_artifact_id is not None:
            artifact = await self._artifact_store.read(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                tenant_resource_version=record.tenant_resource_version,
                artifact_id=record.candidate_artifact_id,
                kind="ai_synthesis_result",
                sha256_b64=record.candidate_sha256_b64,
            )
            value = json.loads(artifact.canonical_bytes)
            if not isinstance(value, Mapping):
                raise SynthesisArtifactConflictError
            synthesis_document = value
        request = AnalysisReportWriteRequest(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            synthesis_execution_id=record.id,
            tenant_resource_version=record.tenant_resource_version,
            generation=record.generation,
            generated_at=record.report_generated_at,
            core_document=core.document,
            synthesis_document=synthesis_document,
            synthesis_failure_code=record.stable_error_code,
            canonical_artifact_id=loaded.artifact_id,
            canonical_sha256_b64=loaded.sha256_b64,
            projection_artifact_id=record.projection_artifact_id,
            projection_sha256_b64=record.projection_sha256_b64,
            synthesis_artifact_id=record.candidate_artifact_id,
            synthesis_sha256_b64=record.candidate_sha256_b64,
            normalizer_version=record.normalizer_version,
            prompt_template_version=record.prompt_template_version,
            prompt_template_sha256_b64=record.prompt_template_sha256_b64,
            report_worker_image_digest=record.report_worker_image_digest,
            provider_protocol=record.provider_protocol,
            provider_name=record.provider_name,
            model=record.provider_model,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            latency_ms=record.latency_ms,
        )
        published = await self._report_writer.publish(request)
        await self._checkpoint("tenant_report_insert")
        expected_report_id = report_version_id(record.id)
        if published.id != expected_report_id:
            raise ReportIntegrityError("immutable report conflict")
        if source.normalized_report_version_id != expected_report_id:
            await self._repository.bind_source_report(
                team_id=record.team_id,
                analysis_id=record.analysis_id,
                execution_id=record.id,
                report_version_id=expected_report_id,
                now=now,
                fence=fence,
            )
            await self._checkpoint("engine_report_binding")
            return SynthesisStepResult("running")
        await self._repository.bind_report(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            execution_id=record.id,
            report_version_id=expected_report_id,
            synthesis_succeeded=record.candidate_artifact_id is not None,
            now=now,
            fence=fence,
        )
        await self._checkpoint("synthesis_completion")
        return SynthesisStepResult("running")


class SynthesisOrchestrationWorker:
    def __init__(
        self,
        *,
        coordinator: SynthesisCoordinator | None = None,
        queue: SQLAlchemySynthesisWorkQueue,
        pipeline: SynthesisPipeline,
        worker_id: str,
        idle_poll_seconds: float = 1,
        active_poll_seconds: float = 1,
        failure_backoff_seconds: float = 5,
        heartbeat_seconds: float = 10,
    ) -> None:
        if _WORKER.fullmatch(worker_id) is None or min(
            idle_poll_seconds,
            active_poll_seconds,
            failure_backoff_seconds,
            heartbeat_seconds,
        ) <= 0:
            raise ValueError("synthesis worker configuration is invalid")
        self._coordinator = coordinator
        self._queue = queue
        self._pipeline = pipeline
        self._worker_id = worker_id
        self._idle_poll_seconds = idle_poll_seconds
        self._active_poll_seconds = active_poll_seconds
        self._failure_backoff_seconds = failure_backoff_seconds
        self._heartbeat_seconds = heartbeat_seconds

    async def _heartbeat(self, claim: SynthesisWorkClaim) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self._queue.renew(claim)

    async def _advance(self, claim: SynthesisWorkClaim) -> SynthesisStepResult:
        advancement = asyncio.create_task(self._pipeline.advance(claim))
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            done, _ = await asyncio.wait(
                (advancement, heartbeat), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                advancement.cancel()
                with suppress(asyncio.CancelledError):
                    await advancement
                await heartbeat
                raise AssertionError("unreachable")
            return await advancement
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def run_once(self) -> bool:
        try:
            coordinated = (
                await self._coordinator.coordinate_next()
                if self._coordinator is not None
                else None
            )
        except (SynthesisClaimLostError, TenantRouteError):
            coordinated = None
        claim = await self._queue.claim_next(consumer_id=self._worker_id)
        if claim is None:
            return coordinated is not None
        try:
            outcome = await self._advance(claim)
            if outcome.state in {"succeeded", "failed", "canceled"}:
                await self._queue.complete(claim)
            elif outcome.state in {"pending", "running"}:
                await self._queue.reschedule(
                    claim,
                    delay_seconds=outcome.retry_after_seconds
                    or self._active_poll_seconds,
                )
            else:
                raise RuntimeError("synthesis pipeline returned an invalid state")
        except SynthesisClaimLostError:
            return False
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except Exception:
            try:
                await self._queue.retry(
                    claim, delay_seconds=self._failure_backoff_seconds
                )
            except SynthesisClaimLostError:
                return False
        return True

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            try:
                worked = await self.run_once()
                delay = 0 if worked else self._idle_poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = self._failure_backoff_seconds
            if delay == 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
            except TimeoutError:
                pass


__all__ = [
    "SQLAlchemyAutomaticSynthesisRequestFactory",
    "SQLAlchemySynthesisAnalysisContextRepository",
    "SQLAlchemySynthesisMemorySourceRepository",
    "SQLAlchemySynthesisParentProjector",
    "SQLAlchemySynthesisWorkQueue",
    "SynthesisAnalysisContext",
    "SynthesisMemorySourceContext",
    "SynthesisClaimLostError",
    "SynthesisCoordinator",
    "SynthesisOrchestrationWorker",
    "SynthesisPipeline",
    "SynthesisStepResult",
    "SynthesisWorkClaim",
    "analysis_synthesis_requested_event_id",
]
