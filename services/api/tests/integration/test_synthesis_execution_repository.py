from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from perfpilot_api.db.control.models import EngineExecution, SynthesisExecution, TenantResource
from perfpilot_api.db.control.models import OutboxEvent, WorkerClaim
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
    SynthesisIdempotencyConflictError,
    SynthesisMutationFence,
    SynthesisRequest,
)
from perfpilot_api.workers.synthesis_orchestrator import SQLAlchemySynthesisWorkQueue
from pydantic import SecretStr

# Reuse the real migrated PostgreSQL database fixture used by engine executions.
from test_engine_execution_repository import (  # type: ignore[import-not-found]
    ANALYSIS_ID,
    NOW,
    TEAM_ID,
    ExecutionDatabase,
    execution_database as _execution_database,  # noqa: F401
)


def _checksum(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


@pytest.fixture(name="database")
async def _database_fixture(
    _execution_database: ExecutionDatabase,  # noqa: F811
) -> ExecutionDatabase:
    return _execution_database


@pytest.mark.asyncio
async def test_automatic_generation_replays_authoritative_source(
    database: ExecutionDatabase,
) -> None:
    execution_database = database
    source_id = uuid4()
    async with execution_database.sessions.begin() as session:
        session.add(TenantResource(
            team_id=TEAM_ID, resource_version=7, state="active", provisioning_step="active",
            credential_version=1, retry_count=0, fencing_token=0, write_paused=False,
        ))
        session.add(EngineExecution(
            id=source_id, team_id=TEAM_ID, analysis_id=ANALYSIS_ID, engine_id="smartperfetto",
            attempt_number=1, tenant_resource_version=7, adapter_version="1.0.0",
            engine_commit_sha="a" * 40, engine_image_digest="sha256:" + "b" * 64,
            input_manifest_hash="c" * 64, config_hash="d" * 64, state="completed",
            raw_result_artifact_id=uuid4(), completed_at=NOW, version=3,
        ))
    checksum = _checksum(b"canonical")
    request = SynthesisRequest(
        canonical_sha256_b64=checksum, tenant_resource_version=7, question="why jank?",
        normalizer_version="smartperfetto-normalizer-1", prompt_template_version="perfpilot-synthesis-v1",
        prompt_template_sha256_b64=checksum, report_worker_image_digest="sha256:" + "d" * 64,
        provider_name="test", model="test-model", inference_config_hash="e" * 64,
        projection_sha256_b64=checksum, generation=1,
    )
    repository = SQLAlchemySynthesisExecutionRepository(
        execution_database.sessions, clock=lambda: NOW
    )

    first = await repository.allocate(team_id=TEAM_ID, analysis_id=ANALYSIS_ID, source_execution_id=source_id, request=request, now=NOW)
    replay = await repository.allocate(team_id=TEAM_ID, analysis_id=ANALYSIS_ID, source_execution_id=source_id, request=request, now=NOW)

    assert replay == first
    manual = replace(request, generation=2)
    second = await repository.allocate(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        source_execution_id=source_id,
        request=manual,
        now=NOW,
        mode="manual",
        idempotency_key="manual-generation-two",
    )
    assert await repository.allocate(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        source_execution_id=source_id,
        request=manual,
        now=NOW,
        mode="manual",
        idempotency_key="manual-generation-two",
    ) == second
    with pytest.raises(SynthesisIdempotencyConflictError):
        await repository.allocate(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            source_execution_id=source_id,
            request=manual,
            now=NOW,
            mode="manual",
            idempotency_key="another-key",
        )
    async with execution_database.sessions() as session:
        assert len((await session.scalars(select(SynthesisExecution))).all()) == 2


@pytest.mark.asyncio
async def test_valid_claim_completes_after_report_makes_synthesis_terminal(
    database: ExecutionDatabase,
) -> None:
    """A worker owns the requested event until it completes it, not until version one."""
    execution_database = database
    synthesis_id = uuid4()
    source_id = uuid4()
    event_id = uuid4()
    claim_id = uuid4()
    token = SecretStr("x" * 32)
    async with execution_database.sessions.begin() as session:
        session.add(EngineExecution(
            id=source_id, team_id=TEAM_ID, analysis_id=ANALYSIS_ID,
            engine_id="smartperfetto", attempt_number=1, tenant_resource_version=1,
            adapter_version="v1", engine_commit_sha="a" * 40,
            engine_image_digest="sha256:" + "b" * 64,
            input_manifest_hash="c" * 64, config_hash="d" * 64,
            state="completed", raw_result_artifact_id=uuid4(), completed_at=NOW, version=1,
        ))
        session.add(SynthesisExecution(
            id=synthesis_id, team_id=TEAM_ID, analysis_id=ANALYSIS_ID, source_execution_id=source_id,
            tenant_resource_version=1, generation=1, state="running", request_fingerprint="a" * 64,
            normalizer_version="v1", report_worker_image_digest="sha256:" + "a" * 64,
            projection_sha256_b64=_checksum(b"projection"), provider_protocol="p", provider_name="n",
            provider_model="m", prompt_template_version="v1", prompt_template_sha256_b64=_checksum(b"prompt"),
            attempt_count=1, version=1, started_at=NOW,
        ))
        session.add(OutboxEvent(id=event_id, team_id=TEAM_ID, global_job_id=ANALYSIS_ID,
            event_type="analysis_synthesis_requested", subject_type="synthesis_execution", subject_id=synthesis_id,
            subject_version=1, ready_at=NOW, retry_count=0, version=1))
        session.add(WorkerClaim(id=claim_id, global_job_id=ANALYSIS_ID, event_id=event_id,
            consumer_id="worker", token_digest=hashlib.sha256(token.get_secret_value().encode()).hexdigest(),
            state="active", expires_at=NOW.replace(year=NOW.year + 1), retry_count=0, version=1))
    repository = SQLAlchemySynthesisExecutionRepository(execution_database.sessions, clock=lambda: NOW)
    fence = SynthesisMutationFence(claim_id, event_id, "worker", token)
    stamped = await repository.bind_report_timestamp(team_id=TEAM_ID, analysis_id=ANALYSIS_ID, execution_id=synthesis_id, generated_at=NOW, fence=fence)
    await repository.bind_report(team_id=TEAM_ID, analysis_id=ANALYSIS_ID, execution_id=synthesis_id, report_version_id=uuid4(), now=NOW, fence=fence)
    queue = SQLAlchemySynthesisWorkQueue(execution_database.sessions, clock=lambda: NOW)
    await queue.complete(type("Claim", (), {"claim_id": claim_id, "event_id": event_id, "team_id": TEAM_ID, "analysis_id": ANALYSIS_ID, "synthesis_execution_id": synthesis_id, "consumer_id": "worker", "token": token})())
    assert stamped.report_generated_at == NOW
