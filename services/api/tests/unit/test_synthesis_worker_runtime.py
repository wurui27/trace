from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.workers import synthesis_runtime
from perfpilot_api.db.tenant.router import TenantRouteError

from perfpilot_api.workers.synthesis_orchestrator import (
    SynthesisClaimLostError,
    SynthesisOrchestrationWorker,
    SynthesisStepResult,
    SynthesisWorkClaim,
)
from perfpilot_api.workers.synthesis_runtime import (
    SynthesisRuntimeInputs,
    SynthesisWorkerRuntime,
    SynthesisWorkerRuntimeError,
    validate_synthesis_runtime_environment,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "PERFPILOT_SYNTHESIS_WORKER_ID": "synthesis-1",
        "PERFPILOT_REPORT_WORKER_IMAGE_DIGEST": "sha256:" + "1" * 64,
        "PERFPILOT_AI_CREDENTIAL_FILE": "/run/secrets/perfpilot-ai",
        "PERFPILOT_ENGINE_LOCK_FILE": "/app/engines.lock.json",
        "PERFPILOT_ENGINE_LOCK_SCHEMA_FILE": "/app/engines.lock.schema.json",
        "PERFPILOT_AI_EGRESS_ALLOWLIST": "provider.example.com",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_runtime_environment_requires_pinned_worker_secret_and_provider_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch)
    settings = SimpleNamespace(
        app_env="production",
        ai_enabled=True,
        ai_base_url=SecretStr("https://provider.example.com/openai/v1/"),
    )

    inputs = validate_synthesis_runtime_environment(settings)  # type: ignore[arg-type]

    assert inputs.worker_id == "synthesis-1"
    assert inputs.credential_path == Path("/run/secrets/perfpilot-ai")
    assert inputs.provider_host == "provider.example.com"

    monkeypatch.setenv("PERFPILOT_AI_EGRESS_ALLOWLIST", "other.example.com")
    with pytest.raises(SynthesisWorkerRuntimeError, match="egress"):
        validate_synthesis_runtime_environment(settings)  # type: ignore[arg-type]


def test_runtime_environment_rejects_nonproduction_or_unpinned_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch)
    settings = SimpleNamespace(
        app_env="development",
        ai_enabled=True,
        ai_base_url=SecretStr("https://provider.example.com/openai/v1/"),
    )
    with pytest.raises(SynthesisWorkerRuntimeError, match="production"):
        validate_synthesis_runtime_environment(settings)  # type: ignore[arg-type]

    settings.app_env = "production"
    monkeypatch.setenv("PERFPILOT_REPORT_WORKER_IMAGE_DIGEST", "latest")
    with pytest.raises(SynthesisWorkerRuntimeError, match="identity"):
        validate_synthesis_runtime_environment(settings)  # type: ignore[arg-type]


