from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

import perfpilot_api.engines.android_memory as adapter_module
from perfpilot_api.engines.android_memory import AndroidMemoryAdapter
from perfpilot_api.engines.android_memory_worker import MemoryWorkerResult
from perfpilot_api.engines.contracts import EngineInput, EngineRunRef, SubmitConfig
from perfpilot_api.engines.errors import EngineAdapterError


ANALYSIS_ID = UUID("a1000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("a2000000-0000-4000-8000-000000000001")
MANIFEST_ID = UUID("a3000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("a4000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
MAX_JSON_NODES = 200_000
MAX_JSON_STRING_CHARS = 16 * 1024 * 1024


def _digest(payload: bytes = b"payload") -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _input(
    *,
    artifact_id: object = MANIFEST_ID,
    kind: object = "memory_capture_manifest",
    mime: object = "application/json",
    size_bytes: object = 100,
    sha256_b64: object | None = None,
    download_url: object = SecretStr("https://claims.example/opaque"),
) -> EngineInput:
    return EngineInput(
        artifact_id=artifact_id,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        mime=mime,  # type: ignore[arg-type]
        size_bytes=size_bytes,  # type: ignore[arg-type]
        sha256_b64=_digest() if sha256_b64 is None else sha256_b64,  # type: ignore[arg-type]
        download_url=download_url,  # type: ignore[arg-type]
    )


def _inputs() -> tuple[EngineInput, ...]:
    return (
        _input(),
        _input(
            artifact_id=EVIDENCE_ID,
            kind="memory_evidence",
            mime="text/plain",
        ),
    )


def _config(**overrides: object) -> SubmitConfig:
    values: dict[str, object] = {
        "execution_id": EXECUTION_ID,
        "analysis_id": ANALYSIS_ID,
        "profile": "auto",
        "question": "Why is memory retained?",
        "external_workspace_id": None,
        "timeout_seconds": 900,
    }
    values.update(overrides)
    return SubmitConfig(**values)  # type: ignore[arg-type]


def _analysis_contract(
    *,
    support_level: object = "limited",
    primary_intent_support_level: object | None = None,
    raw_contents_embedded: object = False,
    local_paths_included: object = False,
) -> dict[str, object]:
    return {
        "support_level": support_level,
        "primary_intent_support_level": (
            support_level if primary_intent_support_level is None else primary_intent_support_level
        ),
        "privacy": {
            "raw_contents_embedded": raw_contents_embedded,
            "local_paths_included": local_paths_included,
        },
    }


def _context(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "context_type": "android-memory-ai-context",
        "schema_version": "1.2",
        "generator": {"name": "android-memory-ai", "version": "1.2.0"},
        "analysis_contract": _analysis_contract(),
    }
    payload.update(overrides)
    return payload


def _context_bytes(**overrides: object) -> bytes:
    return json.dumps(_context(**overrides), separators=(",", ":")).encode("utf-8")


class FakeStaged:
    def __init__(
        self,
        *,
        analysis_id: UUID = ANALYSIS_ID,
        abandon_failure: BaseException | None = None,
        operation_release: asyncio.Event | None = None,
    ) -> None:
        self.manifest = SimpleNamespace(analysis_id=analysis_id)
        self.abandon_failure = abandon_failure
        self.operation_release = operation_release
        self.cleanup_calls = 0
        self.abandon_calls = 0
        self.operation_started = asyncio.Event()
        self.operation_finished = asyncio.Event()

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.operation_started.set()
        try:
            if self.operation_release is not None:
                await self.operation_release.wait()
        finally:
            self.operation_finished.set()

    async def abandon(self) -> None:
        self.abandon_calls += 1
        self.operation_started.set()
        try:
            if self.operation_release is not None:
                await self.operation_release.wait()
            if self.abandon_failure is not None:
                raise self.abandon_failure
        finally:
            self.operation_finished.set()


class FakeStager:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.staged = FakeStaged()
        self.calls: list[tuple[str, tuple[EngineInput, ...]]] = []

    async def stage(self, *, run_id: str, inputs: tuple[EngineInput, ...]) -> FakeStaged:
        self.calls.append((run_id, inputs))
        if self.failure is not None:
            raise self.failure
        return self.staged


class FakeWorker:
    isolation = "oci"

    def __init__(
        self,
        *,
        state: str = "running",
        exit_code: int = 0,
        payload: bytes | None = None,
        start_failure: BaseException | None = None,
        status_failure: BaseException | None = None,
        result_failure: BaseException | None = None,
        cancel_failure: BaseException | None = None,
        cleanup_on_start_failure: bool = False,
        retain_stage_on_start_failure: bool = False,
    ) -> None:
        self.state = state
        self.exit_code = exit_code
        default_support = "insufficient" if exit_code == 2 else "limited"
        self.payload = (
            _context_bytes(analysis_contract=_analysis_contract(support_level=default_support))
            if payload is None
            else payload
        )
        self.start_failure = start_failure
        self.status_failure = status_failure
        self.result_failure = result_failure
        self.cancel_failure = cancel_failure
        self.cleanup_on_start_failure = cleanup_on_start_failure
        self.retain_stage_on_start_failure = retain_stage_on_start_failure
        self.owned_staged: FakeStaged | None = None
        self.start_calls: list[dict[str, object]] = []
        self.status_calls: list[str] = []
        self.result_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def start(self, **kwargs: object) -> None:
        self.start_calls.append(kwargs)
        if self.start_failure is not None:
            staged = kwargs["staged"]
            assert isinstance(staged, FakeStaged)
            if self.cleanup_on_start_failure:
                await staged.cleanup()
            elif self.retain_stage_on_start_failure:
                self.owned_staged = staged
            raise self.start_failure

    async def status(self, run_id: str) -> str:
        self.status_calls.append(run_id)
        if self.status_failure is not None:
            raise self.status_failure
        return self.state

    async def result(self, run_id: str) -> MemoryWorkerResult:
        self.result_calls.append(run_id)
        if self.result_failure is not None:
            raise self.result_failure
        return MemoryWorkerResult(exit_code=self.exit_code, payload=self.payload)

    async def cancel(self, run_id: str) -> None:
        self.cancel_calls.append(run_id)
        if self.cancel_failure is not None:
            raise self.cancel_failure
        if self.state == "running":
            self.state = "canceled"
        await self._cleanup_owned()

    async def shutdown(self) -> None:
        await self._cleanup_owned()

    async def _cleanup_owned(self) -> None:
        if self.owned_staged is None:
            return
        staged = self.owned_staged
        self.owned_staged = None
        await staged.cleanup()


def _json_metrics(root: object) -> tuple[int, int]:
    nodes = 0
    string_chars = 0
    stack = [root]
    while stack:
        value = stack.pop()
        nodes += 1
        if isinstance(value, str):
            string_chars += len(value)
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
    return nodes, string_chars


def _adapter(
    *, stager: FakeStager | None = None, worker: FakeWorker | None = None
) -> AndroidMemoryAdapter:
    return AndroidMemoryAdapter(
        stager=stager or FakeStager(),  # type: ignore[arg-type]
        worker=worker or FakeWorker(),  # type: ignore[arg-type]
        max_timeout_seconds=900,
        now=lambda: NOW,
    )


def _run_ref(
    *,
    engine_id: str = "android_memory",
    run_id: str = f"memory-{EXECUTION_ID.hex}",
    session_id: str | None = None,
    workspace_id: str | None = None,
    cursor: str | None = None,
) -> EngineRunRef:
    return EngineRunRef(
        engine_id=engine_id,
        external_session_id=session_id,
        external_run_id=run_id,
        cursor=cursor,
        external_workspace_id=workspace_id,
    )


def _assert_redacted(error: BaseException, marker: str) -> None:
    assert marker not in str(error)
    assert marker not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_descriptor_is_exact_and_only_declares_stable_codes() -> None:
    descriptor = AndroidMemoryAdapter.descriptor

    assert descriptor.engine_id == "android_memory"
    assert descriptor.adapter_version == "1.0.0"
    assert descriptor.profiles == frozenset({"auto"})
    assert descriptor.required_inputs == frozenset({"memory_capture_manifest"})
    assert descriptor.optional_inputs == frozenset(
        {"memory_evidence", "capture_manifest", "log", "screenshot", "trace"}
    )
    assert descriptor.accepted_contracts == frozenset({"android-memory-ai-context-1.2"})
    assert descriptor.default_timeout_seconds == 900
    assert descriptor.resource_profile == "isolated_worker"
    assert descriptor.stable_error_codes == frozenset(
        {
            "missing_input",
            "manifest_invalid",
            "download_failed",
            "integrity_mismatch",
            "input_limit_exceeded",
            "worker_unavailable",
            "engine_timeout",
            "engine_failed",
            "invalid_output",
            "incompatible_contract",
            "privacy_violation",
        }
    )


@pytest.mark.asyncio
async def test_submit_uses_execution_id_and_transfers_cleanup_ownership() -> None:
    stager = FakeStager()
    worker = FakeWorker()
    adapter = _adapter(stager=stager, worker=worker)

    run_ref = await adapter.submit(_inputs(), _config())

    expected = f"memory-{EXECUTION_ID.hex}"
    assert run_ref == EngineRunRef("android_memory", None, expected, None, None)
    assert stager.calls == [(expected, _inputs())]
    assert worker.start_calls == [
        {
            "run_id": expected,
            "staged": stager.staged,
            "question": "Why is memory retained?",
            "timeout_seconds": 900,
        }
    ]
    assert stager.staged.cleanup_calls == 0


@pytest.mark.asyncio
async def test_retries_for_one_analysis_have_distinct_opaque_run_ids() -> None:
    adapter = _adapter()
    other_execution = UUID("a2000000-0000-4000-8000-000000000002")

    first = await adapter.submit(_inputs(), _config())
    second = await adapter.submit(
        _inputs(), _config(execution_id=other_execution, analysis_id=ANALYSIS_ID)
    )

    assert first.external_run_id == f"memory-{EXECUTION_ID.hex}"
    assert second.external_run_id == f"memory-{other_execution.hex}"
    assert ANALYSIS_ID.hex not in first.external_run_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "stable_code"),
    [
        ({"execution_id": "not-a-uuid"}, "manifest_invalid"),
        ({"analysis_id": "not-a-uuid"}, "manifest_invalid"),
        ({"profile": "startup"}, "manifest_invalid"),
        ({"question": 7}, "manifest_invalid"),
        ({"question": "x\x00y"}, "manifest_invalid"),
        ({"question": "x" * 16_385}, "manifest_invalid"),
        ({"external_workspace_id": "workspace"}, "manifest_invalid"),
        ({"timeout_seconds": True}, "engine_timeout"),
        ({"timeout_seconds": 0}, "engine_timeout"),
        ({"timeout_seconds": 901}, "engine_timeout"),
    ],
)
async def test_submit_rejects_invalid_config_before_staging(
    overrides: dict[str, object], stable_code: str
) -> None:
    stager = FakeStager()

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager).submit(_inputs(), _config(**overrides))

    assert caught.value.stable_code == stable_code
    assert stager.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inputs", "stable_code"),
    [
        ((), "missing_input"),
        ((_input(kind="memory_evidence"),), "missing_input"),
        ((_input(), _input(artifact_id=EVIDENCE_ID)), "manifest_invalid"),
        ((_input(), _input(artifact_id=MANIFEST_ID, kind="trace")), "manifest_invalid"),
        ((_input(), _input(artifact_id=EVIDENCE_ID, kind="apk")), "manifest_invalid"),
        ((_input(artifact_id="bad"),), "manifest_invalid"),
        ((_input(mime=""),), "manifest_invalid"),
        ((_input(size_bytes=True),), "manifest_invalid"),
        ((_input(size_bytes=-1),), "manifest_invalid"),
        ((_input(sha256_b64="not-a-digest"),), "manifest_invalid"),
        ((_input(download_url="not-a-secret"),), "manifest_invalid"),
        ((_input(download_url=SecretStr("")),), "manifest_invalid"),
    ],
)
async def test_submit_prevalidates_authoritative_inputs(
    inputs: tuple[EngineInput, ...], stable_code: str
) -> None:
    stager = FakeStager()

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager).submit(inputs, _config())

    assert caught.value.stable_code == stable_code
    assert stager.calls == []


