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
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.ai.openai_compatible import AIProviderError, SynthesisCandidate
from perfpilot_api.ai.synthesis import SynthesisValidationError, validate_synthesis_output
from perfpilot_api.db.control.models import (
    EngineExecution,
    GlobalJob,
    OutboxEvent,
    SynthesisExecution,
    WorkerClaim,
)
from perfpilot_api.db.tenant.models import Analysis, ReportVersion
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.domain.states import AnalysisState
from perfpilot_api.domain.transitions import (
    InvalidSynthesisProjection,
    remediate_failed_synthesis,
    transition,
)
from perfpilot_api.reports.contracts import validate_contract
from perfpilot_api.reports.normalizer import (
    NormalizedTraceReport,
    SmartPerfettoNormalizationError,
    normalize_smartperfetto_result,
)
from perfpilot_api.reports.projection import AIProjection, build_ai_projection
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
    SynthesisExecutionRecord,
    SynthesisLeaseLostError,
    SynthesisMutationFence,
    SynthesisRequest,
)


_WORKER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_EVENT_NAMESPACE = UUID("9bf739f1-eafc-5ba6-a95b-09fe18c4c315")
_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


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


class SynthesisCoordinator:
    """Turns verified source-result events into automatic generation one.

    `request_factory` receives metadata only; canonical artifact bytes are deliberately
    unavailable to this component.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], repository: SQLAlchemySynthesisExecutionRepository, request_factory: Callable[[EngineExecution, int], SynthesisRequest], clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._sessions, self._repository, self._request_factory, self._clock = session_factory, repository, request_factory, clock

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
                raise SynthesisClaimLostError("source event authority changed")
            latest = await session.scalar(select(EngineExecution.id).where(
                EngineExecution.team_id == source.team_id, EngineExecution.analysis_id == source.analysis_id,
                EngineExecution.engine_id == "smartperfetto").order_by(EngineExecution.attempt_number.desc()).limit(1))
            if latest != source.id:
                raise SynthesisClaimLostError("source attempt is stale")
            # Close the control transaction before repository allocation to retain one
            # lock authority; retries reload by unique source/generation.
            team_id, analysis_id, source_id = source.team_id, source.analysis_id, source.id
        record = await self._repository.allocate(
            team_id=team_id,
            analysis_id=analysis_id,
            source_execution_id=source_id,
            request=self._request_factory(source, 1),
            now=now,
            mode="auto",
        )
        async with self._sessions.begin() as session:
            event = await session.get(OutboxEvent, event.id)
            if event is None:
                raise SynthesisClaimLostError("source event disappeared")
            requested_id = analysis_synthesis_requested_event_id(record.id)
            requested = await session.get(OutboxEvent, requested_id)
            if requested is None:
                session.add(OutboxEvent(id=requested_id, team_id=record.team_id, global_job_id=record.analysis_id,
                    scenario_job_id=None, event_type="analysis_synthesis_requested", subject_type="synthesis_execution",
                    subject_id=record.id, subject_version=record.version, ready_at=now, published_at=None,
                    dead_lettered_at=None, retry_count=0, version=1))
            elif (requested.team_id != record.team_id or requested.global_job_id != record.analysis_id
                  or requested.subject_id != record.id or requested.subject_version != record.version):
                raise SynthesisClaimLostError("synthesis event authority changed")
            event.published_at, event.version, event.updated_at = now, event.version + 1, now
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
            row, event = await self._owned(session, claim)
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
                or row.analysis_mode != "trace_upload"
                or row.analysis_profile not in {"auto", "startup", "scroll"}
                or row.tombstoned_at is not None
            ):
                raise SynthesisArtifactConflictError
            return SynthesisAnalysisContext(
                analysis_profile=row.analysis_profile,  # type: ignore[arg-type]
                question=row.question,
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
        async with self._tenant_router.session(team_id) as session:
            if session.info.get("tenant_resource_version") != tenant_resource_version:
                raise SynthesisArtifactUnavailableError
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None or analysis.analysis_mode != "trace_upload":
                raise SynthesisArtifactConflictError
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

        async with self._control_sessions.begin() as session:
            job = await session.scalar(
                select(GlobalJob)
                .where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                    GlobalJob.analysis_mode == "trace_upload",
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
        parent_projector: _ParentProjector,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        checkpoint: Checkpoint | None = None,
        normalizer: Callable[[Any], NormalizedTraceReport] = normalize_smartperfetto_result,
        projection_builder: Callable[..., AIProjection] = build_ai_projection,
    ) -> None:
        self._repository = repository
        self._canonical_reader = canonical_reader
        self._artifact_store = artifact_store
        self._provider = provider
        self._report_writer = report_writer
        self._analysis_contexts = analysis_contexts
        self._parent_projector = parent_projector
        self._clock = clock
        self._checkpoint_callback = checkpoint
        self._normalizer = normalizer
        self._projection_builder = projection_builder

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

    async def _core(self, source: object) -> tuple[object, NormalizedTraceReport]:
        loaded = await self._canonical_reader.read(source)
        await self._checkpoint("canonical_read")
        return loaded, self._normalizer(loaded)

    async def _projection(
        self,
        *,
        execution: SynthesisExecutionRecord,
        source: object,
    ) -> tuple[object, NormalizedTraceReport, AIProjection]:
        loaded, core = await self._core(source)
        context = await self._analysis_contexts.load(
            team_id=execution.team_id,
            analysis_id=execution.analysis_id,
            tenant_resource_version=execution.tenant_resource_version,
        )
        projection = self._projection_builder(
            core,
            analysis_profile=context.analysis_profile,
            question=context.question,
        )
        return loaded, core, projection

    async def advance(self, claim: SynthesisWorkClaim) -> SynthesisStepResult:
        try:
            return await self._advance(claim)
        except SynthesisLeaseLostError:
            raise SynthesisClaimLostError("synthesis claim was lost") from None
        except (CanonicalResultUnavailableError, SynthesisArtifactUnavailableError):
            return SynthesisStepResult("pending", 5)
        except (
            CanonicalResultIntegrityError,
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
        if record.projection_artifact_id is None:
            loaded, _core, projection = await self._projection(
                execution=record,
                source=source,
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
            _loaded, _core, projection = await self._projection(
                execution=record,
                source=source,
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

        loaded, core = await self._core(source)
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
        claim = await self._queue.claim_next(consumer_id=self._worker_id)
        if claim is None:
            return False
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


__all__ = ["SQLAlchemySynthesisAnalysisContextRepository", "SQLAlchemySynthesisParentProjector", "SQLAlchemySynthesisWorkQueue", "SynthesisAnalysisContext", "SynthesisClaimLostError", "SynthesisCoordinator", "SynthesisOrchestrationWorker", "SynthesisPipeline", "SynthesisStepResult", "SynthesisWorkClaim", "analysis_synthesis_requested_event_id"]