def _claim() -> SynthesisWorkClaim:
    return SynthesisWorkClaim(
        claim_id=UUID("31000000-0000-4000-8000-000000000001"),
        event_id=UUID("32000000-0000-4000-8000-000000000001"),
        team_id=UUID("33000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("34000000-0000-4000-8000-000000000001"),
        synthesis_execution_id=UUID("35000000-0000-4000-8000-000000000001"),
        consumer_id="synthesis-1",
        token=SecretStr("x" * 32),
        expires_at=NOW.replace(year=2027),
    )


class FakeQueue:
    def __init__(self) -> None:
        self.claim = _claim()
        self.actions: list[tuple[str, float | None]] = []

    async def claim_next(self, *, consumer_id: str):
        assert consumer_id == "synthesis-1"
        claim, self.claim = self.claim, None
        return claim

    async def renew(self, _claim) -> None:
        self.actions.append(("renew", None))

    async def complete(self, _claim) -> None:
        self.actions.append(("complete", None))

    async def reschedule(self, _claim, *, delay_seconds: float) -> None:
        self.actions.append(("reschedule", delay_seconds))

    async def retry(self, _claim, *, delay_seconds: float) -> None:
        self.actions.append(("retry", delay_seconds))


class FakePipeline:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome

    async def advance(self, _claim):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeCoordinator:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def coordinate_next(self) -> object:
        self.actions.append("coordinate")
        return object()


class EmptyQueue:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def claim_next(self, *, consumer_id: str):
        assert consumer_id == "synthesis-1"
        self.actions.append("claim")
        return None


class LostCoordinator:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def coordinate_next(self) -> object:
        self.actions.append("coordinate")
        raise SynthesisClaimLostError("stale source event")


class TenantUnavailableCoordinator:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def coordinate_next(self) -> object:
        self.actions.append("coordinate")
        raise TenantRouteError()


@pytest.mark.asyncio
async def test_worker_coordinates_engine_results_before_claiming_synthesis_work() -> None:
    actions: list[str] = []
    worker = SynthesisOrchestrationWorker(
        coordinator=FakeCoordinator(actions),  # type: ignore[arg-type]
        queue=EmptyQueue(actions),  # type: ignore[arg-type]
        pipeline=FakePipeline(SynthesisStepResult("succeeded")),  # type: ignore[arg-type]
        worker_id="synthesis-1",
    )

    assert await worker.run_once() is True
    assert actions == ["coordinate", "claim"]


@pytest.mark.asyncio
async def test_stale_source_event_does_not_block_synthesis_queue_claims() -> None:
    actions: list[str] = []
    worker = SynthesisOrchestrationWorker(
        coordinator=LostCoordinator(actions),  # type: ignore[arg-type]
        queue=EmptyQueue(actions),  # type: ignore[arg-type]
        pipeline=FakePipeline(SynthesisStepResult("succeeded")),  # type: ignore[arg-type]
        worker_id="synthesis-1",
    )

    assert await worker.run_once() is False
    assert actions == ["coordinate", "claim"]


@pytest.mark.asyncio
async def test_tenant_route_failure_does_not_block_synthesis_queue_claims() -> None:
    actions: list[str] = []
    worker = SynthesisOrchestrationWorker(
        coordinator=TenantUnavailableCoordinator(actions),  # type: ignore[arg-type]
        queue=EmptyQueue(actions),  # type: ignore[arg-type]
        pipeline=FakePipeline(SynthesisStepResult("succeeded")),  # type: ignore[arg-type]
        worker_id="synthesis-1",
    )

    assert await worker.run_once() is False
    assert actions == ["coordinate", "claim"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "action"),
    [
        (SynthesisStepResult("running"), ("reschedule", 2)),
        (SynthesisStepResult("pending", 7), ("reschedule", 7)),
        (SynthesisStepResult("succeeded"), ("complete", None)),
        (SynthesisStepResult("failed"), ("complete", None)),
        (RuntimeError("temporary"), ("retry", 5)),
    ],
)
async def test_worker_reschedules_progress_and_completes_all_terminal_results(
    outcome: object,
    action: tuple[str, float | None],
) -> None:
    queue = FakeQueue()
    worker = SynthesisOrchestrationWorker(
        queue=queue,  # type: ignore[arg-type]
        pipeline=FakePipeline(outcome),  # type: ignore[arg-type]
        worker_id="synthesis-1",
        active_poll_seconds=2,
        failure_backoff_seconds=5,
        heartbeat_seconds=30,
    )

    assert await worker.run_once() is True
    assert queue.actions == [action]


@pytest.mark.asyncio
async def test_runtime_closes_owned_components_in_reverse_order() -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def second() -> None:
        order.append("second")

    runtime = SynthesisWorkerRuntime(
        worker=SimpleNamespace(),  # type: ignore[arg-type]
        close_callbacks=(first, second),
    )

    await runtime.close()
    await runtime.close()

    assert order == ["second", "first"]