@pytest.mark.asyncio
async def test_staging_failure_preserves_stable_mapping_without_exception_chain() -> None:
    marker = "https://secret.example/object?X-Amz-Signature=marker"
    failure = EngineAdapterError(stable_code="integrity_mismatch", retryable=False)
    failure.add_note(marker)
    stager = FakeStager(failure=failure)

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager).submit(_inputs(), _config())

    assert caught.value.stable_code == "integrity_mismatch"
    assert caught.value.retryable is False
    assert stager.staged.cleanup_calls == 0
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
async def test_worker_pre_spawn_failure_is_not_double_cleaned_by_adapter() -> None:
    marker = "/work/input/private/source"
    stager = FakeStager()
    worker = FakeWorker(
        start_failure=RuntimeError(marker),
        cleanup_on_start_failure=True,
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager, worker=worker).submit(_inputs(), _config())

    assert caught.value.stable_code == "worker_unavailable"
    assert caught.value.retryable is True
    assert stager.staged.cleanup_calls == 1
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
async def test_worker_unconfirmed_start_failure_retains_stage_until_worker_shutdown() -> None:
    marker = "container-termination-unconfirmed"
    stager = FakeStager()
    worker = FakeWorker(
        start_failure=RuntimeError(marker),
        retain_stage_on_start_failure=True,
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager, worker=worker).submit(_inputs(), _config())

    assert caught.value.stable_code == "worker_unavailable"
    assert caught.value.retryable is True
    assert stager.staged.cleanup_calls == 0
    _assert_redacted(caught.value, marker)

    await worker.shutdown()
    await worker.shutdown()
    assert stager.staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_worker_start_cancellation_does_not_return_cleanup_ownership_to_adapter() -> None:
    stager = FakeStager()
    worker = FakeWorker(
        start_failure=asyncio.CancelledError(),
        retain_stage_on_start_failure=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await _adapter(stager=stager, worker=worker).submit(_inputs(), _config())

    assert stager.staged.cleanup_calls == 0
    await worker.shutdown()
    assert stager.staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_submit_rejects_a_staged_manifest_bound_to_another_analysis() -> None:
    stager = FakeStager()
    stager.staged = FakeStaged(analysis_id=UUID("a1000000-0000-4000-8000-000000000099"))
    worker = FakeWorker()

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager, worker=worker).submit(_inputs(), _config())

    assert caught.value.stable_code == "manifest_invalid"
    assert stager.staged.cleanup_calls == 0
    assert stager.staged.abandon_calls == 1
    assert worker.start_calls == []


