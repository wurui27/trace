from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from perfpilot_api.db.control.models import (
    AIInvocation,
    EngineExecution,
    OutboxEvent,
    SynthesisExecution,
    WorkerClaim,
)
from perfpilot_api.db.control.models import GlobalJob
from perfpilot_api.db.tenant.models import Analysis, ReportVersion
from perfpilot_api.reports.writer import (
    AnalysisReportWriteRequest,
    compose_analysis_report,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
    SynthesisMutationFence,
)
from perfpilot_api.workers.synthesis_orchestrator import (
    SQLAlchemySynthesisParentProjector,
    SQLAlchemySynthesisWorkQueue,
)

from test_engine_execution_repository import (  # type: ignore[import-not-found]
    ANALYSIS_ID,
    NOW,
    TEAM_ID,
    ExecutionDatabase,
    execution_database as _execution_database,  # noqa: F401
)
from test_analysis_repository import (  # type: ignore[import-not-found]
    ANALYSIS_ID as PARENT_ANALYSIS_ID,
    NOW as PARENT_NOW,
    TEAM_ID as PARENT_TEAM_ID,
    AnalysisDatabases,
    analysis_databases as _analysis_databases,  # noqa: F401
)


ROOT = Path(__file__).resolve().parents[4]


def _checksum(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


@pytest.fixture(name="database")
async def _database_fixture(
    _execution_database: ExecutionDatabase,  # noqa: F811
) -> ExecutionDatabase:
    return _execution_database


@pytest.fixture(name="parent_databases")
async def _parent_database_fixture(
    _analysis_databases: AnalysisDatabases,  # noqa: F811
) -> AnalysisDatabases:
    return _analysis_databases


@dataclass
class Clock:
    now: object = NOW

    def __call__(self):
        return self.now


async def _seed(database: ExecutionDatabase) -> tuple[UUID, UUID, UUID]:
    source_id = uuid4()
    synthesis_id = uuid4()
    event_id = uuid4()
    async with database.sessions.begin() as session:
        session.add(
            EngineExecution(
                id=source_id,
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                engine_id="smartperfetto",
                attempt_number=1,
                tenant_resource_version=7,
                adapter_version="1.0.0",
                engine_commit_sha="a" * 40,
                engine_image_digest="sha256:" + "b" * 64,
                input_manifest_hash="c" * 64,
                config_hash="d" * 64,
                state="completed",
                raw_result_artifact_id=uuid4(),
                completed_at=NOW,
                version=1,
            )
        )
        session.add(
            SynthesisExecution(
                id=synthesis_id,
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                source_execution_id=source_id,
                tenant_resource_version=7,
                generation=1,
                state="pending",
                request_fingerprint="e" * 64,
                normalizer_version="smartperfetto-normalizer-1",
                report_worker_image_digest="sha256:" + "f" * 64,
                projection_sha256_b64=_checksum(b"projection"),
                provider_protocol="chat-completions-json-schema-v1",
                provider_name="fake",
                provider_model="fake-model",
                prompt_template_version="perfpilot-synthesis-v1",
                prompt_template_sha256_b64=_checksum(b"prompt"),
                attempt_count=0,
                version=1,
            )
        )
        session.add(
            OutboxEvent(
                id=event_id,
                team_id=TEAM_ID,
                global_job_id=ANALYSIS_ID,
                scenario_job_id=None,
                event_type="analysis_synthesis_requested",
                subject_type="synthesis_execution",
                subject_id=synthesis_id,
                subject_version=1,
                ready_at=NOW,
                retry_count=0,
                version=1,
            )
        )
    return source_id, synthesis_id, event_id


def _fence(claim) -> SynthesisMutationFence:
    return SynthesisMutationFence(
        claim.claim_id,
        claim.event_id,
        claim.consumer_id,
        claim.token,
    )


@pytest.mark.asyncio
async def test_rescheduled_event_reclaims_after_business_version_advances(
    database: ExecutionDatabase,
) -> None:
    _source_id, synthesis_id, _event_id = await _seed(database)
    clock = Clock()
    queue = SQLAlchemySynthesisWorkQueue(
        database.sessions, lease_seconds=30, clock=clock  # type: ignore[arg-type]
    )
    repository = SQLAlchemySynthesisExecutionRepository(
        database.sessions, clock=clock  # type: ignore[arg-type]
    )
    first = await queue.claim_next(consumer_id="worker-1")
    assert first is not None
    attempt = await repository.begin_invocation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        now=NOW,
        fence=_fence(first),
    )
    await repository.finish_invocation_failure(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        attempt_number=attempt,
        stable_error_code="ai_output_invalid",
        latency_ms=12,
        exhausted=False,
        generated_at=None,
        now=NOW,
        fence=_fence(first),
    )
    await queue.reschedule(first, delay_seconds=2)
    clock.now = NOW + timedelta(seconds=3)

    replay = await queue.claim_next(consumer_id="worker-2")

    assert replay is not None
    assert replay.synthesis_execution_id == synthesis_id
    async with database.sessions() as session:
        execution = await session.get(SynthesisExecution, synthesis_id)
        event = await session.get(OutboxEvent, replay.event_id)
        assert execution is not None and event is not None
        assert execution.version > event.subject_version