@pytest.mark.asyncio
async def test_production_builder_wires_automatic_synthesis_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        control_database_url=SecretStr("postgresql+psycopg://control"),
        ai_base_url=SecretStr("https://provider.example.com/openai/v1/"),
        ai_provider_name="provider",
        ai_model="model",
        ai_connect_timeout_seconds=1.0,
        ai_read_timeout_seconds=2.0,
        ai_write_timeout_seconds=3.0,
        ai_pool_timeout_seconds=4.0,
        ai_max_projection_bytes=2048,
        ai_max_response_bytes=4096,
    )
    inputs = SynthesisRuntimeInputs(
        worker_id="synthesis-1",
        report_worker_image_digest="sha256:" + "1" * 64,
        credential_path=Path("/run/secrets/ai"),
        engine_lock_path=Path("/app/engines.lock.json"),
        engine_lock_schema_path=Path("/app/engines.lock.schema.json"),
        provider_host="provider.example.com",
    )
    sessions = object()
    tenant_router = object()
    captured: dict[str, object] = {}

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    class FakeArtifacts:
        s3_client = object()

        def __init__(self) -> None:
            self.tenant_router = tenant_router

        async def close(self) -> None:
            return None

    class FakeClient:
        async def aclose(self) -> None:
            return None

    async def build_artifacts(**_kwargs):
        return FakeArtifacts()

    def fake_component(*_args, **kwargs):
        return SimpleNamespace(**kwargs)

    def fake_coordinator(*_args, **kwargs):
        captured["coordinator"] = kwargs
        return "automatic-coordinator"

    def fake_pipeline(*_args, **kwargs):
        captured["pipeline"] = kwargs
        return SimpleNamespace()

    def fake_worker(*_args, **kwargs):
        captured["worker"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(synthesis_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(
        synthesis_runtime,
        "validate_synthesis_runtime_environment",
        lambda _settings: inputs,
    )
    monkeypatch.setattr(synthesis_runtime, "load_engine_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(synthesis_runtime, "_load_token", lambda _path: SecretStr("x" * 32))
    monkeypatch.setattr(synthesis_runtime, "create_control_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(
        synthesis_runtime, "create_control_session_factory", lambda _engine: sessions
    )
    monkeypatch.setattr(synthesis_runtime, "build_artifact_runtime", build_artifacts)
    monkeypatch.setattr(synthesis_runtime.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    for name in (
        "SQLAlchemyTenantBucketResolver",
        "CanonicalResultReader",
        "SQLAlchemyEngineResultArtifactRepository",
        "S3SynthesisArtifactStore",
        "SQLAlchemySynthesisArtifactRepository",
        "OpenAICompatibleSynthesisProvider",
        "SQLAlchemySynthesisExecutionRepository",
        "AnalysisReportWriter",
        "SQLAlchemySynthesisAnalysisContextRepository",
        "SQLAlchemySynthesisMemorySourceRepository",
        "SQLAlchemySynthesisParentProjector",
        "SQLAlchemySynthesisWorkQueue",
    ):
        monkeypatch.setattr(synthesis_runtime, name, fake_component)
    monkeypatch.setattr(synthesis_runtime, "SynthesisPipeline", fake_pipeline)
    monkeypatch.setattr(
        synthesis_runtime,
        "load_synthesis_prompt",
        lambda: SimpleNamespace(version="prompt-v1", sha256_b64="p" * 44),
    )
    monkeypatch.setattr(
        synthesis_runtime,
        "SQLAlchemyAutomaticSynthesisRequestFactory",
        fake_component,
        raising=False,
    )
    monkeypatch.setattr(
        synthesis_runtime, "SynthesisCoordinator", fake_coordinator, raising=False
    )
    monkeypatch.setattr(
        synthesis_runtime, "SynthesisOrchestrationWorker", fake_worker
    )

    runtime = await synthesis_runtime.build_production_synthesis_worker()

    assert runtime.worker is not None
    assert captured["worker"]["coordinator"] == "automatic-coordinator"  # type: ignore[index]
    assert captured["coordinator"]["session_factory"] is sessions  # type: ignore[index]
    assert captured["pipeline"]["max_projection_bytes"] == 2048  # type: ignore[index]
    assert captured["pipeline"]["memory_sources"].session_factory is sessions  # type: ignore[index,union-attr]
    source_gate = captured["coordinator"]["source_gate"]  # type: ignore[index]
    source_orchestrator = source_gate.__self__
    assert type(source_orchestrator._states).__name__ == (
        "SQLAlchemySourceAnalysisStateRepository"
    )
    assert source_orchestrator._authority._canonical_reader is not None