@pytest.mark.asyncio
async def test_submit_cancellation_waits_for_adapter_owned_abandon() -> None:
    release = asyncio.Event()
    stager = FakeStager()
    stager.staged = FakeStaged(
        analysis_id=UUID("a1000000-0000-4000-8000-000000000099"),
        operation_release=release,
    )
    worker = FakeWorker()
    submit_task = asyncio.create_task(
        _adapter(stager=stager, worker=worker).submit(_inputs(), _config())
    )
    await stager.staged.operation_started.wait()

    submit_task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await submit_task
    assert stager.staged.operation_finished.is_set()
    assert stager.staged.cleanup_calls == 0
    assert stager.staged.abandon_calls == 1
    assert worker.start_calls == []


@pytest.mark.asyncio
async def test_adapter_owned_abandon_failure_preserves_stable_redacted_error() -> None:
    marker = "abandon-private-failure-marker"
    stager = FakeStager()
    stager.staged = FakeStaged(
        analysis_id=UUID("a1000000-0000-4000-8000-000000000099"),
        abandon_failure=RuntimeError(marker),
    )
    worker = FakeWorker()

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager, worker=worker).submit(_inputs(), _config())

    assert caught.value.stable_code == "manifest_invalid"
    assert stager.staged.cleanup_calls == 0
    assert stager.staged.abandon_calls == 1
    assert stager.staged.operation_finished.is_set()
    assert worker.start_calls == []
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
async def test_worker_start_timeout_preserves_the_stable_timeout_mapping() -> None:
    stager = FakeStager()
    worker = FakeWorker(
        start_failure=EngineAdapterError(
            stable_code="engine_timeout", retryable=True, terminal_state=None
        ),
        cleanup_on_start_failure=True,
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(stager=stager, worker=worker).submit(_inputs(), _config())

    assert caught.value.stable_code == "engine_timeout"
    assert caught.value.retryable is True
    assert stager.staged.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_state", "exit_code", "state", "code", "retryable"),
    [
        ("running", 0, "running", None, False),
        ("completed", 0, "completed", None, False),
        ("completed", 2, "insufficient_data", None, False),
        ("completed", 17, "failed", "engine_failed", False),
        ("failed", 1, "failed", "engine_failed", False),
        ("canceled", -1, "canceled", None, False),
        ("lost", -1, "failed", "worker_unavailable", True),
    ],
)
async def test_status_strictly_maps_worker_states(
    worker_state: str,
    exit_code: int,
    state: str,
    code: str | None,
    retryable: bool,
) -> None:
    worker = FakeWorker(state=worker_state, exit_code=exit_code)

    status = await _adapter(worker=worker).status(_run_ref())

    assert (status.state, status.stable_error_code, status.retryable) == (
        state,
        code,
        retryable,
    )
    assert worker.result_calls == (
        [f"memory-{EXECUTION_ID.hex}"] if worker_state == "completed" else []
    )


