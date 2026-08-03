"""Recoverable outbox worker for tenant-owned SmartPerfetto analyses."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import GlobalJob, OutboxEvent, WorkerClaim
from perfpilot_api.engines.contracts import EngineStepOutcome


_WORKER_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PARENT_TERMINAL_STATES = frozenset(
    {"completed", "partially_completed", "failed", "canceled"}
)
_ENGINE_TERMINAL_STATES = frozenset(
    {"completed", "insufficient_data", "failed", "canceled"}
)
_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


class TraceClaimError(RuntimeError):
    """A redacted direct-parent Trace claim failure."""


class TraceClaimLostError(TraceClaimError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("trace worker claim was lost")


class TraceQueueUnavailableError(TraceClaimError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("trace work queue is unavailable")


@dataclass(frozen=True, slots=True)
class TraceWorkClaim:
    claim_id: UUID
    event_id: UUID
    team_id: UUID
    analysis_id: UUID
    consumer_id: str
    token: SecretStr = field(repr=False)
    analysis_state: str
    expires_at: datetime


class TraceExecutionAdvancer(Protocol):
    async def advance(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome: ...


class TraceWorkQueue(Protocol):
    async def claim_next(self, *, consumer_id: str) -> TraceWorkClaim | None: ...

    async def renew(self, claim: TraceWorkClaim) -> None: ...

    async def complete(self, claim: TraceWorkClaim) -> None: ...

    async def reschedule(
        self,
        claim: TraceWorkClaim,
        *,
        delay_seconds: float,
    ) -> None: ...

    async def retry(
        self,
        claim: TraceWorkClaim,
        *,
        delay_seconds: float,
    ) -> None: ...


@dataclass(slots=True)
class TraceOrchestrator:
    """Advance one explicitly identified durable SmartPerfetto execution."""

    service: TraceExecutionAdvancer

    async def run_once(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome:
        return await self.service.advance(team_id=team_id, analysis_id=analysis_id)


class TraceOrchestrationWorker:
    """Claim outbox events and advance each execution under a renewable lease."""

    def __init__(
        self,
        *,
        queue: TraceWorkQueue,
        service: TraceExecutionAdvancer,
        worker_id: str,
        idle_poll_seconds: float = 1.0,
        active_poll_seconds: float = 2.0,
        failure_backoff_seconds: float = 5.0,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        if _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("trace worker identity is invalid")
        if min(
            idle_poll_seconds,
            active_poll_seconds,
            failure_backoff_seconds,
            heartbeat_seconds,
        ) <= 0:
            raise ValueError("trace worker intervals must be positive")
        self._queue = queue
        self._service = service
        self._worker_id = worker_id
        self._idle_poll_seconds = idle_poll_seconds
        self._active_poll_seconds = active_poll_seconds
        self._failure_backoff_seconds = failure_backoff_seconds
        self._heartbeat_seconds = heartbeat_seconds

    async def _heartbeat(self, claim: TraceWorkClaim) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self._queue.renew(claim)

    async def _advance(self, claim: TraceWorkClaim) -> EngineStepOutcome:
        advancement = asyncio.create_task(
            self._service.advance(
                team_id=claim.team_id,
                analysis_id=claim.analysis_id,
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            done, _pending = await asyncio.wait(
                (advancement, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
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
        if claim.analysis_state in _PARENT_TERMINAL_STATES:
            await self._queue.complete(claim)
            return True

        try:
            outcome = await self._advance(claim)
            if not isinstance(outcome, EngineStepOutcome):
                raise TraceQueueUnavailableError
            if outcome.retry is not None:
                retry_after = outcome.retry.retry_after_seconds
                if type(retry_after) is not int or retry_after < 1:
                    raise TraceQueueUnavailableError
                await self._queue.reschedule(
                    claim,
                    delay_seconds=min(max(retry_after, self._active_poll_seconds), 300),
                )
            elif outcome.state in _ENGINE_TERMINAL_STATES:
                await self._queue.complete(claim)
            elif outcome.state in {"pending", "running", "awaiting_user"}:
                await self._queue.reschedule(
                    claim,
                    delay_seconds=self._active_poll_seconds,
                )
            else:
                raise TraceQueueUnavailableError
        except TraceClaimLostError:
            return False
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except Exception:
            try:
                await self._queue.retry(
                    claim,
                    delay_seconds=self._failure_backoff_seconds,
                )
            except TraceClaimLostError:
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


class SQLAlchemyTraceWorkQueueRepository:
    """Serialize Trace outbox delivery through renewable control-database claims."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        lease_seconds: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_source: Callable[[], UUID] = uuid4,
        token_source: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ValueError("trace claim lease is invalid")
        self._session_factory = session_factory
        self._lease = timedelta(seconds=lease_seconds)
        self._clock = clock
        self._uuid_source = uuid_source
        self._token_source = token_source

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise TraceQueueUnavailableError
        return now

    @staticmethod
    def _digest(token: SecretStr) -> str:
        return hashlib.sha256(token.get_secret_value().encode("utf-8")).hexdigest()

    @staticmethod
    def _event_matches(
        event: OutboxEvent,
        job: GlobalJob,
    ) -> bool:
        return (
            event.team_id == job.team_id
            and event.global_job_id == job.id
            and event.scenario_job_id is None
            and event.event_type == "trace_analysis_ready"
            and event.subject_type == "analysis"
            and event.subject_id == job.id
            and event.ready_at is not None
            and event.published_at is None
            and event.dead_lettered_at is None
            and job.analysis_mode == "trace_upload"
            and job.state in _PARENT_TERMINAL_STATES | {"analyzing"}
        )

    def _new_token(self) -> SecretStr:
        token = self._token_source()
        if (
            not isinstance(token, str)
            or token != token.strip()
            or not 32 <= len(token) <= 512
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            raise TraceQueueUnavailableError
        return SecretStr(token)

    async def claim_next(self, *, consumer_id: str) -> TraceWorkClaim | None:
        if _WORKER_ID.fullmatch(consumer_id) is None:
            raise ValueError("trace worker identity is invalid")
        now = self._now()
        try:
            async with self._session_factory.begin() as session:
                trace_event_ids = select(OutboxEvent.id).where(
                    OutboxEvent.event_type == "trace_analysis_ready"
                )
                await session.execute(
                    update(WorkerClaim)
                    .where(
                        WorkerClaim.event_id.in_(trace_event_ids),
                        WorkerClaim.state == "active",
                        WorkerClaim.expires_at <= now,
                    )
                    .values(
                        state="expired",
                        version=WorkerClaim.version + 1,
                        updated_at=now,
                    )
                )
                await session.flush()

                active_claim = (
                    select(WorkerClaim.id)
                    .where(
                        WorkerClaim.global_job_id == GlobalJob.id,
                        WorkerClaim.state == "active",
                    )
                    .exists()
                )
                statement = (
                    select(OutboxEvent, GlobalJob)
                    .join(GlobalJob, GlobalJob.id == OutboxEvent.global_job_id)
                    .where(
                        OutboxEvent.event_type == "trace_analysis_ready",
                        OutboxEvent.subject_type == "analysis",
                        OutboxEvent.scenario_job_id.is_(None),
                        OutboxEvent.ready_at.is_not(None),
                        OutboxEvent.ready_at <= now,
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.dead_lettered_at.is_(None),
                        GlobalJob.analysis_mode == "trace_upload",
                        GlobalJob.state.in_(_PARENT_TERMINAL_STATES | {"analyzing"}),
                        ~active_claim,
                    )
                    .order_by(OutboxEvent.ready_at, OutboxEvent.created_at, OutboxEvent.id)
                    .with_for_update(of=OutboxEvent, skip_locked=True)
                    .limit(1)
                )
                selected = (await session.execute(statement)).first()
                if selected is None:
                    return None
                event, job = selected
                if not self._event_matches(event, job):
                    raise TraceQueueUnavailableError

                token = self._new_token()
                claim_id = self._uuid_source()
                expires_at = now + self._lease
                session.add(
                    WorkerClaim(
                        id=claim_id,
                        global_job_id=job.id,
                        scenario_job_id=None,
                        event_id=event.id,
                        consumer_id=consumer_id,
                        token_digest=self._digest(token),
                        state="active",
                        expires_at=expires_at,
                        completed_at=None,
                        retry_count=event.retry_count,
                        report_id=None,
                        version=1,
                    )
                )
                await session.flush()
                return TraceWorkClaim(
                    claim_id=claim_id,
                    event_id=event.id,
                    team_id=job.team_id,
                    analysis_id=job.id,
                    consumer_id=consumer_id,
                    token=token,
                    analysis_state=job.state,
                    expires_at=expires_at,
                )
        except TraceClaimError:
            raise
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except Exception:
            raise TraceQueueUnavailableError from None

    async def _owned_rows(
        self,
        session: AsyncSession,
        claim: TraceWorkClaim,
        *,
        now: datetime,
    ) -> tuple[WorkerClaim, OutboxEvent]:
        row = await session.scalar(
            select(WorkerClaim)
            .where(
                WorkerClaim.id == claim.claim_id,
                WorkerClaim.event_id == claim.event_id,
                WorkerClaim.global_job_id == claim.analysis_id,
                WorkerClaim.scenario_job_id.is_(None),
                WorkerClaim.consumer_id == claim.consumer_id,
            )
            .with_for_update()
        )
        event = await session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == claim.event_id,
                OutboxEvent.global_job_id == claim.analysis_id,
                OutboxEvent.team_id == claim.team_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or event is None
            or row.state != "active"
            or row.expires_at <= now
            or not hmac.compare_digest(row.token_digest, self._digest(claim.token))
            or event.event_type != "trace_analysis_ready"
            or event.subject_type != "analysis"
            or event.subject_id != claim.analysis_id
            or event.scenario_job_id is not None
            or event.published_at is not None
            or event.dead_lettered_at is not None
        ):
            raise TraceClaimLostError
        return row, event

    async def renew(self, claim: TraceWorkClaim) -> None:
        now = self._now()
        try:
            async with self._session_factory.begin() as session:
                row, _event = await self._owned_rows(session, claim, now=now)
                row.expires_at = now + self._lease
                row.version += 1
                row.updated_at = now
        except TraceClaimError:
            raise
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except Exception:
            raise TraceQueueUnavailableError from None

    @staticmethod
    def _delay(value: float) -> timedelta:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
            raise ValueError("trace queue delay is invalid")
        return timedelta(seconds=value)

    async def _finish(
        self,
        claim: TraceWorkClaim,
        *,
        delay: timedelta | None,
        retry: bool,
    ) -> None:
        now = self._now()
        try:
            async with self._session_factory.begin() as session:
                row, event = await self._owned_rows(session, claim, now=now)
                row.state = "expired" if retry else "completed"
                row.completed_at = None if retry else now
                row.version += 1
                row.updated_at = now
                event.version += 1
                event.updated_at = now
                if delay is None:
                    event.published_at = now
                else:
                    event.ready_at = now + delay
                    if retry:
                        event.retry_count += 1
        except TraceClaimError:
            raise
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except Exception:
            raise TraceQueueUnavailableError from None

    async def complete(self, claim: TraceWorkClaim) -> None:
        await self._finish(claim, delay=None, retry=False)

    async def reschedule(
        self,
        claim: TraceWorkClaim,
        *,
        delay_seconds: float,
    ) -> None:
        await self._finish(
            claim,
            delay=self._delay(delay_seconds),
            retry=False,
        )

    async def retry(
        self,
        claim: TraceWorkClaim,
        *,
        delay_seconds: float,
    ) -> None:
        await self._finish(
            claim,
            delay=self._delay(delay_seconds),
            retry=True,
        )


__all__ = [
    "SQLAlchemyTraceWorkQueueRepository",
    "TraceClaimError",
    "TraceClaimLostError",
    "TraceExecutionAdvancer",
    "TraceOrchestrationWorker",
    "TraceOrchestrator",
    "TraceQueueUnavailableError",
    "TraceWorkClaim",
    "TraceWorkQueue",
]
