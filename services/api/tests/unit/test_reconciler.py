from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.services.agent_tasks import AgentExecutionAccess
from perfpilot_api.workers.reconciler import LeaseReconciliation, Reconciler

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")


def _result(outcome: str = "requeued") -> LeaseReconciliation:
    return LeaseReconciliation(
        access=AgentExecutionAccess(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            lease_expires_at=NOW - timedelta(seconds=1),
            scenario_types=("startup", "scroll", "memory_cycle"),
        ),
        outcome=outcome,  # type: ignore[arg-type]
        reconciled_at=NOW,
    )


class FakeRepository:
    def __init__(self, result: LeaseReconciliation | None) -> None:
        self.result = result
        self.projected: list[LeaseReconciliation] = []

    async def expire_one(self, *, now: datetime) -> LeaseReconciliation | None:
        assert now == NOW
        result, self.result = self.result, None
        return result

    async def project_reconciliation(
        self,
        *,
        reconciliation: LeaseReconciliation,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.projected.append(reconciliation)


class FakeArtifacts:
    def __init__(self) -> None:
        self.aborted: list[AgentExecutionAccess] = []

    async def abort_execution(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.aborted.append(access)


@pytest.mark.asyncio
async def test_reconciler_expires_then_cleans_and_projects_one_lease() -> None:
    repository = FakeRepository(_result())
    artifacts = FakeArtifacts()
    reconciler = Reconciler(
        repository=repository,
        artifact_coordinator=artifacts,
        clock=lambda: NOW,
    )

    result = await reconciler.run_once()
    empty = await reconciler.run_once()

    assert result == _result()
    assert empty is None
    assert artifacts.aborted == [result.access]
    assert repository.projected == [result]