@pytest.mark.asyncio
async def test_unexpected_worker_state_fails_closed() -> None:
    worker = FakeWorker(state="mystery")

    status = await _adapter(worker=worker).status(_run_ref())

    assert status.state == "failed"
    assert status.stable_error_code == "worker_unavailable"
    assert status.retryable is True


@pytest.mark.asyncio
async def test_status_worker_exception_is_stable_and_redacted() -> None:
    marker = "postgresql://user:password@database.internal/app"
    worker = FakeWorker(status_failure=RuntimeError(marker))

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).status(_run_ref())

    assert caught.value.stable_code == "worker_unavailable"
    assert caught.value.retryable is True
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
async def test_stream_emits_each_synthetic_progress_event_at_most_once() -> None:
    marker = "private-question-marker"
    worker = FakeWorker(state="running")
    adapter = _adapter(worker=worker)

    first = await adapter.stream(_run_ref(), None)
    replay = await adapter.stream(first.run_ref, first.run_ref.cursor)

    assert [
        (event.event_id, event.message_code, event.progress_percent) for event in first.events
    ] == [
        ("1", "downloading", 10),
        ("2", "verifying", 35),
        ("3", "analyzing", 65),
    ]
    assert all(event.state == "running" and event.occurred_at == NOW for event in first.events)
    assert first.run_ref.cursor == "3"
    assert replay.events == ()
    assert replay.run_ref.cursor == "3"
    assert marker not in repr(first)
    assert len(worker.status_calls) == 2


