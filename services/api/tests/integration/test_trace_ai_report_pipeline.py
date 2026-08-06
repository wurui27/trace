from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from perfpilot_api.ai.openai_compatible import (
    AIProviderError,
    OpenAICompatibleSynthesisProvider,
)
from perfpilot_api.ai.prompt import load_synthesis_prompt
from perfpilot_api.config import Settings
from perfpilot_api.db.control.models import (
    AIInvocation,
    OutboxEvent,
    SynthesisExecution,
    TenantResource,
)
from perfpilot_api.db.tenant.models import Artifact, ReportVersion
from perfpilot_api.engines.contracts import EngineRunRef
from perfpilot_api.main import create_app
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.projection import AIProjection
from perfpilot_api.reports.writer import AnalysisReportWriter
from perfpilot_api.security.proxy_signature import sign_proxy_request
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.analyses import (
    AnalysisService,
    SynthesisRunConfiguration,
    SynthesisRunService,
)
from perfpilot_api.services.canonical_result_reader import CanonicalResultReader
from perfpilot_api.services.engine_executions import (
    EngineExecutionSeed,
    SQLAlchemyEngineExecutionRepository,
)
from perfpilot_api.services.engine_result_artifacts import (
    SQLAlchemyEngineResultArtifactRepository,
)
from perfpilot_api.services.synthesis_artifacts import (
    S3SynthesisArtifactStore,
    SQLAlchemySynthesisArtifactRepository,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
)
from perfpilot_api.services.uploads import (
    SQLAlchemyTenantBucketResolver,
    SQLAlchemyUploadRepository,
    TenantBucket,
    UploadDescriptor,
)
from perfpilot_api.workers.synthesis_orchestrator import (
    SQLAlchemyAutomaticSynthesisRequestFactory,
    SQLAlchemySynthesisAnalysisContextRepository,
    SQLAlchemySynthesisMemorySourceRepository,
    SQLAlchemySynthesisParentProjector,
    SQLAlchemySynthesisWorkQueue,
    SynthesisCoordinator,
    SynthesisOrchestrationWorker,
    SynthesisPipeline,
)
from perfpilot_api.workers.trace_orchestrator import (
    SQLAlchemyTraceWorkQueueRepository,
)

from test_analysis_repository import (  # type: ignore[import-not-found]
    ANALYSIS_ID,
    ARTIFACT_ID as TRACE_ARTIFACT_ID,
    CHECKSUM as TRACE_CHECKSUM,
    NOW,
    TEAM_ID,
    UPLOAD_ID as TRACE_UPLOAD_ID,
    USER_ID,
    AnalysisDatabases,
    _create_trace_analysis,
    analysis_databases as _analysis_databases,  # noqa: F401
)


ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "services/api/tests/fixtures"
BUCKET = "tenant-private-test"
PRIVATE_MARKER = "private-marker-must-never-leave-canonical-input"
PROXY_SECRET = "trace-ai-pipeline-proxy-secret"
ORIGIN = "https://console.example.com"


def _sha256_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


@pytest.fixture(name="databases")
async def _databases_fixture(
    _analysis_databases: AnalysisDatabases,  # noqa: F811
) -> AnalysisDatabases:
    return _analysis_databases


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 2) -> None:
        self.now += timedelta(seconds=seconds)


class FakeVersionedObjectStore:
    def __init__(self) -> None:
        self._objects: dict[tuple[str, str, str], tuple[bytes, str, str]] = {}
        self._counter = 0
        self.get_calls: list[dict[str, object]] = []
        self.put_calls: list[dict[str, object]] = []

    def seed(
        self,
        *,
        bucket: str,
        key: str,
        version_id: str,
        body: bytes,
        checksum: str,
    ) -> None:
        self._objects[(bucket, key, version_id)] = (
            body,
            checksum,
            "application/json",
        )

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self._counter += 1
        version_id = f"fake-version-{self._counter}"
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        checksum = str(kwargs["ChecksumSHA256"])
        assert isinstance(body, bytes)
        self._objects[(bucket, key, version_id)] = (
            body,
            checksum,
            str(kwargs["ContentType"]),
        )
        self.put_calls.append({**kwargs, "Body": b"<private-json>"})
        return {"VersionId": version_id, "ChecksumSHA256": checksum}

    def _metadata(self, **kwargs: object) -> dict[str, object]:
        identity = (
            str(kwargs["Bucket"]),
            str(kwargs["Key"]),
            str(kwargs["VersionId"]),
        )
        body, checksum, content_type = self._objects[identity]
        return {
            "VersionId": identity[2],
            "ChecksumSHA256": checksum,
            "ContentType": content_type,
            "ContentLength": len(body),
            "DeleteMarker": False,
        }

    def head_object(self, **kwargs: object) -> dict[str, object]:
        return self._metadata(**kwargs)

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.get_calls.append(dict(kwargs))
        metadata = self._metadata(**kwargs)
        identity = (
            str(kwargs["Bucket"]),
            str(kwargs["Key"]),
            str(kwargs["VersionId"]),
        )
        body = self._objects[identity][0]
        return {**metadata, "Body": io.BytesIO(body)}


