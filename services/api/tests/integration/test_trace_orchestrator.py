from __future__ import annotations

from uuid import UUID

import pytest

from perfpilot_api.engines.contracts import EngineStepOutcome
from perfpilot_api.workers.trace_orchestrator import TraceOrchestrator


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")


class FakeTraceExecutionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def advance(self, **kwargs: object) -> EngineStepOutcome:
        self.calls.append(kwargs)
        return EngineStepOutcome(EXECUTION_ID, "running", None)


@pytest.mark.asyncio
async def test_trace_orchestrator_runs_one_recoverable_database_backed_step() -> None:
    service = FakeTraceExecutionService()
    orchestrator = TraceOrchestrator(service=service)  # type: ignore[arg-type]

    outcome = await orchestrator.run_once(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome == EngineStepOutcome(EXECUTION_ID, "running", None)
    assert service.calls == [{"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID}]
