from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from perfpilot_api.workers.dispatcher import DispatchableEvent, Dispatcher

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")


def _event(event_type: str = "analysis_queued") -> DispatchableEvent:
    return DispatchableEvent(
        event_id=EVENT_ID,
        team_id=TEAM_ID,
        global_job_id=ANALYSIS_ID,
        scenario_job_id=None,
        event_type=event_type,
        subject_type="analysis",
        subject_id=ANALYSIS_ID,
        subject_version=2,
        version=1,
    )


class FakeRepository:
    def __init__(self, event: DispatchableEvent) -> None:
        self.event = event
        self.published: list[UUID] = []
        self.dead_lettered: list[UUID] = []

    async def next_event(self, *, now: datetime) -> DispatchableEvent | None:
        assert now == NOW
        return None if self.published or self.dead_lettered else self.event

    async def mark_published(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.published.append(event.event_id)

    async def mark_dead_lettered(
        self,
        *,
        event: DispatchableEvent,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.dead_lettered.append(event.event_id)


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def publish(self, *, stream: str, envelope: dict[str, str]) -> None:
        self.calls.append((stream, envelope))


@pytest.mark.asyncio
async def test_dispatcher_routes_stable_envelope_and_marks_published() -> None:
    repository = FakeRepository(_event())
    publisher = FakePublisher()
    dispatcher = Dispatcher(repository=repository, publisher=publisher, clock=lambda: NOW)

    first = await dispatcher.run_once()
    second = await dispatcher.run_once()

    assert first == _event()
    assert second is None
    assert repository.published == [EVENT_ID]
    assert repository.dead_lettered == []
    assert publisher.calls == [
        (
            "perfpilot:schedule",
            {
                "schema_version": "1.0",
                "event_id": str(EVENT_ID),
                "team_id": str(TEAM_ID),
                "global_job_id": str(ANALYSIS_ID),
                "scenario_job_id": "",
                "event_type": "analysis_queued",
                "subject_type": "analysis",
                "subject_id": str(ANALYSIS_ID),
                "subject_version": "2",
            },
        )
    ]


@pytest.mark.asyncio
async def test_dispatcher_dead_letters_unknown_event_without_broadcasting() -> None:
    repository = FakeRepository(_event("unknown_private_event"))
    publisher = FakePublisher()
    dispatcher = Dispatcher(repository=repository, publisher=publisher, clock=lambda: NOW)

    result = await dispatcher.run_once()

    assert result == _event("unknown_private_event")
    assert publisher.calls == []
    assert repository.published == []
    assert repository.dead_lettered == [EVENT_ID]
