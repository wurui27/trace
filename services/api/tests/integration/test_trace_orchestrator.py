from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.engines.contracts import EngineStepOutcome
from perfpilot_api.workers.trace_orchestrator import (
    TraceOrchestrationWorker,
    TraceOrchestrator,
    TraceWorkClaim,
)


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")
EVENT_ID = UUID("94000000-0000-4000-8000-000000000001")
CLAIM_ID = UUID("95000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)


class FakeTraceExecutionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.outcome = EngineStepOutcome(EXECUTION_ID, "running", None)
        self.error: Exception | None = None

    async def advance(self, **kwargs: object) -> EngineStepOutcome:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.outcome


def _claim(*, analysis_state: str = "analyzing") -> TraceWorkClaim:
    return TraceWorkClaim(
        claim_id=CLAIM_ID,
        event_id=EVENT_ID,
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        consumer_id="trace-worker-1",
        token=SecretStr("claim-secret-must-stay-private"),
        analysis_state=analysis_state,
        expires_at=NOW + timedelta(seconds=30),
    )


class FakeTraceWorkQueue:
    def __init__(self, claim: TraceWorkClaim | None) -> None:
        self.claim = claim
        self.claim_calls: list[str] = []
        self.renewed: list[TraceWorkClaim] = []
        self.completed: list[TraceWorkClaim] = []
        self.rescheduled: list[tuple[TraceWorkClaim, float]] = []
        self.retried: list[tuple[TraceWorkClaim, float]] = []

    async def claim_next(self, *, consumer_id: str) -> TraceWorkClaim | None:
        self.claim_calls.append(consumer_id)
        claimed, self.claim = self.claim, None
        return claimed

    async def renew(self, claim: TraceWorkClaim) -> None:
        self.renewed.append(claim)

    async def complete(self, claim: TraceWorkClaim) -> None:
        self.completed.append(claim)

    async def reschedule(self, claim: TraceWorkClaim, *, delay_seconds: float) -> None:
        self.rescheduled.append((claim, delay_seconds))

    async def retry(self, claim: TraceWorkClaim, *, delay_seconds: float) -> None:
        self.retried.append((claim, delay_seconds))


@pytest.mark.asyncio
async def test_trace_orchestrator_runs_one_recoverable_database_backed_step() -> None:
    service = FakeTraceExecutionService()
    orchestrator = TraceOrchestrator(service=service)  # type: ignore[arg-type]

    outcome = await orchestrator.run_once(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == EngineStepOutcome(EXECUTION_ID, "running", None)
    assert service.calls == [{"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID}]


@pytest.mark.asyncio
async def test_worker_claims_one_ready_event_and_reschedules_a_running_execution() -> None:
    claim = _claim()
    queue = FakeTraceWorkQueue(claim)
    service = FakeTraceExecutionService()
    worker = TraceOrchestrationWorker(
        queue=queue,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        worker_id="trace-worker-1",
        active_poll_seconds=2,
        failure_backoff_seconds=5,
        heartbeat_seconds=10,
    )

    assert await worker.run_once()

    assert queue.claim_calls == ["trace-worker-1"]
    assert service.calls == [{"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID}]
    assert queue.rescheduled == [(claim, 2)]
    assert queue.completed == []
    assert "claim-secret-must-stay-private" not in repr(claim)


@pytest.mark.asyncio
async def test_worker_publishes_terminal_execution_and_skips_terminal_parent_replay() -> None:
    for parent_state, engine_state, expected_calls in (
        ("analyzing", "completed", 1),
        ("completed", "running", 0),
    ):
        claim = _claim(analysis_state=parent_state)
        queue = FakeTraceWorkQueue(claim)
        service = FakeTraceExecutionService()
        service.outcome = EngineStepOutcome(EXECUTION_ID, engine_state, None)  # type: ignore[arg-type]
        worker = TraceOrchestrationWorker(
            queue=queue,  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
            worker_id="trace-worker-1",
            active_poll_seconds=2,
            failure_backoff_seconds=5,
            heartbeat_seconds=10,
        )

        assert await worker.run_once()

        assert len(service.calls) == expected_calls
        assert queue.completed == [claim]
        assert queue.rescheduled == []


@pytest.mark.asyncio
async def test_worker_requeues_a_failed_step_without_publishing_the_event() -> None:
    claim = _claim()
    queue = FakeTraceWorkQueue(claim)
    service = FakeTraceExecutionService()
    service.error = RuntimeError("temporary dependency failure")
    worker = TraceOrchestrationWorker(
        queue=queue,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        worker_id="trace-worker-1",
        active_poll_seconds=2,
        failure_backoff_seconds=5,
        heartbeat_seconds=10,
    )

    assert await worker.run_once()

    assert queue.retried == [(claim, 5)]
    assert queue.completed == []