@pytest.mark.asyncio
async def test_stream_resumes_from_a_valid_cursor_and_terminal_status_is_finite() -> None:
    worker = FakeWorker(state="running")
    adapter = _adapter(worker=worker)

    resumed = await adapter.stream(_run_ref(cursor="1"), "1")
    worker.state = "completed"
    terminal = await adapter.stream(resumed.run_ref, resumed.run_ref.cursor)

    assert [event.event_id for event in resumed.events] == ["2", "3"]
    assert terminal.events == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_ref",
    [
        _run_ref(engine_id="smartperfetto"),
        _run_ref(run_id="memory-invalid"),
        _run_ref(session_id="session"),
        _run_ref(workspace_id="workspace"),
    ],
)
async def test_operations_reject_unbound_run_references(run_ref: EngineRunRef) -> None:
    with pytest.raises(EngineAdapterError) as caught:
        await _adapter().status(run_ref)

    assert caught.value.stable_code == "incompatible_contract"


@pytest.mark.asyncio
async def test_stream_rejects_invalid_or_conflicting_cursors() -> None:
    adapter = _adapter()

    for run_ref, cursor in ((_run_ref(), "4"), (_run_ref(cursor="2"), "1")):
        with pytest.raises(EngineAdapterError) as caught:
            await adapter.stream(run_ref, cursor)
        assert caught.value.stable_code == "incompatible_contract"


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_does_not_delete_terminal_result() -> None:
    worker = FakeWorker(state="running")
    adapter = _adapter(worker=worker)

    assert await adapter.cancel(_run_ref()) == "canceled"
    assert await adapter.cancel(_run_ref()) == "canceled"
    assert worker.cancel_calls == [f"memory-{EXECUTION_ID.hex}"]

    completed = FakeWorker(state="completed", exit_code=0)
    completed_adapter = _adapter(worker=completed)
    assert await completed_adapter.cancel(_run_ref()) == "completed"
    assert completed.cancel_calls == []
    assert (await completed_adapter.fetch_result(_run_ref())).state == "completed"


@pytest.mark.asyncio
async def test_cancel_lost_or_unavailable_maps_to_retryable_worker_unavailable() -> None:
    marker = "file:///private/worker.sock"
    workers = (
        FakeWorker(state="lost"),
        FakeWorker(state="running", cancel_failure=RuntimeError(marker)),
    )
    for worker in workers:
        with pytest.raises(EngineAdapterError) as caught:
            await _adapter(worker=worker).cancel(_run_ref())
        assert caught.value.stable_code == "worker_unavailable"
        assert caught.value.retryable is True
        _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "state", "support_level"),
    [
        (0, "completed", "limited"),
        (0, "completed", "supported"),
        (0, "completed", "strong"),
        (2, "insufficient_data", "insufficient"),
    ],
)
async def test_fetch_result_accepts_exact_success_exit_semantics(
    exit_code: int, state: str, support_level: str
) -> None:
    payload = _context(
        analysis_contract=_analysis_contract(support_level=support_level),
        upstream_extension={"retained": True},
    )
    worker = FakeWorker(
        state="completed",
        exit_code=exit_code,
        payload=json.dumps(payload).encode(),
    )

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.contract == "android-memory-ai-context-1.2"
    assert result.state == state
    assert result.payload == payload
    assert isinstance(result.payload, dict)


@pytest.mark.asyncio
async def test_fetch_result_does_not_reserialize_validated_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _context(upstream_extension={"retained": True, "labels": ["safe"]})
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())
    real_dumps = json.dumps

    def forbidden_dumps(*args: object, **kwargs: object) -> str:
        raise AssertionError("fetch_result must not serialize a second payload copy")

    monkeypatch.setattr(adapter_module.json, "dumps", forbidden_dumps)

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.payload == payload
    assert json.loads(real_dumps(result.payload)) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [1, -1, 3, 255])
async def test_fetch_result_maps_failed_or_unknown_exit_to_engine_failed(
    exit_code: int,
) -> None:
    worker = FakeWorker(state="failed", exit_code=exit_code, payload=b"")

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "engine_failed"