@pytest.mark.asyncio
async def test_candidate_report_and_queue_completion_are_durable_and_fenced(
    database: ExecutionDatabase,
) -> None:
    source_id, synthesis_id, event_id = await _seed(database)
    queue = SQLAlchemySynthesisWorkQueue(
        database.sessions, lease_seconds=30, clock=lambda: NOW
    )
    repository = SQLAlchemySynthesisExecutionRepository(
        database.sessions, clock=lambda: NOW
    )
    claim = await queue.claim_next(consumer_id="worker-1")
    assert claim is not None
    fence = _fence(claim)
    attempt = await repository.begin_invocation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        now=NOW,
        fence=fence,
    )
    candidate_id = uuid4()
    candidate_checksum = _checksum(b"candidate")
    bound = await repository.bind_candidate_result(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        attempt_number=attempt,
        artifact_id=candidate_id,
        sha256_b64=candidate_checksum,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        latency_ms=40,
        generated_at=NOW,
        now=NOW,
        fence=fence,
    )
    report_id = uuid4()
    await repository.bind_source_report(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        report_version_id=report_id,
        now=NOW,
        fence=fence,
    )
    completed = await repository.bind_report(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        report_version_id=report_id,
        synthesis_succeeded=True,
        now=NOW,
        fence=fence,
    )
    await queue.complete(claim)

    assert bound.report_generated_at == NOW
    assert completed.state == "succeeded"
    async with database.sessions() as session:
        source = await session.get(EngineExecution, source_id)
        invocation = await session.scalar(
            select(AIInvocation).where(
                AIInvocation.synthesis_execution_id == synthesis_id
            )
        )
        event = await session.get(OutboxEvent, event_id)
        work_claim = await session.get(WorkerClaim, claim.claim_id)
        assert source is not None and source.normalized_report_version_id == report_id
        assert invocation is not None and invocation.state == "succeeded"
        assert invocation.total_tokens == 30
        assert event is not None and event.published_at == NOW
        assert work_claim is not None and work_claim.report_id == report_id


@pytest.mark.asyncio
async def test_exhausted_ai_failure_stays_publishable_until_failed_report_is_bound(
    database: ExecutionDatabase,
) -> None:
    _source_id, synthesis_id, _event_id = await _seed(database)
    queue = SQLAlchemySynthesisWorkQueue(
        database.sessions, lease_seconds=30, clock=lambda: NOW
    )
    repository = SQLAlchemySynthesisExecutionRepository(
        database.sessions, clock=lambda: NOW
    )
    claim = await queue.claim_next(consumer_id="worker-1")
    assert claim is not None
    fence = _fence(claim)
    attempt = await repository.begin_invocation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        now=NOW,
        fence=fence,
    )
    exhausted = await repository.finish_invocation_failure(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        attempt_number=attempt,
        stable_error_code="ai_authentication_failed",
        latency_ms=None,
        exhausted=True,
        generated_at=NOW,
        now=NOW,
        fence=fence,
    )
    assert exhausted.state == "running"
    assert exhausted.stable_error_code == "ai_authentication_failed"
    report_id = uuid4()
    await repository.bind_source_report(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        report_version_id=report_id,
        now=NOW,
        fence=fence,
    )
    failed = await repository.bind_report(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=synthesis_id,
        report_version_id=report_id,
        synthesis_succeeded=False,
        now=NOW,
        fence=fence,
    )
    await queue.complete(claim)

    assert failed.state == "failed"
    assert failed.report_version_id == report_id


