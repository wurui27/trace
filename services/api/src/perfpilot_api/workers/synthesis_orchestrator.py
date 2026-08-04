"""Control-only coordinator and renewable work claims for AI synthesis."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID, uuid4, uuid5

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import EngineExecution, OutboxEvent, SynthesisExecution, WorkerClaim
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
    SynthesisExecutionRecord,
    SynthesisRequest,
)


_WORKER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_EVENT_NAMESPACE = UUID("9bf739f1-eafc-5ba6-a95b-09fe18c4c315")


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
                OutboxEvent.dead_lettered_at.is_(None), SynthesisExecution.state.in_(("pending", "running")),
                SynthesisExecution.team_id == OutboxEvent.team_id, SynthesisExecution.analysis_id == OutboxEvent.global_job_id,
                SynthesisExecution.version == OutboxEvent.subject_version, ~active).order_by(OutboxEvent.ready_at, OutboxEvent.id).with_for_update(of=OutboxEvent, skip_locked=True).limit(1))).first()
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
                or event.event_type != "analysis_synthesis_requested" or event.subject_type != "synthesis_execution" or event.subject_id != claim.synthesis_execution_id):
            raise SynthesisClaimLostError("synthesis claim was lost")
        if synthesis is None or (
            not allow_terminal and synthesis.state not in {"pending", "running"}
        ):
            raise SynthesisClaimLostError("synthesis work authority was lost")
        return row, event

    async def renew(self, claim: SynthesisWorkClaim) -> None:
        async with self._sessions.begin() as session:
            row, _ = await self._owned(session, claim)
            row.expires_at = self._clock() + self._lease
            row.version += 1

    async def complete(self, claim: SynthesisWorkClaim) -> None:
        now = self._clock()
        async with self._sessions.begin() as session:
            row, event = await self._owned(session, claim, allow_terminal=True)
            row.state, row.completed_at, row.version, row.updated_at = "completed", now, row.version + 1, now
            event.published_at, event.version, event.updated_at = now, event.version + 1, now


__all__ = ["SynthesisClaimLostError", "SynthesisCoordinator", "SynthesisWorkClaim", "SQLAlchemySynthesisWorkQueue", "analysis_synthesis_requested_event_id"]
