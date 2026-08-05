from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from redis import asyncio as redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.config import get_settings
from perfpilot_api.db.control.models import OutboxEvent
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)

_ROUTES = {
    "analysis_queued": "perfpilot:schedule",
    "agent_execution_completed": "perfpilot:analysis",
    "agent_execution_canceled": "perfpilot:analysis",
}
# These event types are still consumed transactionally by their specialized workers. The
# dispatcher must not mark them published until those consumers move to Redis inboxes.
_DIRECT_CONSUMER_EVENTS = (
    "trace_analysis_ready",
    "engine_result_ready",
    "analysis_synthesis_requested",
)


@dataclass(frozen=True, slots=True)
class DispatchableEvent:
    event_id: UUID
    team_id: UUID
    global_job_id: UUID | None
    scenario_job_id: UUID | None
    event_type: str
    subject_type: str
    subject_id: UUID
    subject_version: int | None
    version: int


class DispatcherRepository(Protocol):
    async def next_event(self, *, now: datetime) -> DispatchableEvent | None: ...

    async def mark_published(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
    ) -> None: ...

    async def mark_dead_lettered(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
    ) -> None: ...


class EventPublisher(Protocol):
    async def publish(self, *, stream: str, envelope: dict[str, str]) -> None: ...


class SQLAlchemyDispatcherRepository:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._sessions = session_factory

    async def next_event(self, *, now: datetime) -> DispatchableEvent | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.ready_at.is_not(None),
                    OutboxEvent.ready_at <= now,
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.dead_lettered_at.is_(None),
                    OutboxEvent.event_type.not_in(_DIRECT_CONSUMER_EVENTS),
                )
                .order_by(OutboxEvent.ready_at, OutboxEvent.created_at, OutboxEvent.id)
                .limit(1)
            )
        return None if row is None else _dispatchable(row)

    async def mark_published(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
    ) -> None:
        await self._mark(event=event, now=now, published=True)

    async def mark_dead_lettered(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
    ) -> None:
        await self._mark(event=event, now=now, published=False)

    async def _mark(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
        published: bool,
    ) -> None:
        values: dict[str, object] = {
            "version": OutboxEvent.version + 1,
            "updated_at": now,
        }
        values["published_at" if published else "dead_lettered_at"] = now
        async with self._sessions() as session:
            async with session.begin():
                changed = await session.scalar(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event.event_id,
                        OutboxEvent.version == event.version,
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.dead_lettered_at.is_(None),
                    )
                    .values(**values)
                    .returning(OutboxEvent.id)
                )
                if changed is not None:
                    return
                current = await session.get(OutboxEvent, event.event_id)
                already_marked = current is not None and (
                    current.published_at is not None
                    if published
                    else current.dead_lettered_at is not None
                )
                if not already_marked:
                    raise RuntimeError("Outbox event changed during dispatch")


class RedisEventPublisher:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def publish(self, *, stream: str, envelope: dict[str, str]) -> None:
        await self._client.xadd(
            stream,
            envelope,
            maxlen=100_000,
            approximate=True,
        )


class Dispatcher:
    def __init__(
        self,
        *,
        repository: DispatcherRepository,
        publisher: EventPublisher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._clock = clock

    async def run_once(self) -> DispatchableEvent | None:
        now = _aware(self._clock())
        event = await self._repository.next_event(now=now)
        if event is None:
            return None
        stream = _ROUTES.get(event.event_type)
        if stream is None:
            await self._repository.mark_dead_lettered(event=event, now=now)
            return event
        await self._publisher.publish(
            stream=stream,
            envelope=_envelope(event),
        )
        await self._repository.mark_published(event=event, now=now)
        return event


class DispatcherWorker:
    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        idle_poll_seconds: float = 1.0,
        failure_backoff_seconds: float = 2.0,
    ) -> None:
        if idle_poll_seconds <= 0 or failure_backoff_seconds <= 0:
            raise ValueError("Dispatcher intervals must be positive")
        self._dispatcher = dispatcher
        self._idle_poll_seconds = idle_poll_seconds
        self._failure_backoff_seconds = failure_backoff_seconds

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            try:
                event = await self._dispatcher.run_once()
                delay = 0 if event is not None else self._idle_poll_seconds
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


def _dispatchable(row: OutboxEvent) -> DispatchableEvent:
    return DispatchableEvent(
        event_id=row.id,
        team_id=row.team_id,
        global_job_id=row.global_job_id,
        scenario_job_id=row.scenario_job_id,
        event_type=row.event_type,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        subject_version=row.subject_version,
        version=row.version,
    )


def _envelope(event: DispatchableEvent) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "event_id": str(event.event_id),
        "team_id": str(event.team_id),
        "global_job_id": "" if event.global_job_id is None else str(event.global_job_id),
        "scenario_job_id": "" if event.scenario_job_id is None else str(event.scenario_job_id),
        "event_type": event.event_type,
        "subject_type": event.subject_type,
        "subject_id": str(event.subject_id),
        "subject_version": "" if event.subject_version is None else str(event.subject_version),
    }


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Dispatcher clock must return an aware datetime")
    return value.astimezone(UTC)


async def _run() -> None:
    settings = get_settings()
    engine = create_control_engine(settings.control_database_url.get_secret_value())
    redis_client = redis.from_url(settings.redis_url.get_secret_value())
    try:
        dispatcher = Dispatcher(
            repository=SQLAlchemyDispatcherRepository(
                create_control_session_factory(engine)
            ),
            publisher=RedisEventPublisher(redis_client),
        )
        await DispatcherWorker(dispatcher=dispatcher).run_forever()
    finally:
        await redis_client.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish control outbox events")
    parser.parse_args(argv)
    asyncio.run(_run())


__all__ = [
    "DispatchableEvent",
    "Dispatcher",
    "DispatcherWorker",
    "RedisEventPublisher",
    "SQLAlchemyDispatcherRepository",
    "main",
]