def _canonical_result(
    *,
    analysis_id: UUID,
    execution_id: UUID,
    artifact_id: UUID,
    result_state: str,
) -> bytes:
    document = json.loads(
        (
            FIXTURES
            / "canonical_results/smartperfetto-result-contract-1.0.0.json"
        ).read_text(encoding="utf-8")
    )
    document["analysis_id"] = str(analysis_id)
    document["execution_id"] = str(execution_id)
    document["artifact_id"] = str(artifact_id)
    document["tenant_resource_version"] = 1
    document["engine"] = {
        "engine_id": "smartperfetto",
        "adapter_version": "1.0.0",
        "source_contract": "workspace-agent-v1",
        "source_commit_sha": "1" * 40,
        "image_digest": "sha256:" + "2" * 64,
    }
    document["attempt"] = {
        "number": 1,
        "input_manifest_hash": "3" * 64,
        "config_hash": "4" * 64,
    }
    document["result"]["state"] = result_state
    payload = document["result"]["payload"]
    payload["report"]["conversationTimeline"] = [PRIVATE_MARKER]
    payload["report"]["queryHistory"] = [PRIVATE_MARKER]
    document["result"]["payload_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return canonical_json_bytes(document)


def _candidate_for_projection(projection: dict[str, object]) -> dict[str, object]:
    scenarios = projection["scenarios"]
    assert isinstance(scenarios, list)
    scenario = next(
        item
        for item in scenarios
        if isinstance(item, dict) and item.get("findings") and item.get("metrics")
    )
    findings = scenario["findings"]
    metrics = scenario["metrics"]
    assert isinstance(findings, list) and isinstance(metrics, list)
    finding = findings[0]
    metric = metrics[0]
    assert isinstance(finding, dict) and isinstance(metric, dict)
    evidence_ids = finding["evidence_ids"]
    assert isinstance(evidence_ids, list) and evidence_ids
    return {
        "schema_version": "1.0",
        "executive_summary": "Measured launch work overlaps the critical path.",
        "top_findings": [
            {
                "finding_id": finding["finding_id"],
                "evidence_ids": evidence_ids,
                "user_impact": "The first screen appears later than expected.",
            }
        ],
        "recommendations": [
            {
                "priority": "p1",
                "title": "Move launch work",
                "action": "Defer nonessential work until after the first frame.",
                "expected_effect": "Reduce work on the launch-critical path.",
                "finding_ids": [finding["finding_id"]],
                "evidence_ids": evidence_ids,
            }
        ],
        "retest_plan": [
            {
                "mode": "verify_metric",
                "scenario_type": scenario["scenario_type"],
                "metric_ids": [metric["metric_id"]],
                "limitation_ids": [],
                "steps": "Repeat the same launch capture and compare the measured metric.",
                "success_condition": "meet_existing_threshold",
                "failure_condition": "threshold_missed",
            }
        ],
        "limitations": [],
    }


class FakeAuthService:
    async def authorize_team_request(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(user_id=USER_ID, team_id=TEAM_ID, role="team_admin")


def _settings() -> Settings:
    return Settings(
        app_env="test",
        proxy_secret=PROXY_SECRET,
        allowed_origins=[ORIGIN],
        _env_file=None,
        _secrets_dir=None,
    )


def _headers(*, target: str, request_id: str) -> dict[str, str]:
    signature = sign_proxy_request(
        PROXY_SECRET.encode(),
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=target.encode("ascii"),
        raw_query=b"",
        body=b"",
    )
    return {
        "x-perfpilot-proxy-timestamp": "1700000000",
        "x-perfpilot-proxy-signature": signature,
        "x-request-id": request_id,
        "origin": ORIGIN,
        "x-csrf-token": "csrf-token",
        "cookie": f"{COOKIE_NAME}=session-token",
    }


@pytest.mark.parametrize(
    (
        "source_state",
        "provider_mode",
        "expected_parent_state",
        "expected_ai_stage",
        "expected_provider_calls",
        "expected_candidate_artifacts",
        "expected_failure_code",
    ),
    [
        ("completed", "success", "completed", "completed", 1, 1, None),
        (
            "insufficient_data",
            "success",
            "partially_completed",
            "completed",
            1,
            1,
            None,
        ),
        ("completed", "rate_then_success", "completed", "completed", 2, 1, None),
        (
            "completed",
            "invalid_twice",
            "partially_completed",
            "failed",
            2,
            0,
            "ai_output_invalid",
        ),
        (
            "completed",
            "authentication_failure",
            "partially_completed",
            "failed",
            1,
            0,
            "ai_authentication_failed",
        ),
        (
            "completed",
            "projection_too_large",
            "partially_completed",
            "failed",
            0,
            0,
            "ai_projection_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_trace_result_reaches_private_ai_and_public_report(
    databases: AnalysisDatabases,
    caplog: pytest.LogCaptureFixture,
    source_state: str,
    provider_mode: str,
    expected_parent_state: str,
    expected_ai_stage: str,
    expected_provider_calls: int,
    expected_candidate_artifacts: int,
    expected_failure_code: str | None,
) -> None:
    async with databases.control_sessions.begin() as session:
        session.add(
            TenantResource(
                team_id=TEAM_ID,
                resource_version=1,
                state="active",
                provisioning_step="active",
                bucket_name=BUCKET,
                credential_version=1,
                retry_count=0,
                fencing_token=0,
                write_paused=False,
            )
        )
    await _create_trace_analysis(
        databases,
        question="Why is launch slow?",
    )
    tenant = TenantBucket(team_id=TEAM_ID, bucket=BUCKET, resource_version=1)
    uploads = SQLAlchemyUploadRepository(
        tenant_router=databases.tenant_router  # type: ignore[arg-type]
    )
    trace_slot = await uploads.reserve_slot(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        idempotency_key="input-trace",
        request_hash="9" * 64,
        descriptor=UploadDescriptor(
            "trace", "application/octet-stream", 4, TRACE_CHECKSUM
        ),
        artifact_id=TRACE_ARTIFACT_ID,
        upload_id=TRACE_UPLOAD_ID,
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/trace/{TRACE_UPLOAD_ID}",
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    await databases.repository.mark_trace_uploading(
        team_id=TEAM_ID, analysis_id=ANALYSIS_ID, now=NOW
    )
    await uploads.finalize_upload(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        upload_id=trace_slot.upload_id,
        expected_version=trace_slot.version,
        storage_version_id="trace-input-version-1",
        finalized_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(days=30),
    )
    await databases.repository.queue_trace_execution(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW + timedelta(minutes=1),
    )
    trace_queue = SQLAlchemyTraceWorkQueueRepository(
        session_factory=databases.control_sessions,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    trace_claim = await trace_queue.claim_next(consumer_id="trace-worker")
    assert trace_claim is not None

    engine_repository = SQLAlchemyEngineExecutionRepository(
        databases.control_sessions
    )
    pending = await engine_repository.allocate_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        seed=EngineExecutionSeed(
            engine_id="smartperfetto",
            tenant_resource_version=1,
            adapter_version="1.0.0",
            engine_commit_sha="1" * 40,
            engine_image_digest="sha256:" + "2" * 64,
            input_manifest_hash="3" * 64,
            config_hash="4" * 64,
        ),
        now=NOW + timedelta(minutes=2),
    )
    running = await engine_repository.mark_submitted(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=pending.id,
        expected_version=pending.version,
        run_ref=EngineRunRef(
            "smartperfetto", "session-local", "run-local", None, "workspace-local"
        ),
        now=NOW + timedelta(minutes=2),
    )
    finalization = await engine_repository.claim_finalization(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=running.id,
        now=NOW + timedelta(minutes=3),
    )
    assert finalization.is_owner
    canonical_artifact_id = finalization.record.raw_result_artifact_id
    assert canonical_artifact_id is not None
    canonical_bytes = _canonical_result(
        analysis_id=ANALYSIS_ID,
        execution_id=running.id,
        artifact_id=canonical_artifact_id,
        result_state=source_state,
    )
    canonical_checksum = _sha256_b64(canonical_bytes)
    canonical_key = (
        f"raw/analyses/{ANALYSIS_ID}/internal/engine-results/"
        f"{canonical_artifact_id}.json"
    )
    canonical_version_id = "canonical-version-1"
    async with databases.tenant_sessions.begin() as session:
        session.add(
            Artifact(
                id=canonical_artifact_id,
                analysis_id=ANALYSIS_ID,
                upload_id=canonical_artifact_id,
                idempotency_key=f"internal:engine_result:{running.id}",
                request_hash=hashlib.sha256(canonical_bytes).hexdigest(),
                artifact_kind="engine_result",
                mime_type="application/json",
                size_bytes=len(canonical_bytes),
                sha256_b64=canonical_checksum,
                object_key=canonical_key,
                version_id=canonical_version_id,
                state="finalized",
                finalized_at=NOW + timedelta(minutes=3),
                expires_at=NOW + timedelta(days=30),
                version=2,
            )
        )
    object_store = FakeVersionedObjectStore()
    object_store.seed(
        bucket=BUCKET,
        key=canonical_key,
        version_id=canonical_version_id,
        body=canonical_bytes,
        checksum=canonical_checksum,
    )
    completed_source = await engine_repository.finalize(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=running.id,
        expected_version=finalization.record.version,
        artifact_id=canonical_artifact_id,
        terminal_state=source_state,  # type: ignore[arg-type]
        schedule_synthesis=True,
        now=NOW + timedelta(minutes=3),
    )
    replayed_source = await engine_repository.finalize(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=running.id,
        expected_version=finalization.record.version,
        artifact_id=canonical_artifact_id,
        terminal_state=source_state,  # type: ignore[arg-type]
        schedule_synthesis=True,
        now=NOW + timedelta(minutes=3),
    )
    assert replayed_source == completed_source
    await trace_queue.complete(trace_claim)

    success_fixture = json.loads(
        (
            FIXTURES / "openai_compatible/synthesis-success.json"
        ).read_text(encoding="utf-8")
    )
    invalid_fixture = json.loads(
        (
            FIXTURES / "openai_compatible/synthesis-invalid-reference.json"
        ).read_text(encoding="utf-8")
    )
    provider_requests: list[dict[str, object]] = []

    def provider_handler(request: httpx.Request) -> httpx.Response:
        request_json = json.loads(request.content)
        provider_requests.append(request_json)
        if provider_mode == "rate_then_success" and len(provider_requests) == 1:
            return httpx.Response(429)
        if provider_mode == "authentication_failure":
            return httpx.Response(401)
        if provider_mode == "invalid_twice":
            return httpx.Response(200, json=invalid_fixture)
        messages = request_json["messages"]
        projection = json.loads(messages[1]["content"])
        envelope = copy.deepcopy(success_fixture)
        envelope["choices"][0]["message"]["content"] = json.dumps(
            _candidate_for_projection(projection),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return httpx.Response(200, json=envelope)

    provider_client = httpx.AsyncClient(
        transport=httpx.MockTransport(provider_handler),
        follow_redirects=False,
    )
    prompt = load_synthesis_prompt()
    provider = OpenAICompatibleSynthesisProvider(
        base_url=SecretStr("https://provider.example.com/openai/v1/"),
        model="fake-model",
        token=SecretStr("local-test-provider-token"),
        prompt=prompt,
        max_response_bytes=128 * 1024,
        client=provider_client,
    )
    clock = MutableClock(NOW + timedelta(minutes=4))
    bucket_resolver = SQLAlchemyTenantBucketResolver(
        session_factory=databases.control_sessions
    )
    synthesis_repository = SQLAlchemySynthesisExecutionRepository(
        databases.control_sessions, clock=clock
    )
    request_factory = SQLAlchemyAutomaticSynthesisRequestFactory(
        tenant_router=databases.tenant_router,  # type: ignore[arg-type]
        normalizer_version="smartperfetto-normalizer-1",
        prompt_template_version=prompt.version,
        prompt_template_sha256_b64=prompt.sha256_b64,
        report_worker_image_digest="sha256:" + "5" * 64,
        provider_name="local-fake",
        model="fake-model",
        inference_config_hash="6" * 64,
    )
    coordinator = SynthesisCoordinator(
        session_factory=databases.control_sessions,
        repository=synthesis_repository,
        request_factory=request_factory,
        clock=clock,
    )
    pipeline = SynthesisPipeline(
        repository=synthesis_repository,
        canonical_reader=CanonicalResultReader(
            artifact_repository=SQLAlchemyEngineResultArtifactRepository(
                tenant_router=databases.tenant_router  # type: ignore[arg-type]
            ),
            bucket_resolver=bucket_resolver,
            client=object_store,
        ),
        artifact_store=S3SynthesisArtifactStore(
            repository=SQLAlchemySynthesisArtifactRepository(
                tenant_router=databases.tenant_router  # type: ignore[arg-type]
            ),
            bucket_resolver=bucket_resolver,
            client=object_store,
            clock=clock,
        ),
        provider=provider,
        report_writer=AnalysisReportWriter(
            tenant_router=databases.tenant_router  # type: ignore[arg-type]
        ),
        analysis_contexts=SQLAlchemySynthesisAnalysisContextRepository(
            tenant_router=databases.tenant_router  # type: ignore[arg-type]
        ),
        memory_sources=SQLAlchemySynthesisMemorySourceRepository(
            session_factory=databases.control_sessions
        ),
        parent_projector=SQLAlchemySynthesisParentProjector(
            control_session_factory=databases.control_sessions,
            tenant_router=databases.tenant_router,  # type: ignore[arg-type]
        ),
        clock=clock,
        max_projection_bytes=(
            1024 if provider_mode == "projection_too_large" else 256 * 1024
        ),
    )
    def make_worker(worker_id: str) -> SynthesisOrchestrationWorker:
        return SynthesisOrchestrationWorker(
            coordinator=coordinator,
            queue=SQLAlchemySynthesisWorkQueue(
                databases.control_sessions, lease_seconds=30, clock=clock
            ),
            pipeline=pipeline,
            worker_id=worker_id,
            active_poll_seconds=1,
            failure_backoff_seconds=1,
            heartbeat_seconds=20,
        )

    worker_a = make_worker("synthesis-a")
    worker_b = make_worker("synthesis-b")
    first_results = await asyncio.gather(worker_a.run_once(), worker_b.run_once())
    assert any(first_results)
    for _ in range(12):
        clock.advance()
        await worker_a.run_once()
        current = await databases.repository.load_view(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            now=clock.now,
        )
        if current.state == expected_parent_state:
            break
    await provider_client.aclose()

    view = await databases.repository.load_view(
        team_id=TEAM_ID, analysis_id=ANALYSIS_ID, now=clock.now
    )
    expected_projection_artifacts = (
        0 if expected_failure_code == "ai_projection_invalid" else 1
    )
    assert view.state == expected_parent_state
    assert [(stage.stage, stage.state) for stage in view.stages] == [
        ("input_validation", "completed"),
        ("smartperfetto", "completed"),
        ("perfpilot_ai", expected_ai_stage),
        ("report", "completed"),
    ]
    assert len(provider_requests) == expected_provider_calls
    assert PRIVATE_MARKER not in json.dumps(provider_requests, ensure_ascii=False)
    assert len(object_store.put_calls) == (
        expected_projection_artifacts + expected_candidate_artifacts
    )
    assert all(call.get("VersionId") for call in object_store.get_calls)
    canonical_reads = [
        call for call in object_store.get_calls if call["Key"] == canonical_key
    ]
    assert canonical_reads
    assert {call["VersionId"] for call in canonical_reads} == {
        canonical_version_id
    }

    async with databases.tenant_sessions() as session:
        private_artifacts = list(
            (
                await session.scalars(
                    select(Artifact).where(
                        Artifact.artifact_kind.in_(
                            ("ai_projection", "ai_synthesis_result")
                        )
                    )
                )
            ).all()
        )
        reports = list(
            (
                await session.scalars(
                    select(ReportVersion).where(ReportVersion.report.is_not(None))
                )
            ).all()
        )
    assert (
        [row.artifact_kind for row in private_artifacts].count("ai_projection")
        == expected_projection_artifacts
    )
    assert (
        [row.artifact_kind for row in private_artifacts].count("ai_synthesis_result")
        == expected_candidate_artifacts
    )
    assert len(reports) == 1 and reports[0].report is not None
    assert reports[0].report["schema_version"] == "1.1"
    assert reports[0].report["state"] == expected_parent_state

    async with databases.control_sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SynthesisExecution))
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(AIInvocation))
            == expected_provider_calls
        )
        synthesis = await session.scalar(
            select(SynthesisExecution).where(
                SynthesisExecution.analysis_id == ANALYSIS_ID
            )
        )
        assert synthesis is not None
        assert synthesis.stable_error_code == expected_failure_code
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "engine_result_ready")
            )
            == 1
        )
        control_rows = [
            *(
                await session.scalars(
                    select(SynthesisExecution).where(
                        SynthesisExecution.analysis_id == ANALYSIS_ID
                    )
                )
            ).all(),
            *(
                await session.scalars(
                    select(AIInvocation).where(AIInvocation.analysis_id == ANALYSIS_ID)
                )
            ).all(),
        ]
    assert PRIVATE_MARKER not in repr([row.__dict__ for row in control_rows])
    assert PRIVATE_MARKER not in caplog.text

    analysis_service = AnalysisService(
        repository=databases.repository,
        upload_service=SimpleNamespace(),  # type: ignore[arg-type]
        clock=clock,
    )
    app = create_app(
        testing=True,
        settings_override=_settings(),
        auth_service=FakeAuthService(),  # type: ignore[arg-type]
        analysis_service=analysis_service,
        proxy_clock=lambda: 1_700_000_000,
    )
    report_target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/report"
    status_target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        status_response = await client.get(
            status_target,
            headers=_headers(target=status_target, request_id="trace-ai-status"),
        )
        report_response = await client.get(
            report_target,
            headers=_headers(target=report_target, request_id="trace-ai-report"),
        )
    assert status_response.status_code == 200
    assert [stage["state"] for stage in status_response.json()["stages"]] == [
        "completed",
        "completed",
        expected_ai_stage,
        "completed",
    ]
    assert report_response.status_code == 200
    public_report = report_response.json()
    assert public_report["schema_version"] == "1.1"
    assert public_report["synthesis"]["state"] == (
        "failed" if expected_failure_code is not None else "completed"
    )
    assert PRIVATE_MARKER not in report_response.text

    reruns = SynthesisRunService(
        control_session_factory=databases.control_sessions,
        tenant_router=databases.tenant_router,  # type: ignore[arg-type]
        execution_repository=synthesis_repository,
        configuration=SynthesisRunConfiguration(
            normalizer_version="smartperfetto-normalizer-1",
            prompt_template_version=prompt.version,
            prompt_template_sha256_b64=prompt.sha256_b64,
            report_worker_image_digest="sha256:" + "5" * 64,
            provider_name="local-fake",
            model="fake-model",
            inference_config_hash="6" * 64,
        ),
        clock=clock,
    )
    generation_two = await reruns.create(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="manual-generation-two",
    )
    assert generation_two.generation == 2
    still_readable = await databases.repository.load_report(
        team_id=TEAM_ID, analysis_id=ANALYSIS_ID
    )
    assert still_readable == public_report
    async with databases.control_sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SynthesisExecution))
            == 2
        )


@pytest.mark.asyncio
async def test_checked_in_refusal_is_a_permanent_protocol_failure() -> None:
    refusal = json.loads(
        (
            FIXTURES / "openai_compatible/synthesis-refusal.json"
        ).read_text(encoding="utf-8")
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=refusal)
        ),
        follow_redirects=False,
    ) as client:
        provider = OpenAICompatibleSynthesisProvider(
            base_url=SecretStr("https://provider.example.com/openai/v1/"),
            model="fake-model",
            token=SecretStr("local-test-provider-token"),
            prompt=load_synthesis_prompt(),
            max_response_bytes=128 * 1024,
            client=client,
        )
        with pytest.raises(AIProviderError) as exc_info:
            await provider.synthesize(
                AIProjection(
                    canonical_bytes=b'{"schema_version":"1.0"}',
                    sha256_b64="checksum",
                )
            )

    assert (exc_info.value.stable_code, exc_info.value.retryable) == (
        "ai_protocol_invalid",
        False,
    )
