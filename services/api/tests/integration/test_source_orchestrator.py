from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from perfpilot_api.services.source_artifacts import SourceArtifactUnavailableError
from perfpilot_api.services.source_tasks import SourceCompletionArtifact
from perfpilot_api.services.source_workspaces import SourceBinding
from perfpilot_api.services.analyses import source_code_analysis_view
from perfpilot_api.workers.source_orchestrator import (
    InMemorySourceAnalysisStateRepository,
    SourceAnalysisAuthority,
    SourceContextTaskStatus,
    SourceOrchestrator,
    derive_source_authority,
)


NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("98000000-0000-4000-8000-000000000001")


class Authority:
    def __init__(self, value: SourceAnalysisAuthority) -> None:
        self.value = value

    async def load_source_authority(self, analysis_id: UUID):
        assert analysis_id == ANALYSIS_ID
        return self.value


class Tasks:
    def __init__(self, status: SourceContextTaskStatus | None = None) -> None:
        self.status = status
        self.created = 0

    async def context_status(self, *, team_id, analysis_id):
        return self.status

    async def create_context_task(self, **kwargs):
        self.created += 1
        self.status = SourceContextTaskStatus(
            execution_id=UUID("93000000-0000-4000-8000-000000000001"),
            state="queued",
            created_at=NOW,
            artifact_id=None,
            checksum=None,
            failure_code=None,
        )
        return object()


class Artifacts:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def read_context(self, **kwargs):
        if self.fail:
            raise SourceArtifactUnavailableError
        return {"snapshot_hash": "a" * 64, "match_summary": "strong", "fragments": []}

    async def persist_validated_context(self, **kwargs):
        return SourceCompletionArtifact(
            artifact_id=UUID("98000000-0000-4000-8000-000000000002"),
            checksum="d" * 64,
        )


class Scheduler:
    def __init__(self) -> None:
        self.calls = 0

    async def enqueue_once(self, *, team_id, analysis_id):
        self.calls += 1


def _authority(*, bound: bool = True, smart_state: str = "completed"):
    return SourceAnalysisAuthority(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        smartperfetto_state=smart_state,
        agent_id=AGENT_ID if bound else None,
        workspace_id=WORKSPACE_ID if bound else None,
        validation_profile_id=None,
        finding_hints=(),
        direct_identifiers=("demo.Startup.init",),
        finding_ids=(),
        evidence_ids=(),
    )


def test_authority_is_derived_from_canonical_normalized_smartperfetto_facts() -> None:
    report = json.loads(
        (
            Path(__file__).parents[4]
            / "contracts/v1/examples/normalized-trace-report.valid.json"
        ).read_text(encoding="utf-8")
    )
    report["scenario_reports"][0]["evidence"][0]["fields"]["mapped_symbol"] = (
        "demo.Startup.init"
    )

    derived = derive_source_authority(report)

    assert derived.direct_identifiers == ("demo.Startup.init",)
    assert derived.finding_ids == ("85000000-0000-4000-8000-000000000001",)
    assert derived.evidence_ids == ("86000000-0000-4000-8000-000000000001",)
    assert derived.finding_hints == (
        {
            "finding_id": "85000000-0000-4000-8000-000000000001",
            "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
            "rule_id": "startup.main_thread_binder",
            "symbol_hints": ["demo.Startup.init"],
        },
    )


def test_completed_task_is_not_available_until_durable_validation_succeeds() -> None:
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
    )
    completed = SimpleNamespace(
        state="completed",
        completion_artifact_id=ARTIFACT_ID,
        failure_code=None,
    )

    pending = source_code_analysis_view(binding, completed)  # type: ignore[arg-type]
    invalid = source_code_analysis_view(
        binding,
        completed,  # type: ignore[arg-type]
        durable_context_state="unavailable",
        durable_failure_code="source_context_invalid",
    )

    assert pending.context_state == "extracting"
    assert invalid.context_state == "unavailable"
    assert invalid.failure_code == "source_context_invalid"


@pytest.mark.asyncio
async def test_no_binding_skips_source_task_and_enqueues_synthesis_once() -> None:
    tasks, scheduler = Tasks(), Scheduler()
    states = InMemorySourceAnalysisStateRepository()
    orchestrator = SourceOrchestrator(
        authority=Authority(_authority(bound=False)),
        tasks=tasks,
        artifacts=Artifacts(),
        states=states,
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is True
    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is True
    assert tasks.created == 0
    assert scheduler.calls == 1
    assert states.get(ANALYSIS_ID).context_state == "not_requested"


@pytest.mark.asyncio
async def test_source_task_waits_until_smartperfetto_is_complete() -> None:
    tasks, scheduler = Tasks(), Scheduler()
    orchestrator = SourceOrchestrator(
        authority=Authority(_authority(smart_state="running")),
        tasks=tasks,
        artifacts=Artifacts(),
        states=InMemorySourceAnalysisStateRepository(),
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is False
    assert tasks.created == 0
    assert scheduler.calls == 0


@pytest.mark.asyncio
async def test_bound_analysis_creates_one_context_task_then_waits() -> None:
    tasks = Tasks()
    orchestrator = SourceOrchestrator(
        authority=Authority(_authority()),
        tasks=tasks,
        artifacts=Artifacts(),
        states=InMemorySourceAnalysisStateRepository(),
        scheduler=Scheduler(),
        clock=lambda: NOW,
    )

    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is False
    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is False
    assert tasks.created == 1


@pytest.mark.asyncio
async def test_completed_context_is_validated_saved_and_synthesis_is_idempotent() -> None:
    status = SourceContextTaskStatus(
        execution_id=UUID("93000000-0000-4000-8000-000000000001"),
        state="completed",
        created_at=NOW,
        artifact_id=ARTIFACT_ID,
        checksum="c" * 64,
        failure_code=None,
    )
    states, scheduler = InMemorySourceAnalysisStateRepository(), Scheduler()
    orchestrator = SourceOrchestrator(
        authority=Authority(_authority()),
        tasks=Tasks(status),
        artifacts=Artifacts(),
        states=states,
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is True
    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is True
    assert scheduler.calls == 1
    assert states.get(ANALYSIS_ID).context_state == "available"
    assert states.get(ANALYSIS_ID).match_summary == "strong"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "artifact_fail", "failure_code"),
    [
        (
            SourceContextTaskStatus(
                execution_id=UUID("93000000-0000-4000-8000-000000000001"),
                state="running",
                created_at=NOW - timedelta(seconds=121),
                artifact_id=None,
                checksum=None,
                failure_code=None,
            ),
            False,
            "source_context_timeout",
        ),
        (
            SourceContextTaskStatus(
                execution_id=UUID("93000000-0000-4000-8000-000000000001"),
                state="completed",
                created_at=NOW,
                artifact_id=ARTIFACT_ID,
                checksum="c" * 64,
                failure_code=None,
            ),
            True,
            "source_context_invalid",
        ),
    ],
)
async def test_source_failure_or_deadline_degrades_without_blocking_ai(
    status: SourceContextTaskStatus,
    artifact_fail: bool,
    failure_code: str,
) -> None:
    states, scheduler = InMemorySourceAnalysisStateRepository(), Scheduler()
    orchestrator = SourceOrchestrator(
        authority=Authority(_authority()),
        tasks=Tasks(status),
        artifacts=Artifacts(fail=artifact_fail),
        states=states,
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is True
    assert scheduler.calls == 1
    assert states.get(ANALYSIS_ID).context_state == "unavailable"
    assert states.get(ANALYSIS_ID).failure_code == failure_code
