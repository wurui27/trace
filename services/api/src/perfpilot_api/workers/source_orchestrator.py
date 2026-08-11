"""Gate synthesis on bounded source context without making source a hard dependency."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import EngineExecution, GlobalJob
from perfpilot_api.db.tenant.models import Analysis
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.reports.normalizer import normalize_smartperfetto_result
from perfpilot_api.reports.source_context import SourceContextValidationError
from perfpilot_api.services.canonical_result_reader import CanonicalResultReader
from perfpilot_api.services.source_artifacts import SourceArtifactError
from perfpilot_api.services.source_tasks import (
    SourceCompletionArtifact,
    SourceContextTaskStatus,
    SourceTaskError,
)


_SMART_READY = {"completed", "insufficient_data"}
_SMART_FAILED = {"failed", "canceled"}
_ACTIVE = {"queued", "leased", "running", "cancel_requested"}


@dataclass(frozen=True, slots=True)
class SourceAnalysisAuthority:
    team_id: UUID
    analysis_id: UUID
    smartperfetto_state: str
    agent_id: UUID | None
    workspace_id: UUID | None
    validation_profile_id: UUID | None
    finding_hints: tuple[dict[str, object], ...]
    direct_identifiers: tuple[str, ...]
    finding_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedSourceAuthority:
    finding_hints: tuple[dict[str, object], ...]
    direct_identifiers: tuple[str, ...]
    finding_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


_DIRECT_IDENTIFIER_FIELDS = {
    "class_method",
    "mapped_symbol",
    "method_symbol",
    "native_symbol",
    "symbol",
    "trace_section",
}
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "healthy": 2, "informational": 3}


def _identifier(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def derive_source_authority(report: Mapping[str, object]) -> DerivedSourceAuthority:
    scenarios = report.get("scenario_reports") if isinstance(report, Mapping) else None
    if not isinstance(scenarios, list):
        raise ValueError("source authority is invalid")
    findings: list[dict[str, object]] = []
    evidence_by_id: dict[str, Mapping[str, object]] = {}
    finding_ids: set[str] = set()
    evidence_ids: set[str] = set()
    direct: set[str] = set()
    weak: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise ValueError("source authority is invalid")
        raw_evidence = scenario.get("evidence")
        raw_findings = scenario.get("findings")
        health = scenario.get("trace_health")
        if not isinstance(raw_evidence, list) or not isinstance(raw_findings, list):
            raise ValueError("source authority is invalid")
        if isinstance(health, Mapping):
            target = health.get("target_resolution")
            if isinstance(target, Mapping):
                for key in ("package_name", "process_name"):
                    value = _identifier(target.get(key))
                    if value is not None:
                        weak.add(value)
        for evidence in raw_evidence:
            if not isinstance(evidence, Mapping):
                raise ValueError("source authority is invalid")
            evidence_id = _identifier(evidence.get("evidence_id"))
            fields = evidence.get("fields")
            if evidence_id is None or not isinstance(fields, Mapping):
                raise ValueError("source authority is invalid")
            evidence_ids.add(evidence_id)
            evidence_by_id[evidence_id] = evidence
            for key, raw in fields.items():
                if key in _DIRECT_IDENTIFIER_FIELDS:
                    value = _identifier(raw)
                    if value is not None:
                        direct.add(value)
        for finding in raw_findings:
            if not isinstance(finding, dict):
                raise ValueError("source authority is invalid")
            finding_id = _identifier(finding.get("finding_id"))
            if finding_id is None:
                raise ValueError("source authority is invalid")
            finding_ids.add(finding_id)
            findings.append(finding)
    findings.sort(
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity")), 99),
            str(item.get("finding_id")),
        )
    )
    hints: list[dict[str, object]] = []
    for finding in findings[:3]:
        linked = finding.get("evidence_ids")
        if not isinstance(linked, list) or any(item not in evidence_ids for item in linked):
            raise ValueError("source authority is invalid")
        symbols: set[str] = set()
        for evidence_id in linked:
            fields = evidence_by_id[evidence_id].get("fields")
            if isinstance(fields, Mapping):
                for key, raw in fields.items():
                    if key in _DIRECT_IDENTIFIER_FIELDS:
                        value = _identifier(raw)
                        if value is not None:
                            symbols.add(value)
        if not symbols:
            symbols.update(weak)
        hints.append(
            {
                "finding_id": finding["finding_id"],
                "evidence_ids": list(linked),
                "rule_id": finding["rule_id"],
                "symbol_hints": sorted(symbols)[:20],
            }
        )
    return DerivedSourceAuthority(
        finding_hints=tuple(hints),
        direct_identifiers=tuple(sorted(direct)),
        finding_ids=tuple(sorted(finding_ids)),
        evidence_ids=tuple(sorted(evidence_ids)),
    )


@dataclass(frozen=True, slots=True)
class SourceAnalysisState:
    requested: bool
    context_state: Literal[
        "not_requested", "waiting_for_agent", "extracting", "available", "unavailable"
    ]
    match_summary: Literal["strong", "weak", "none"] = "none"
    failure_code: str | None = None
    artifact_id: UUID | None = None
    checksum: str | None = None
    context: dict[str, object] | None = None


class SourceAuthorityReader(Protocol):
    async def load_source_authority(
        self, analysis_id: UUID
    ) -> SourceAnalysisAuthority: ...


class SourceTaskGateway(Protocol):
    async def context_status(
        self, *, team_id: UUID, analysis_id: UUID
    ) -> SourceContextTaskStatus | None: ...

    async def create_context_task(self, **kwargs) -> object: ...


class SourceContextReader(Protocol):
    async def read_context(self, **kwargs) -> dict[str, object]: ...

    async def persist_validated_context(self, **kwargs) -> SourceCompletionArtifact: ...


class SynthesisScheduler(Protocol):
    async def enqueue_once(self, *, team_id: UUID, analysis_id: UUID) -> None: ...


class SourceAnalysisStateRepository(Protocol):
    async def save(
        self,
        team_id: UUID,
        analysis_id: UUID,
        state: SourceAnalysisState,
    ) -> None: ...


class InMemorySourceAnalysisStateRepository:
    def __init__(self) -> None:
        self._states: dict[UUID, SourceAnalysisState] = {}

    async def save(
        self,
        team_id: UUID,
        analysis_id: UUID,
        state: SourceAnalysisState,
    ) -> None:
        del team_id
        self._states[analysis_id] = replace(state)

    def get(self, analysis_id: UUID) -> SourceAnalysisState:
        return self._states.get(
            analysis_id,
            SourceAnalysisState(requested=False, context_state="not_requested"),
        )


class SQLAlchemySourceAnalysisStateRepository:
    def __init__(self, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    async def save(
        self,
        team_id: UUID,
        analysis_id: UUID,
        state: SourceAnalysisState,
    ) -> None:
        async with self._tenant_router.session(team_id) as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None or analysis.tombstoned_at is not None:
                raise ValueError("source analysis state is unavailable")
            analysis.source_context_state = state.context_state
            analysis.source_match_summary = state.match_summary
            analysis.source_failure_code = state.failure_code
            analysis.source_context_artifact_id = state.artifact_id
            analysis.source_context_checksum = state.checksum


class SQLAlchemySourceAuthorityReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        canonical_reader: CanonicalResultReader,
    ) -> None:
        self._sessions = session_factory
        self._canonical_reader = canonical_reader

    async def load_source_authority(
        self, analysis_id: UUID
    ) -> SourceAnalysisAuthority:
        async with self._sessions() as session:
            job = await session.get(GlobalJob, analysis_id)
            engine = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
                .order_by(EngineExecution.attempt_number.desc())
                .limit(1)
            )
        if job is None:
            raise ValueError("source analysis authority is unavailable")
        derived = DerivedSourceAuthority((), (), (), ())
        if engine is not None and engine.state in _SMART_READY:
            loaded = await self._canonical_reader.read(engine)
            normalized = normalize_smartperfetto_result(
                loaded,
                analysis_mode=("device" if job.analysis_mode == "device" else "trace_upload"),
            )
            derived = derive_source_authority(normalized.document)
        return SourceAnalysisAuthority(
            team_id=job.team_id,
            analysis_id=job.id,
            smartperfetto_state="pending" if engine is None else engine.state,
            agent_id=job.source_agent_id,
            workspace_id=job.source_workspace_id,
            validation_profile_id=job.source_validation_profile_id,
            finding_hints=derived.finding_hints,
            direct_identifiers=derived.direct_identifiers,
            finding_ids=derived.finding_ids,
            evidence_ids=derived.evidence_ids,
        )


class NoopSynthesisScheduler:
    async def enqueue_once(self, *, team_id: UUID, analysis_id: UUID) -> None:
        del team_id, analysis_id


class SourceOrchestrator:
    def __init__(
        self,
        *,
        authority: SourceAuthorityReader,
        tasks: SourceTaskGateway,
        artifacts: SourceContextReader,
        states: SourceAnalysisStateRepository,
        scheduler: SynthesisScheduler,
        clock=lambda: datetime.now(UTC),
        deadline_seconds: int = 120,
    ) -> None:
        if type(deadline_seconds) is not int or deadline_seconds != 120:
            raise ValueError("source context deadline is invalid")
        self._authority = authority
        self._tasks = tasks
        self._artifacts = artifacts
        self._states = states
        self._scheduler = scheduler
        self._clock = clock
        self._deadline = timedelta(seconds=deadline_seconds)
        self._scheduled: set[UUID] = set()
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def _enqueue_once(self, authority: SourceAnalysisAuthority) -> None:
        if authority.analysis_id in self._scheduled:
            return
        await self._scheduler.enqueue_once(
            team_id=authority.team_id,
            analysis_id=authority.analysis_id,
        )
        self._scheduled.add(authority.analysis_id)

    async def _degrade(
        self,
        authority: SourceAnalysisAuthority,
        failure_code: str,
    ) -> bool:
        await self._states.save(
            authority.team_id,
            authority.analysis_id,
            SourceAnalysisState(
                requested=True,
                context_state="unavailable",
                failure_code=failure_code,
            ),
        )
        await self._enqueue_once(authority)
        return True

    async def prepare_for_synthesis(self, analysis_id: UUID) -> bool:
        if not isinstance(analysis_id, UUID):
            raise ValueError("analysis identity is invalid")
        lock = self._locks.setdefault(analysis_id, asyncio.Lock())
        async with lock:
            authority = await self._authority.load_source_authority(analysis_id)
            if authority.analysis_id != analysis_id:
                raise ValueError("source analysis authority is invalid")
            if authority.smartperfetto_state in _SMART_FAILED:
                return False
            if authority.smartperfetto_state not in _SMART_READY:
                return False
            if authority.agent_id is None or authority.workspace_id is None:
                await self._states.save(
                    authority.team_id,
                    analysis_id,
                    SourceAnalysisState(
                        requested=False,
                        context_state="not_requested",
                    ),
                )
                await self._enqueue_once(authority)
                return True
            try:
                status = await self._tasks.context_status(
                    team_id=authority.team_id,
                    analysis_id=analysis_id,
                )
                if status is None:
                    await self._tasks.create_context_task(
                        team_id=authority.team_id,
                        analysis_id=analysis_id,
                        agent_id=authority.agent_id,
                        workspace_id=authority.workspace_id,
                        validation_profile_id=authority.validation_profile_id,
                        finding_hints=authority.finding_hints,
                    )
                    await self._states.save(
                        authority.team_id,
                        analysis_id,
                        SourceAnalysisState(
                            requested=True,
                            context_state="waiting_for_agent",
                        ),
                    )
                    return False
            except SourceTaskError:
                return await self._degrade(authority, "source_agent_unavailable")
            if status.state in _ACTIVE:
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("source orchestrator clock is invalid")
                if now.astimezone(UTC) - status.created_at.astimezone(UTC) >= self._deadline:
                    return await self._degrade(authority, "source_context_timeout")
                await self._states.save(
                    authority.team_id,
                    analysis_id,
                    SourceAnalysisState(
                        requested=True,
                        context_state=(
                            "extracting"
                            if status.state in {"leased", "running", "cancel_requested"}
                            else "waiting_for_agent"
                        ),
                    ),
                )
                return False
            if (
                status.state == "completed"
                and status.artifact_id is not None
                and status.checksum is not None
            ):
                try:
                    context = await self._artifacts.read_context(
                        team_id=authority.team_id,
                        analysis_id=analysis_id,
                        artifact_id=status.artifact_id,
                        expected_checksum=status.checksum,
                        direct_identifiers=authority.direct_identifiers,
                        allowed_finding_ids=authority.finding_ids,
                        allowed_evidence_ids=authority.evidence_ids,
                    )
                    match = context.get("match_summary")
                    if match not in {"strong", "weak", "none"}:
                        raise SourceContextValidationError
                    persisted = await self._artifacts.persist_validated_context(
                        team_id=authority.team_id,
                        analysis_id=analysis_id,
                        source_artifact_id=status.artifact_id,
                        context=context,
                        now=self._clock(),
                    )
                except (SourceArtifactError, SourceContextValidationError, ValueError):
                    return await self._degrade(authority, "source_context_invalid")
                await self._states.save(
                    authority.team_id,
                    analysis_id,
                    SourceAnalysisState(
                        requested=True,
                        context_state="available",
                        match_summary=match,  # type: ignore[arg-type]
                        artifact_id=persisted.artifact_id,
                        checksum=persisted.checksum,
                        context=dict(context),
                    ),
                )
                await self._enqueue_once(authority)
                return True
            failure = status.failure_code or "source_agent_unavailable"
            return await self._degrade(authority, failure)


__all__ = [
    "InMemorySourceAnalysisStateRepository",
    "NoopSynthesisScheduler",
    "SQLAlchemySourceAuthorityReader",
    "SQLAlchemySourceAnalysisStateRepository",
    "SourceAnalysisAuthority",
    "SourceAnalysisState",
    "SourceContextTaskStatus",
    "SourceOrchestrator",
    "derive_source_authority",
]
