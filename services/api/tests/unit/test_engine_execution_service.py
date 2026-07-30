from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from perfpilot_api.config import Settings
from perfpilot_api.engines.contracts import (
    AdapterDescriptor,
    EngineEvent,
    EngineEventBatch,
    EngineInput,
    EngineResult,
    EngineRunRef,
    EngineStatus,
    SubmitConfig,
)
from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.lock import EngineLock, EnginePin
from perfpilot_api.engines.registry import AdapterRegistry, AdapterRegistryError
from perfpilot_api.services.engine_executions import (
    EngineExecutionRecord,
    EngineExecutionService,
    FinalizationClaim,
    RetryReservation,
    build_engine_execution_service,
    build_smartperfetto_execution_service,
    result_artifact_id,
)
from perfpilot_api.services.engine_workspaces import EngineWorkspaceRecord


TEAM_ID = UUID("e1000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("e2000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("e3000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _record(**overrides: object) -> EngineExecutionRecord:
    values: dict[str, object] = {
        "id": EXECUTION_ID,
        "analysis_id": ANALYSIS_ID,
        "team_id": TEAM_ID,
        "engine_id": "smartperfetto",
        "attempt_number": 1,
        "adapter_version": "1.0.0",
        "engine_commit_sha": "a" * 40,
        "engine_image_digest": "sha256:" + "b" * 64,
        "input_manifest_hash": "c" * 64,
        "config_hash": "d" * 64,
        "external_workspace_id": None,
        "external_session_id": None,
        "external_run_id": None,
        "state": "pending",
        "last_event_cursor": None,
        "stable_error_code": None,
        "started_at": None,
        "completed_at": None,
        "raw_result_artifact_id": None,
        "normalized_report_version_id": None,
        "version": 1,
    }
    values.update(overrides)
    return EngineExecutionRecord(**values)  # type: ignore[arg-type]


def _run_ref(*, cursor: str | None = None) -> EngineRunRef:
    return EngineRunRef(
        "smartperfetto",
        "session-1",
        "run-1",
        cursor,
        "workspace-server-owned",
    )


class FakeWorkspaceService:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def ensure_workspace(self, *, team_id: UUID) -> EngineWorkspaceRecord:
        self.calls.append(team_id)
        return EngineWorkspaceRecord(
            id=UUID("e4000000-0000-4000-8000-000000000001"),
            team_id=team_id,
            engine_id="smartperfetto",
            external_workspace_id="workspace-server-owned",
            state="active",
            version=2,
        )


class FakeAdapter:
    descriptor = AdapterDescriptor(
        engine_id="smartperfetto",
        adapter_version="1.0.0",
        profiles=frozenset({"auto", "startup", "scroll"}),
        required_inputs=frozenset({"trace"}),
        optional_inputs=frozenset(),
        accepted_contracts=frozenset({"workspace-agent-v1"}),
        default_timeout_seconds=1800,
        resource_profile="network_service",
        stable_error_codes=frozenset(
            {
                "capacity_exceeded",
                "engine_interaction_required",
                "engine_unavailable",
            }
        ),
    )

    def __init__(self) -> None:
        self.submitted: list[SubmitConfig] = []
        self.batch = EngineEventBatch(
            run_ref=EngineRunRef(
                "smartperfetto",
                "session-1",
                "run-1",
                "1",
                "workspace-server-owned",
            ),
            events=(EngineEvent("1", "running", 25, "engine_progress", NOW),),
        )
        self.stream_error: EngineAdapterError | None = None
        self.result = EngineResult(
            "workspace-agent-v1",
            "completed",
            {"report": {"summary": {"conclusion": "usable"}}},
        )
        self.fetch_error: EngineAdapterError | None = None
        self.cancel_calls = 0
        self.cancel_refs: list[EngineRunRef] = []
        self.status_value = "running"

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef:
        self.submitted.append(config)
        return EngineRunRef(
            "smartperfetto",
            "session-1",
            "run-1",
            None,
            config.external_workspace_id,
        )

    async def stream(
        self,
        run_ref: EngineRunRef,
        cursor: str | None,
    ) -> EngineEventBatch:
        if self.stream_error is not None:
            raise self.stream_error
        return self.batch

    async def status(self, run_ref: EngineRunRef) -> EngineStatus:
        return EngineStatus(run_ref, self.status_value, None, False)  # type: ignore[arg-type]

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult:
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.result

    async def cancel(self, run_ref: EngineRunRef) -> str:
        self.cancel_calls += 1
        self.cancel_refs.append(run_ref)
        return "canceled"


class FakeAndroidMemoryAdapter(FakeAdapter):
    descriptor = AdapterDescriptor(
        engine_id="android_memory",
        adapter_version="1.0.0",
        profiles=frozenset({"auto"}),
        required_inputs=frozenset({"memory_capture_manifest"}),
        optional_inputs=frozenset({"memory_evidence"}),
        accepted_contracts=frozenset({"android-memory-ai-context-1.2"}),
        default_timeout_seconds=900,
        resource_profile="isolated_worker",
        stable_error_codes=frozenset(
            {
                "worker_unavailable",
                "engine_timeout",
                "integrity_mismatch",
                "incompatible_contract",
                "privacy_violation",
            }
        ),
    )

    def __init__(self, *, result_state: str = "completed") -> None:
        super().__init__()
        self.status_value = result_state
        self.result = EngineResult(
            "android-memory-ai-context-1.2",
            result_state,  # type: ignore[arg-type]
            {"context_type": "android-memory-ai-context"},
        )
        self.batch = EngineEventBatch(
            run_ref=EngineRunRef(
                "android_memory",
                None,
                f"memory-{EXECUTION_ID.hex}",
                None,
                None,
            ),
            events=(),
        )

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef:
        self.submitted.append(config)
        return EngineRunRef(
            "android_memory",
            None,
            f"memory-{config.execution_id.hex}",
            None,
            None,
        )


class FakeRepository:
    def __init__(self) -> None:
        self.record = _record()
        self.events: list[str] = []
        self.next_attempt: EngineExecutionRecord | None = None
        self.expired = False

    async def allocate_attempt(self, **kwargs: object) -> EngineExecutionRecord:
        self.events.append("allocated")
        return self.record

    async def get(self, **kwargs: object) -> EngineExecutionRecord:
        return self.record

    async def mark_submitted(
        self,
        *,
        run_ref: EngineRunRef,
        **kwargs: object,
    ) -> EngineExecutionRecord:
        self.events.append("submitted")
        self.record = replace(
            self.record,
            external_workspace_id=run_ref.external_workspace_id,
            external_session_id=run_ref.external_session_id,
            external_run_id=run_ref.external_run_id,
            state="running",
            started_at=NOW,
            version=self.record.version + 1,
        )
        return self.record

    async def persist_observation(
        self,
        *,
        run_ref: EngineRunRef,
        target_state: str,
        stable_error_code: str | None,
        **kwargs: object,
    ) -> EngineExecutionRecord:
        self.events.append(f"observed:{target_state}")
        self.record = replace(
            self.record,
            external_run_id=run_ref.external_run_id,
            last_event_cursor=run_ref.cursor,
            state=target_state,
            stable_error_code=stable_error_code,
            version=self.record.version + 1,
        )
        return self.record

    async def claim_finalization(self, **kwargs: object) -> FinalizationClaim:
        self.events.append("claimed")
        artifact_id = result_artifact_id(self.record.id)
        self.record = replace(
            self.record,
            raw_result_artifact_id=artifact_id,
            version=self.record.version + 1,
        )
        return FinalizationClaim(self.record, True)

    async def finalize(
        self,
        *,
        terminal_state: str,
        **kwargs: object,
    ) -> EngineExecutionRecord:
        self.events.append("finalized")
        self.record = replace(
            self.record,
            state=terminal_state,
            completed_at=NOW,
            version=self.record.version + 1,
        )
        return self.record

    async def fail(
        self,
        *,
        stable_error_code: str,
        **kwargs: object,
    ) -> EngineExecutionRecord:
        self.events.append(f"failed:{stable_error_code}")
        self.record = replace(
            self.record,
            state="failed",
            stable_error_code=stable_error_code,
            version=self.record.version + 1,
        )
        return self.record

    async def cancel(self, **kwargs: object) -> EngineExecutionRecord:
        self.events.append("canceled")
        self.record = replace(self.record, state="canceled", version=self.record.version + 1)
        return self.record

    async def record_retryable(
        self,
        *,
        stable_error_code: str,
        **kwargs: object,
    ) -> EngineExecutionRecord:
        self.events.append(f"retryable:{stable_error_code}")
        self.record = replace(self.record, stable_error_code=stable_error_code)
        return self.record

    async def reserve_retry(self, **kwargs: object) -> RetryReservation:
        self.events.append("retry-reserved")
        stable_error_code = kwargs["stable_error_code"]
        assert isinstance(stable_error_code, str)
        self.record = replace(
            self.record,
            state="failed",
            stable_error_code=stable_error_code,
            version=self.record.version + 1,
        )
        self.next_attempt = replace(
            self.record,
            id=UUID("e3000000-0000-4000-8000-000000000002"),
            attempt_number=2,
            state="pending",
            stable_error_code=None,
            external_workspace_id=None,
            external_session_id=None,
            external_run_id=None,
            version=1,
        )
        return RetryReservation(self.record, self.next_attempt)

    async def deadline_expired(self, **kwargs: object) -> bool:
        return self.expired


class FakeSink:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.failure: Exception | None = None

    async def write(self, **kwargs: object) -> UUID:
        self.events.append("sink")
        if self.failure is not None:
            raise self.failure
        assert isinstance(kwargs["result"], EngineResult)
        return kwargs["artifact_id"]  # type: ignore[return-value]


def _lock() -> EngineLock:
    return EngineLock(
        schema_version="1.0",
        smartperfetto=EnginePin(
            source="https://github.com/Gracker/SmartPerfetto.git",
            ref="v1.0.38",
            commit="a" * 40,
            image_digest="sha256:" + "b" * 64,
            contract="workspace-agent-v1",
        ),
        android_memory=EnginePin(
            source="https://github.com/example/memory.git",
            ref=None,
            commit="c" * 40,
            image_digest="sha256:" + "d" * 64,
            contract="android-memory-ai-context-1.2",
        ),
    )


def _service() -> tuple[
    EngineExecutionService,
    FakeRepository,
    FakeWorkspaceService,
    FakeAdapter,
    FakeSink,
]:
    repository = FakeRepository()
    workspaces = FakeWorkspaceService()
    adapter = FakeAdapter()
    sink = FakeSink()
    service = EngineExecutionService(
        repository=repository,  # type: ignore[arg-type]
        workspace_service=workspaces,  # type: ignore[arg-type]
        registry=AdapterRegistry((adapter,)),
        engine_lock=_lock(),
        result_sink=sink,
        now=lambda: NOW,
    )
    return service, repository, workspaces, adapter, sink


def _isolated_service(
    *, result_state: str = "completed"
) -> tuple[
    EngineExecutionService,
    FakeRepository,
    FakeWorkspaceService,
    FakeAndroidMemoryAdapter,
    FakeSink,
]:
    repository = FakeRepository()
    repository.record = _record(engine_id="android_memory")
    workspaces = FakeWorkspaceService()
    adapter = FakeAndroidMemoryAdapter(result_state=result_state)
    sink = FakeSink()
    service = EngineExecutionService(
        repository=repository,  # type: ignore[arg-type]
        workspace_service=workspaces,  # type: ignore[arg-type]
        registry=AdapterRegistry((adapter,)),
        engine_lock=_lock(),
        result_sink=sink,
        now=lambda: NOW,
    )
    return service, repository, workspaces, adapter, sink


@pytest.mark.asyncio
async def test_submit_resolves_workspace_and_persists_only_server_run_reference() -> None:
    service, repository, workspaces, adapter, _sink = _service()
    await service.create_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        engine_id="smartperfetto",
        input_manifest_hash="c" * 64,
        config_hash="d" * 64,
    )

    submitted = await service.submit_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
        inputs=(),
        profile="startup",
        question="private question",
        timeout_seconds=60,
    )

    assert workspaces.calls == [TEAM_ID]
    assert adapter.submitted[0].execution_id == EXECUTION_ID
    assert adapter.submitted[0].external_workspace_id == "workspace-server-owned"
    assert submitted.state == "running"
    assert submitted.external_session_id == "session-1"
    assert repository.events == ["allocated", "submitted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_state", ["completed", "insufficient_data"])
