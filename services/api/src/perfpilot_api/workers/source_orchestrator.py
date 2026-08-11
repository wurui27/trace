"""Gate synthesis on bounded source context without making source a hard dependency."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import EngineExecution, GlobalJob
from perfpilot_api.reports.source_context import SourceContextValidationError
from perfpilot_api.services.source_artifacts import SourceArtifactError
from perfpilot_api.services.source_tasks import SourceContextTaskStatus, SourceTaskError


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


class SynthesisScheduler(Protocol):
    async def enqueue_once(self, *, team_id: UUID, analysis_id: UUID) -> None: ...


class InMemorySourceAnalysisStateRepository:
    def __init__(self) -> None:
        self._states: dict[UUID, SourceAnalysisState] = {}

    async def save(self, analysis_id: UUID, state: SourceAnalysisState) -> None:
        self._states[analysis_id] = replace(state)

    def get(self, analysis_id: UUID) -> SourceAnalysisState:
        return self._states.get(
            analysis_id,
            SourceAnalysisState(requested=False, context_state="not_requested"),
        )


class SQLAlchemySourceAuthorityReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

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
        return SourceAnalysisAuthority(
            team_id=job.team_id,
            analysis_id=job.id,
            smartperfetto_state="pending" if engine is None else engine.state,
            agent_id=job.source_agent_id,
            workspace_id=job.source_workspace_id,
            validation_profile_id=job.source_validation_profile_id,
            finding_hints=(),
            direct_identifiers=(),
            finding_ids=(),
            evidence_ids=(),
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
        states: InMemorySourceAnalysisStateRepository,
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
                        allowed_finding_ids=authority.finding_ids or None,
                        allowed_evidence_ids=authority.evidence_ids or None,
                    )
                    match = context.get("match_summary")
                    if match not in {"strong", "weak", "none"}:
                        raise SourceContextValidationError
                except (SourceArtifactError, SourceContextValidationError, ValueError):
                    return await self._degrade(authority, "source_context_invalid")
                await self._states.save(
                    analysis_id,
                    SourceAnalysisState(
                        requested=True,
                        context_state="available",
                        match_summary=match,  # type: ignore[arg-type]
                        artifact_id=status.artifact_id,
                        checksum=status.checksum,
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
    "SourceAnalysisAuthority",
    "SourceAnalysisState",
    "SourceContextTaskStatus",
    "SourceOrchestrator",
]