@pytest.mark.asyncio
async def test_fetch_result_maps_a_lost_run_to_retryable_worker_unavailable() -> None:
    worker = FakeWorker(state="lost", exit_code=-1, payload=b"")

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "worker_unavailable"
    assert caught.value.retryable is True
    assert worker.result_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        b"",
        b"\xff",
        b"{} trailing",
        b"[]",
        b'{"context_type":"android-memory-ai-context","context_type":"duplicate"}',
        b'{"context_type":"android-memory-ai-context","value":NaN}',
    ],
)
async def test_fetch_result_rejects_missing_or_invalid_json(payload: bytes | None) -> None:
    worker = FakeWorker(state="completed", exit_code=0, payload=b"ignored")
    worker.payload = payload

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "invalid_output"


@pytest.mark.asyncio
async def test_json_preflight_rejects_duplicate_keys_before_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b'{"context_type":"android-memory-ai-context","context_type":"duplicate"}'
    loads_called = False

    def forbidden_loads(*args: object, **kwargs: object) -> object:
        nonlocal loads_called
        loads_called = True
        raise AssertionError("json.loads must not run after a preflight rejection")

    monkeypatch.setattr(adapter_module.json, "loads", forbidden_loads)
    worker = FakeWorker(state="completed", payload=raw)

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "invalid_output"
    assert loads_called is False
    _assert_redacted(caught.value, "duplicate")


@pytest.mark.asyncio
async def test_json_preflight_rejects_node_overflow_before_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _context(extension=[])
    nodes, _ = _json_metrics(payload)
    extension = payload["extension"]
    assert isinstance(extension, list)
    extension.extend([None] * (MAX_JSON_NODES - nodes + 1))
    raw = json.dumps(payload).encode()
    loads_called = False

    def forbidden_loads(*args: object, **kwargs: object) -> object:
        nonlocal loads_called
        loads_called = True
        raise AssertionError("json.loads must not run after a preflight rejection")

    monkeypatch.setattr(adapter_module.json, "loads", forbidden_loads)
    worker = FakeWorker(state="completed", payload=raw)

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "invalid_output"
    assert loads_called is False
    _assert_redacted(caught.value, "extension")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _context(context_type="other"),
        _context(schema_version="1.3"),
        _context(generator={"name": "other", "version": "1.2.0"}),
        _context(generator={"name": "android-memory-ai", "version": "1.2.1"}),
        _context(analysis_contract=_analysis_contract(local_paths_included=0)),
        _context(analysis_contract=_analysis_contract(raw_contents_embedded=0)),
        _context(analysis_contract=_analysis_contract(support_level="Insufficient")),
        _context(analysis_contract=_analysis_contract(support_level="insufficient_data")),
        _context(analysis_contract=_analysis_contract(support_level=1)),
        _context(analysis_contract=_analysis_contract(primary_intent_support_level="unknown")),
        _context(
            analysis_contract={
                "primary_intent_support_level": "limited",
                "privacy": {
                    "raw_contents_embedded": False,
                    "local_paths_included": False,
                },
            }
        ),
        _context(
            analysis_contract={
                "support_level": "limited",
                "privacy": {
                    "raw_contents_embedded": False,
                    "local_paths_included": False,
                },
            }
        ),
        _context(
            analysis_contract={
                "support_level": "limited",
                "primary_intent_support_level": "limited",
                "privacy": {"local_paths_included": False},
            }
        ),
        _context(
            analysis_contract={
                "support_level": "limited",
                "primary_intent_support_level": "limited",
                "privacy": {"raw_contents_embedded": False},
            }
        ),
    ],
)
async def test_fetch_result_rejects_incompatible_contract_without_coercion(
    payload: dict[str, object],
) -> None:
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "incompatible_contract"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _context(analysis_contract=_analysis_contract(local_paths_included=True)),
        _context(analysis_contract=_analysis_contract(raw_contents_embedded=True)),
        _context(path="/work/input/meminfo.txt"),
        _context(uri="FiLe:///private/evidence"),
        _context(url="https://objects/?X-Amz-Signature=secret"),
        _context(nested={"OBJECT_KEY": "tenant/private"}),
        _context(database="postgresql://user:password@db/app"),
        _context(database="postgresql+psycopg://user:password@db/app"),
        _context(database="MySQL://user:password@db/app"),
    ],
)
async def test_fetch_result_rejects_privacy_markers_everywhere(
    payload: dict[str, object],
) -> None:
    marker = json.dumps(payload)
    worker = FakeWorker(state="completed", payload=marker.encode())

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"
    _assert_redacted(caught.value, marker)