async def test_isolated_engine_skips_workspace_and_sinks_terminal_result(
    result_state: str,
) -> None:
    service, repository, workspaces, adapter, sink = _isolated_service(result_state=result_state)

    submitted = await service.submit_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
        inputs=(),
        profile="auto",
        question=None,
        timeout_seconds=900,
    )
    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=submitted.id,
    )

    assert workspaces.calls == []
    assert adapter.submitted == [SubmitConfig(EXECUTION_ID, ANALYSIS_ID, "auto", None, None, 900)]
    assert repository.record.external_workspace_id is None
    assert repository.record.external_session_id is None
    assert repository.record.external_run_id == f"memory-{EXECUTION_ID.hex}"
    assert sink.events == ["sink"]
    assert outcome.state == result_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stable_code",
    ["capacity_exceeded", "worker_unavailable", "engine_timeout"],
)
async def test_bounded_recoverable_failures_reserve_a_new_attempt(stable_code: str) -> None:
    service, repository, _workspaces, adapter, _sink = _isolated_service()
    repository.record = replace(
        repository.record,
        state="running",
        external_run_id=f"memory-{EXECUTION_ID.hex}",
        started_at=NOW,
        version=2,
    )
    adapter.stream_error = EngineAdapterError(
        stable_code=stable_code,
        retryable=False,
        terminal_state=None,
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.retry is not None and outcome.retry.mode == "new_attempt"
    assert outcome.retry.stable_error_code == stable_code
    assert outcome.retry.attempt_number == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stable_code",
    ["integrity_mismatch", "incompatible_contract", "privacy_violation"],
)
async def test_integrity_schema_and_privacy_failures_are_terminal_even_if_misflagged(
    stable_code: str,
) -> None:
    service, repository, _workspaces, adapter, _sink = _isolated_service()
    repository.record = replace(
        repository.record,
        state="running",
        external_run_id=f"memory-{EXECUTION_ID.hex}",
        started_at=NOW,
        version=2,
    )
    adapter.stream_error = EngineAdapterError(
        stable_code=stable_code,
        retryable=True,
        terminal_state=None,
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == "failed"
    assert outcome.retry is None
    assert repository.record.stable_error_code == stable_code


@pytest.mark.asyncio
async def test_cancel_isolated_run_calls_worker_without_workspace_or_session() -> None:
    service, repository, _workspaces, adapter, _sink = _isolated_service()
    repository.record = replace(
        repository.record,
        state="running",
        external_run_id=f"memory-{EXECUTION_ID.hex}",
        started_at=NOW,
        version=2,
    )

    outcome = await service.cancel_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert adapter.cancel_calls == 1
    assert adapter.cancel_refs == [
        EngineRunRef(
            "android_memory",
            None,
            f"memory-{EXECUTION_ID.hex}",
            None,
            None,
        )
    ]
    assert outcome.state == "canceled"


@pytest.mark.asyncio
async def test_completed_event_saves_cursor_then_sinks_before_terminal_cas() -> None:
    service, repository, _workspaces, adapter, sink = _service()
    repository.record = _record(
        state="running",
        external_workspace_id="workspace-server-owned",
        external_session_id="session-1",
        external_run_id="run-1",
        started_at=NOW,
        version=2,
    )
    adapter.batch = EngineEventBatch(
        run_ref=replace(_run_ref(), cursor="9"),
        events=(EngineEvent("9", "completed", None, "analysis_completed", NOW),),
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == "completed"
    assert repository.record.last_event_cursor == "9"
    assert repository.record.raw_result_artifact_id == result_artifact_id(EXECUTION_ID)
    assert repository.events == ["observed:running", "claimed", "finalized"]
    assert sink.events == ["sink"]


@pytest.mark.asyncio
async def test_sink_failure_leaves_execution_running_for_same_artifact_retry() -> None:
    service, repository, _workspaces, adapter, sink = _service()
    repository.record = _record(
        state="running",
        external_workspace_id="workspace-server-owned",
        external_session_id="session-1",
        external_run_id="run-1",
        started_at=NOW,
        version=2,
    )
    adapter.batch = EngineEventBatch(
        run_ref=replace(_run_ref(), cursor="9"),
        events=(EngineEvent("9", "completed", None, "analysis_completed", NOW),),
    )
    sink.failure = RuntimeError("raw sink secret")

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == "running"
    assert outcome.retry is not None and outcome.retry.mode == "reconnect"
    assert outcome.retry.stable_error_code == "result_persistence_failed"
    assert repository.record.raw_result_artifact_id == result_artifact_id(EXECUTION_ID)
    assert "finalized" not in repository.events


@pytest.mark.asyncio
async def test_report_fetch_disconnect_keeps_its_adapter_error_classification() -> None:
    service, repository, _workspaces, adapter, _sink = _service()
    repository.record = _record(
        state="running",
        external_workspace_id="workspace-server-owned",
        external_session_id="session-1",
        external_run_id="run-1",
        raw_result_artifact_id=result_artifact_id(EXECUTION_ID),
        started_at=NOW,
        version=3,
    )
    adapter.fetch_error = EngineAdapterError(
        stable_code="engine_unavailable",
        retryable=True,
        terminal_state=None,
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == "running"
    assert outcome.retry is not None
    assert outcome.retry.stable_error_code == "engine_unavailable"
    assert "retryable:engine_unavailable" in repository.events


@pytest.mark.asyncio
async def test_result_marker_deadline_fails_as_result_persistence_without_sink_call() -> None:
    service, repository, _workspaces, _adapter, sink = _service()
    repository.expired = True
    repository.record = _record(
        state="running",
        external_workspace_id="workspace-server-owned",
        external_session_id="session-1",
        external_run_id="run-1",
        raw_result_artifact_id=result_artifact_id(EXECUTION_ID),
        started_at=NOW,
        version=3,
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == "failed"
    assert repository.record.stable_error_code == "result_persistence_failed"
    assert sink.events == []


@pytest.mark.asyncio
async def test_awaiting_user_is_observed_then_canceled_and_failed() -> None:
    service, repository, _workspaces, adapter, _sink = _service()
    repository.record = _record(
        state="running",
        external_workspace_id="workspace-server-owned",
        external_session_id="session-1",
        external_run_id="run-1",
        started_at=NOW,
        version=2,
    )
    adapter.batch = EngineEventBatch(
        run_ref=replace(_run_ref(), cursor="4"),
        events=(
            EngineEvent(
                "4",
                "awaiting_user",
                None,
                "engine_interaction_required",
                NOW,
            ),
        ),
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == "failed"
    assert adapter.cancel_calls == 1
    assert repository.events == [
        "observed:awaiting_user",
        "failed:engine_interaction_required",
    ]


@pytest.mark.asyncio
async def test_retryable_disconnect_keeps_same_execution_but_capacity_reserves_next() -> None:
    service, repository, _workspaces, adapter, _sink = _service()
    repository.record = _record(
        state="running",
        external_workspace_id="workspace-server-owned",
        external_session_id="session-1",
        external_run_id="run-1",
        started_at=NOW,
        version=2,
    )
    adapter.stream_error = EngineAdapterError(
        stable_code="engine_unavailable",
        retryable=True,
        terminal_state=None,
    )

    reconnect = await service.step(
        team_id=TEAM_ID, analysis_id=ANALYSIS_ID, execution_id=EXECUTION_ID
    )
    assert reconnect.retry is not None and reconnect.retry.mode == "reconnect"
    assert reconnect.retry.execution_id == EXECUTION_ID

    adapter.stream_error = EngineAdapterError(
        stable_code="capacity_exceeded",
        retryable=True,
        terminal_state=None,
    )
    new_attempt = await service.step(
        team_id=TEAM_ID, analysis_id=ANALYSIS_ID, execution_id=EXECUTION_ID
    )
    assert new_attempt.retry is not None and new_attempt.retry.mode == "new_attempt"
    assert new_attempt.retry.execution_id != EXECUTION_ID
    assert new_attempt.retry.attempt_number == 2


@pytest.mark.asyncio
async def test_internal_composition_requires_sink_and_builds_no_public_route() -> None:
    async def credential(_: SecretStr) -> SecretStr:
        return SecretStr("runtime-secret")

    engine_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        follow_redirects=False,
    )
    artifact_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        follow_redirects=False,
    )
    settings = Settings(
        smartperfetto_enabled=True,
        smartperfetto_base_url="http://127.0.0.1:3001",
        smartperfetto_credential_reference="development-secret-ref",
    )
    try:
        service = build_smartperfetto_execution_service(
            settings=settings,
            control_session_factory=object(),  # type: ignore[arg-type]
            credential_resolver=credential,
            engine_client=engine_client,
            artifact_client=artifact_client,
            engine_lock=_lock(),
            result_sink=FakeSink(),
            now=lambda: NOW,
        )
        assert isinstance(service, EngineExecutionService)
        with pytest.raises(ValueError, match="result sink"):
            build_smartperfetto_execution_service(
                settings=settings,
                control_session_factory=object(),  # type: ignore[arg-type]
                credential_resolver=credential,
                engine_client=engine_client,
                artifact_client=artifact_client,
                engine_lock=_lock(),
                result_sink=None,  # type: ignore[arg-type]
            )
    finally:
        await engine_client.aclose()
        await artifact_client.aclose()


def test_general_composition_registers_only_explicit_optional_adapters() -> None:
    android = FakeAndroidMemoryAdapter()
    service = build_engine_execution_service(
        control_session_factory=object(),  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(),  # type: ignore[arg-type]
        adapters=(None, android),
        engine_lock=_lock(),
        result_sink=FakeSink(),
        now=lambda: NOW,
    )

    assert service._registry.require("android_memory") is android
    with pytest.raises(AdapterRegistryError, match="not registered"):
        service._registry.require("smartperfetto")