def _report_request(*, failed: bool, generation: int) -> AnalysisReportWriteRequest:
    core = json.loads(
        (ROOT / "contracts/v1/examples/normalized-trace-report.valid.json").read_text(
            encoding="utf-8"
        )
    )
    core["analysis_id"] = str(PARENT_ANALYSIS_ID)
    synthesis = json.loads(
        (ROOT / "contracts/v1/examples/synthesis-output.valid.json").read_text(
            encoding="utf-8"
        )
    )
    checksum = base64.b64encode(b"c" * 32).decode("ascii")
    return AnalysisReportWriteRequest(
        team_id=PARENT_TEAM_ID,
        analysis_id=PARENT_ANALYSIS_ID,
        synthesis_execution_id=UUID(
            f"a2000000-0000-4000-8000-{generation:012d}"
        ),
        tenant_resource_version=1,
        generation=generation,
        generated_at=PARENT_NOW + timedelta(minutes=generation),
        core_document=core,
        synthesis_document=None if failed else synthesis,
        synthesis_failure_code="synthesis_unavailable" if failed else None,
        canonical_artifact_id=UUID("85000000-0000-4000-8000-000000000001"),
        canonical_sha256_b64=checksum,
        projection_artifact_id=UUID("89000000-0000-4000-8000-000000000001"),
        projection_sha256_b64=checksum,
        synthesis_artifact_id=(
            None
            if failed
            else UUID("88000000-0000-4000-8000-000000000001")
        ),
        synthesis_sha256_b64=None if failed else checksum,
        normalizer_version="smartperfetto-normalizer-1",
        prompt_template_version="perfpilot-synthesis-v1",
        prompt_template_sha256_b64=base64.b64encode(b"p" * 32).decode("ascii"),
        report_worker_image_digest="sha256:" + "1" * 64,
        provider_protocol="chat-completions-json-schema-v1",
        provider_name="fake",
        model="fake-model",
        prompt_tokens=None if failed else 10,
        completion_tokens=None if failed else 20,
        total_tokens=None if failed else 30,
        latency_ms=None if failed else 40,
    )


@pytest.mark.asyncio
async def test_parent_remediation_promotes_only_failed_synthesis_partial_report(
    parent_databases: AnalysisDatabases,
) -> None:
    previous = compose_analysis_report(_report_request(failed=True, generation=1), report_version=1)
    replacement = compose_analysis_report(
        _report_request(failed=False, generation=2), report_version=2
    )
    async with parent_databases.control_sessions.begin() as session:
        session.add(
            GlobalJob(
                id=PARENT_ANALYSIS_ID,
                team_id=PARENT_TEAM_ID,
                idempotency_key="synthesis-parent",
                analysis_mode="trace_upload",
                state="partially_completed",
                retry_count=0,
                max_retries=2,
                started_at=PARENT_NOW,
                completed_at=PARENT_NOW,
                version=1,
            )
        )
    async with parent_databases.tenant_sessions.begin() as session:
        session.add(
            Analysis(
                id=PARENT_ANALYSIS_ID,
                application_version_id=None,
                analysis_mode="trace_upload",
                analysis_profile="auto",
                input_manifest=[],
                state="partially_completed",
                started_at=PARENT_NOW,
                completed_at=PARENT_NOW,
                version=1,
            )
        )
        session.add_all(
            (
                ReportVersion(
                    id=previous.id,
                    analysis_id=PARENT_ANALYSIS_ID,
                    scenario_result_id=None,
                    report_version=1,
                    state="partial",
                    generated_at=PARENT_NOW,
                    tool_version="writer",
                    rule_version="normalizer",
                    provenance={},
                    report=previous.document,
                    report_sha256_b64=previous.sha256_b64,
                ),
                ReportVersion(
                    id=replacement.id,
                    analysis_id=PARENT_ANALYSIS_ID,
                    scenario_result_id=None,
                    report_version=2,
                    state="complete",
                    generated_at=PARENT_NOW + timedelta(minutes=2),
                    tool_version="writer",
                    rule_version="normalizer",
                    provenance={},
                    report=replacement.document,
                    report_sha256_b64=replacement.sha256_b64,
                ),
            )
        )
    projector = SQLAlchemySynthesisParentProjector(
        control_session_factory=parent_databases.control_sessions,
        tenant_router=parent_databases.tenant_router,  # type: ignore[arg-type]
    )

    target = await projector.project(
        team_id=PARENT_TEAM_ID,
        analysis_id=PARENT_ANALYSIS_ID,
        tenant_resource_version=1,
        report_id=replacement.id,
        terminal="report",
        failure_code=None,
        now=PARENT_NOW + timedelta(minutes=3),
    )

    assert target == "completed"
    async with parent_databases.control_sessions() as session:
        job = await session.get(GlobalJob, PARENT_ANALYSIS_ID)
        assert job is not None and job.state == "completed"
    async with parent_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, PARENT_ANALYSIS_ID)
        assert analysis is not None and analysis.state == "completed"