@pytest.mark.parametrize(
    "key",
    ["localPath", "LocalPath", "local-path", "local path", "artifactPaths"],
)
def test_path_key_detection_normalizes_common_field_styles(key: str) -> None:
    assert adapter_module._is_path_key(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    [
        {"nested": {"path": "/private/evidence.txt"}},
        {"nested": {"artifact_path": "C:\\private\\evidence.txt"}},
        {"nested": {"paths": ["safe/name", "C:/private/evidence.txt"]}},
        {"nested": {"DIRECTORY": "\\\\server\\share\\evidence"}},
        {"nested": {"cache_dir": "/var/private/evidence"}},
        {"nested": {"capture_root": "/private/root"}},
        {"nested": {"location": ["relative", "/private/location"]}},
    ],
)
async def test_privacy_scan_rejects_absolute_paths_under_path_semantic_keys(
    extension: dict[str, object],
) -> None:
    marker = json.dumps(extension)
    worker = FakeWorker(
        state="completed",
        payload=json.dumps(_context(**extension)).encode(),
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    [
        {"nested": {"paths": {"/private/evidence": {}}}},
        {"nested": {"artifact_paths": {r"C:\private\evidence": []}}},
        {"nested": {"paths": [{"C:/private/evidence": None}]}},
        {"nested": {"paths": {"relative": {r"\\server\share\evidence": {}}}}},
    ],
)
async def test_privacy_scan_rejects_absolute_mapping_keys_inherited_from_paths(
    extension: dict[str, object],
) -> None:
    marker = json.dumps(extension)
    worker = FakeWorker(
        state="completed",
        payload=json.dumps(_context(**extension)).encode(),
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
async def test_privacy_scan_allows_relative_values_under_path_semantic_keys() -> None:
    payload = _context(
        nested={
            "path": "meminfo/evidence.txt",
            "artifact_paths": ["logs/a.txt", "screenshots/b.png"],
            "directory": ".",
            "cache_dir": "cache",
            "root": "input",
            "location": "relative/location",
            "windows_drive_relative": "C:evidence.txt",
        }
    )
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.payload == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nested",
    [
        {"paths": {"evidence/meminfo.txt": {}}},
        {"artifact_paths": [{"meminfo.txt": {"relative/path": []}}]},
    ],
)
async def test_privacy_scan_allows_relative_mapping_keys_inherited_from_paths(
    nested: dict[str, object],
) -> None:
    payload = _context(nested=nested)
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.payload == payload


@pytest.mark.asyncio
async def test_privacy_scan_allows_slash_like_keys_in_non_path_mappings() -> None:
    payload = _context(nested={"identifiers": {"ordinary/slash-like-id": {}, "C:label": []}})
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.payload == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "  /Users/private/evidence.txt\t",
        r" C:\private\evidence.txt ",
        "\nC:/private/evidence.txt",
        r"  \\server\share\evidence.txt  ",
    ],
)
async def test_privacy_scan_rejects_trimmed_absolute_path_strings_under_any_key(
    value: str,
) -> None:
    payload = _context(identifier=value)
    marker = json.dumps(payload)
    worker = FakeWorker(state="completed", payload=marker.encode())

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoded", "decoded_marker"),
    [
        ("%2FUsers%2Fprivate%2Fevidence.txt", "/Users/private/evidence.txt"),
        ("%252FUsers%252Fprivate%252Fevidence.txt", "/Users/private/evidence.txt"),
        (
            "postgresql%3A%2F%2Fuser%3Apassword%40db%2Fapp",
            "postgresql://user:password@db/app",
        ),
        ("https%3A%2F%2Fobjects%2F%3FX-Amz-Signature%3Dsecret", "X-Amz-Signature"),
        (
            "%2FUsers%2Falice%2Fprivate.txt%FF",
            "/Users/alice/private.txt",
        ),
        (
            "C%3A%2Fprivate%2Fevidence.txt%FF",
            "C:/private/evidence.txt",
        ),
        (
            "https%3A%2F%2Fuser%3Apassword%40example.test%2Fprivate%FF",
            "https://user:password@example.test/private",
        ),
    ],
)
async def test_privacy_scan_rejects_bounded_percent_decoded_variants(
    encoded: str,
    decoded_marker: str,
) -> None:
    payload = _context(identifier=encoded)
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"
    _assert_redacted(caught.value, encoded)
    _assert_redacted(caught.value, decoded_marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    [
        {"objectKey": "tenant/private"},
        {"apiKey": "secret-value"},
        {"client-secret": "secret-value"},
        {"description": "object_key"},
        {"description": "object_key=tenant/private"},
        {"database": "redis://cache.internal/0"},
        {"endpoint": "https://user:password@example.test/private"},
    ],
)
async def test_privacy_scan_rejects_exact_sensitive_fields_and_structured_values(
    extension: dict[str, object],
) -> None:
    marker = json.dumps(extension)
    worker = FakeWorker(
        state="completed",
        payload=json.dumps(_context(**extension)).encode(),
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"
    _assert_redacted(caught.value, marker)


@pytest.mark.asyncio
async def test_privacy_scan_allows_benign_marker_words_and_descriptive_urls() -> None:
    payload = _context(
        object_key_count=3,
        note="object_key was omitted",
        description="The cache documentation mentions redis:// without embedding a connection URL.",
        evidence_summary="The report references /proc/meminfo without including a local path value.",
        localized_note="内存分析完成",
        progress="100% complete",
    )
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.payload == payload


@pytest.mark.asyncio
async def test_privacy_scan_sees_json_escapes_and_nested_keys() -> None:
    raw = _context_bytes(nested={"ob\u006aect_key": "safe"}).replace(
        b"object_key", b"ob\\u006aect_key"
    )
    worker = FakeWorker(state="completed", payload=raw)

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "privacy_violation"


@pytest.mark.asyncio
async def test_fetch_result_rejects_excessive_json_depth() -> None:
    nested: dict[str, Any] = {"leaf": "safe"}
    for _ in range(70):
        nested = {"nested": nested}
    worker = FakeWorker(state="completed", payload=json.dumps(_context(extension=nested)).encode())

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("over_limit", [False, True])
async def test_json_node_bound_counts_mapping_keys_and_values(over_limit: bool) -> None:
    payload = _context(extension=[])
    nodes, _ = _json_metrics(payload)
    extension = payload["extension"]
    assert isinstance(extension, list)
    extension.extend([None] * (MAX_JSON_NODES - nodes + int(over_limit)))
    assert _json_metrics(payload)[0] == MAX_JSON_NODES + int(over_limit)
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    if not over_limit:
        result = await _adapter(worker=worker).fetch_result(_run_ref())
        assert result.state == "completed"
        return
    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())
    assert caught.value.stable_code == "invalid_output"
    _assert_redacted(caught.value, "extension")


