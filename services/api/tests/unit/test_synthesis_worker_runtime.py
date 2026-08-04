from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.workers.synthesis_orchestrator import (
    SynthesisOrchestrationWorker,
    SynthesisStepResult,
    SynthesisWorkClaim,
)
from perfpilot_api.workers.synthesis_runtime import (
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
