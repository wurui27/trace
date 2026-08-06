from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest

from perfpilot_api.engines.contracts import EngineRetryDirective, EngineStepOutcome
from perfpilot_api.services.device_kernel_executions import (
    DeviceKernelContext,
    DeviceKernelExecutionService,
)


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
TRACE_EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")
MEMORY_EXECUTION_ID = UUID("94000000-0000-4000-8000-000000000001")
CAPTURE_ID = UUID("95000000-0000-4000-8000-000000000001")


class FakeTraceService:
    def __init__(self, outcome: EngineStepOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def advance(self, **kwargs: object) -> EngineStepOutcome:
        self.calls.append(kwargs)
        return self.outcome


class FakeContextRepository:
    def __init__(self, context: DeviceKernelContext) -> None:
        self.context = context
        self.calls: list[dict[str, object]] = []

    async def load_context(self, **kwargs: object) -> DeviceKernelContext:
        self.calls.append(kwargs)
        return self.context


@dataclass(frozen=True)
class FakeManifest:
    capture_id: UUID


@dataclass(frozen=True)
class FakeCapture:
    manifest: FakeManifest


class FakeCaptureService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_device_capture(self, **kwargs: object) -> FakeCapture:
        self.calls.append(kwargs)
        return FakeCapture(FakeManifest(CAPTURE_ID))


class FakeMemoryService:
    def __init__(self, outcome: EngineStepOutcome | None = None) -> None:
        self.outcome = outcome or EngineStepOutcome(MEMORY_EXECUTION_ID, "running", None)
        self.calls: list[dict[str, object]] = []

    async def advance(self, **kwargs: object) -> EngineStepOutcome:
        self.calls.append(kwargs)
        return self.outcome


def _context(
    *,
    analysis_mode: str = "device",
    memory_scenario_state: str | None = "analyzing",
) -> DeviceKernelContext:
    return DeviceKernelContext(
        analysis_id=ANALYSIS_ID,
        analysis_mode=analysis_mode,
        memory_scenario_state=memory_scenario_state,
    )


def _service(
    *,
    trace_outcome: EngineStepOutcome,
    context: DeviceKernelContext | None = None,
    memory_outcome: EngineStepOutcome | None = None,
) -> tuple[
    DeviceKernelExecutionService,
    FakeContextRepository,
    FakeCaptureService,
    FakeMemoryService,
]:
    repository = FakeContextRepository(context or _context())
    captures = FakeCaptureService()
    memory = FakeMemoryService(memory_outcome)
    return (
        DeviceKernelExecutionService(
            repository=repository,
            trace_service=FakeTraceService(trace_outcome),  # type: ignore[arg-type]
            capture_service=captures,  # type: ignore[arg-type]
            memory_service=memory,  # type: ignore[arg-type]
        ),
        repository,
        captures,
        memory,
    )


@pytest.mark.asyncio
async def test_running_smartperfetto_does_not_start_memory_early() -> None:
    trace = EngineStepOutcome(TRACE_EXECUTION_ID, "running", None)
    service, repository, captures, memory = _service(trace_outcome=trace)

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == trace
    assert repository.calls == []
    assert captures.calls == []
    assert memory.calls == []


@pytest.mark.asyncio
async def test_retrying_smartperfetto_does_not_start_memory_early() -> None:
    trace = EngineStepOutcome(
        TRACE_EXECUTION_ID,
        "running",
        EngineRetryDirective(
            mode="reconnect",
            execution_id=TRACE_EXECUTION_ID,
            attempt_number=1,
            stable_error_code="worker_unavailable",
            retry_after_seconds=5,
        ),
    )
    service, repository, captures, memory = _service(trace_outcome=trace)

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == trace
    assert repository.calls == []
    assert captures.calls == []
    assert memory.calls == []


@pytest.mark.asyncio
async def test_trace_upload_stops_after_smartperfetto_terminal_result() -> None:
    trace = EngineStepOutcome(TRACE_EXECUTION_ID, "completed", None)
    service, repository, captures, memory = _service(
        trace_outcome=trace,
        context=_context(analysis_mode="trace_upload", memory_scenario_state=None),
    )

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == trace
    assert repository.calls == [{"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID}]
    assert captures.calls == []
    assert memory.calls == []


@pytest.mark.asyncio
async def test_device_runs_memory_after_smartperfetto_terminal_result() -> None:
    trace = EngineStepOutcome(TRACE_EXECUTION_ID, "completed", None)
    memory_outcome = EngineStepOutcome(MEMORY_EXECUTION_ID, "running", None)
    service, repository, captures, memory = _service(
        trace_outcome=trace,
        memory_outcome=memory_outcome,
    )

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == memory_outcome
    assert repository.calls == [{"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID}]
    assert captures.calls == [{"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID}]
    assert memory.calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "capture_id": CAPTURE_ID,
        }
    ]


@pytest.mark.parametrize("memory_state", ["failed", "canceled", "queued", None])
@pytest.mark.asyncio
async def test_unavailable_device_memory_evidence_finishes_as_partial_input(
    memory_state: str | None,
) -> None:
    trace = EngineStepOutcome(TRACE_EXECUTION_ID, "insufficient_data", None)
    service, _repository, captures, memory = _service(
        trace_outcome=trace,
        context=_context(memory_scenario_state=memory_state),
    )

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == trace
    assert captures.calls == []
    assert memory.calls == []


@pytest.mark.parametrize("trace_state", ["failed", "canceled"])
@pytest.mark.asyncio
async def test_failed_smartperfetto_does_not_start_memory(
    trace_state: str,
) -> None:
    trace = EngineStepOutcome(TRACE_EXECUTION_ID, trace_state, None)  # type: ignore[arg-type]
    service, repository, captures, memory = _service(trace_outcome=trace)

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == trace
    assert repository.calls == []
    assert captures.calls == []
    assert memory.calls == []