@pytest.mark.asyncio
@pytest.mark.parametrize("over_limit", [False, True])
async def test_json_aggregate_string_bound_counts_keys_and_values(over_limit: bool) -> None:
    payload = _context(blob="")
    _, string_chars = _json_metrics(payload)
    payload["blob"] = "x" * (MAX_JSON_STRING_CHARS - string_chars + int(over_limit))
    assert _json_metrics(payload)[1] == MAX_JSON_STRING_CHARS + int(over_limit)
    worker = FakeWorker(state="completed", payload=json.dumps(payload).encode())

    if not over_limit:
        result = await _adapter(worker=worker).fetch_result(_run_ref())
        assert result.state == "completed"
        return
    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())
    assert caught.value.stable_code == "invalid_output"
    _assert_redacted(caught.value, "x" * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    [
        {"nested": {"key": "\ud800"}},
        {"nested": {"\ud800": "value"}},
    ],
)
async def test_json_rejects_surrogates_in_nested_keys_or_values(
    extension: dict[str, object],
) -> None:
    worker = FakeWorker(
        state="completed",
        payload=json.dumps(_context(**extension)).encode("ascii"),
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "invalid_output"
    _assert_redacted(caught.value, "\\ud800")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "support_level"),
    [
        (0, "insufficient"),
        (2, "limited"),
        (2, "supported"),
        (2, "strong"),
    ],
)
async def test_fetch_result_rejects_exit_and_exact_support_level_mismatch(
    exit_code: int, support_level: str
) -> None:
    worker = FakeWorker(
        state="completed",
        exit_code=exit_code,
        payload=_context_bytes(analysis_contract=_analysis_contract(support_level=support_level)),
    )

    with pytest.raises(EngineAdapterError) as caught:
        await _adapter(worker=worker).fetch_result(_run_ref())

    assert caught.value.stable_code == "invalid_output"


@pytest.mark.asyncio
async def test_root_coverage_status_does_not_override_exact_support_contract() -> None:
    payload = _context(
        analysis_contract=_analysis_contract(support_level="limited"),
        coverage={"status": "insufficient"},
    )
    worker = FakeWorker(state="completed", exit_code=0, payload=json.dumps(payload).encode())

    result = await _adapter(worker=worker).fetch_result(_run_ref())

    assert result.state == "completed"
    assert result.payload == payload


@pytest.mark.asyncio
async def test_terminal_result_replays_through_a_fresh_adapter() -> None:
    worker = FakeWorker(state="completed", exit_code=2)

    first = await _adapter(worker=worker).fetch_result(_run_ref())
    replay = await _adapter(worker=worker).fetch_result(_run_ref())

    assert first == replay
    assert len(worker.result_calls) == 2
