from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from perfpilot_api.engines.contracts import EngineInput, EngineRunRef, SubmitConfig
from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.smartperfetto import SmartPerfettoAdapter
from perfpilot_api.engines.smartperfetto_transport import SmartPerfettoTransport


TEAM_WORKSPACE = "pp-11111111-2222-5333-8444-555555555555"
ANALYSIS_ID = UUID("c1000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("c1000000-0000-4000-8000-000000000002")
ARTIFACT_ID = UUID("c2000000-0000-4000-8000-000000000001")
TRACE_BYTES = b"PERFETTO-SYNTHETIC-TRACE"
SIGNED_URL = "https://objects.example/private-trace?signature=signed-secret-marker"
_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "smartperfetto_workspace_agent_v1"
)


def _json_fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _hash(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


async def _credential(_: SecretStr) -> SecretStr:
    return SecretStr("engine-service-secret")


def _input(
    *,
    kind: str = "trace",
    size_bytes: int | None = None,
    sha256_b64: str | None = None,
    download_url: str = SIGNED_URL,
) -> EngineInput:
    return EngineInput(
        artifact_id=ARTIFACT_ID,
        kind=kind,
        mime="application/octet-stream",
        size_bytes=len(TRACE_BYTES) if size_bytes is None else size_bytes,
        sha256_b64=_hash(TRACE_BYTES) if sha256_b64 is None else sha256_b64,
        download_url=SecretStr(download_url),
    )


def _config(
    *,
    profile: str = "startup",
    question: str | None = None,
    workspace_id: str | None = TEAM_WORKSPACE,
    timeout_seconds: int = 60,
) -> SubmitConfig:
    return SubmitConfig(
        execution_id=EXECUTION_ID,
        analysis_id=ANALYSIS_ID,
        profile=profile,  # type: ignore[arg-type]
        question=question,
        external_workspace_id=workspace_id,
        timeout_seconds=timeout_seconds,
    )


def _adapter(
    *,
    engine_handler: Callable[[httpx.Request], httpx.Response],
    artifact_handler: Callable[[httpx.Request], httpx.Response],
    max_trace_bytes: int = 1024,
    max_timeout_seconds: int = 120,
    spool_factory: Callable[..., Any] = tempfile.SpooledTemporaryFile,
    **adapter_kwargs: object,
) -> tuple[SmartPerfettoAdapter, httpx.AsyncClient, httpx.AsyncClient]:
    engine_client = httpx.AsyncClient(
        transport=httpx.MockTransport(engine_handler),
        follow_redirects=False,
    )
    artifact_client = httpx.AsyncClient(
        transport=httpx.MockTransport(artifact_handler),
        follow_redirects=False,
    )
    transport = SmartPerfettoTransport(
        base_url="https://smartperfetto.example.com",
        credential_reference=SecretStr("vault://smartperfetto/service"),
        credential_resolver=_credential,
        client=engine_client,
        max_json_bytes=64 * 1024,
    )
    return (
        SmartPerfettoAdapter(
            transport=transport,
            artifact_client=artifact_client,
            max_trace_bytes=max_trace_bytes,
            max_timeout_seconds=max_timeout_seconds,
            max_sse_event_bytes=64 * 1024,
            spool_max_memory_bytes=1,
            spool_factory=spool_factory,
            **adapter_kwargs,
        ),
        engine_client,
        artifact_client,
    )


async def _close(*clients: httpx.AsyncClient) -> None:
    for client in clients:
        await client.aclose()


def _successful_artifact(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("Authorization") is None
    return httpx.Response(200, content=TRACE_BYTES)


def _startup_engine(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/traces/upload"):
        return httpx.Response(200, json=_json_fixture("trace-upload-success.json"))
    if request.url.path.endswith("/agent/analyze"):
        return httpx.Response(200, json=_json_fixture("analyze-success.json"))
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def test_descriptor_freezes_the_public_adapter_capabilities() -> None:
    descriptor = SmartPerfettoAdapter.descriptor

    assert descriptor.engine_id == "smartperfetto"
    assert descriptor.adapter_version == "1.0.0"
    assert descriptor.profiles == frozenset({"auto", "startup", "scroll"})
    assert descriptor.required_inputs == frozenset({"trace"})
    assert descriptor.optional_inputs == frozenset()
    assert descriptor.accepted_contracts == frozenset({"workspace-agent-v1"})
    assert descriptor.resource_profile == "network_service"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inputs", "config", "stable_code"),
    [
        ((), _config(), "engine_input_invalid"),
        ((_input(), _input()), _config(), "engine_input_invalid"),
        ((_input(kind="apk"),), _config(), "engine_input_invalid"),
        ((_input(size_bytes=0),), _config(), "engine_input_invalid"),
        ((_input(sha256_b64="not-base64"),), _config(), "engine_input_invalid"),
        ((_input(),), _config(workspace_id=None), "engine_workspace_missing"),
        ((_input(),), _config(timeout_seconds=121), "engine_timeout_invalid"),
        ((_input(),), _config(timeout_seconds=0), "engine_timeout_invalid"),
    ],
)
async def test_submit_rejects_untrusted_or_incomplete_inputs_before_http(
    inputs: tuple[EngineInput, ...],
    config: SubmitConfig,
    stable_code: str,
) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid submission must not perform HTTP")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=forbidden,
        artifact_handler=forbidden,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.submit(inputs, config)
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == stable_code


@pytest.mark.asyncio
async def test_submit_streams_verified_trace_then_uploads_and_analyzes() -> None:
    events: list[str] = []
    engine_requests: list[httpx.Request] = []
    spools: list[Any] = []

    def artifact_handler(request: httpx.Request) -> httpx.Response:
        events.append("artifact-get")
        assert request.headers.get("Authorization") is None
        return httpx.Response(200, content=TRACE_BYTES)

    def engine_handler(request: httpx.Request) -> httpx.Response:
        engine_requests.append(request)
        events.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer engine-service-secret"
        assert request.headers["X-Workspace-Id"] == TEAM_WORKSPACE
        return _startup_engine(request)

    def spool_factory(**kwargs: object) -> Any:
        spool = tempfile.SpooledTemporaryFile(**kwargs)
        spools.append(spool)
        return spool

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=artifact_handler,
        spool_factory=spool_factory,
    )
    try:
        run_ref = await adapter.submit(
            (_input(),),
            _config(question="Focus on the first rendered frame."),
        )
    finally:
        await _close(engine_client, artifact_client)

    assert events == [
        "artifact-get",
        f"/api/workspaces/{TEAM_WORKSPACE}/traces/upload",
        f"/api/workspaces/{TEAM_WORKSPACE}/agent/analyze",
    ]
    upload = engine_requests[0]
    for request in engine_requests:
        rendered = " ".join(
            (str(request.url), repr(request.headers), request.content.decode("latin-1"))
        )
        assert str(EXECUTION_ID) not in rendered
        assert EXECUTION_ID.hex not in rendered
    assert 'name="file"' in upload.content.decode("latin-1")
    assert 'filename="perfpilot-trace-' in upload.content.decode("latin-1")
    assert TRACE_BYTES in upload.content
    assert "upload-url" not in upload.url.path
    assert "signed-secret-marker" not in upload.content.decode("latin-1")

    analyze_body = json.loads(engine_requests[1].content)
    assert analyze_body["traceId"] == "trace-synthetic-001"
    assert analyze_body["options"] == {"analysisMode": "full"}
    assert analyze_body["query"].startswith("Analyze Android application startup")
    assert analyze_body["query"].endswith("Focus on the first rendered frame.")
    assert run_ref.external_session_id == "session-synthetic-001"
    assert run_ref.external_run_id == "run-session-synthetic-001-1"
    assert run_ref.external_workspace_id == TEAM_WORKSPACE
    assert run_ref.cursor is None
    assert "trace-synthetic-001" not in repr(run_ref)
    assert "signed-secret-marker" not in repr(run_ref)
    assert spools and spools[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_bytes", "declared_size", "declared_hash"),
    [
        (TRACE_BYTES + b"overflow", len(TRACE_BYTES), _hash(TRACE_BYTES)),
        (TRACE_BYTES[:-1], len(TRACE_BYTES), _hash(TRACE_BYTES)),
        (TRACE_BYTES, len(TRACE_BYTES), _hash(b"different")),
    ],
)
async def test_materialization_enforces_bound_size_and_exact_hash(
    response_bytes: bytes,
    declared_size: int,
    declared_hash: str,
) -> None:
    def artifact_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=response_bytes)

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unverified bytes must not reach SmartPerfetto")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=forbidden,
        artifact_handler=artifact_handler,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.submit(
                (
                    _input(
                        size_bytes=declared_size,
                        sha256_b64=declared_hash,
                    ),
                ),
                _config(),
            )
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == "trace_integrity_failed"
    assert "signed-secret-marker" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_artifact_redirect_is_refused_without_leaking_location() -> None:
    def artifact_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=_startup_engine,
        artifact_handler=artifact_handler,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.submit((_input(),), _config())
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == "artifact_unavailable"
    assert "evil.example" not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_scroll_uses_fixed_query_and_never_sends_profile_as_analysis_mode() -> None:
    analyze_bodies: list[dict[str, object]] = []

    def engine_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent/analyze"):
            analyze_bodies.append(json.loads(request.content))
        return _startup_engine(request)

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        await adapter.submit((_input(),), _config(profile="scroll"))
    finally:
        await _close(engine_client, artifact_client)

    assert analyze_bodies[0]["options"] == {"analysisMode": "full"}
    assert analyze_bodies[0]["query"].startswith("Analyze Android scrolling")
    assert "scroll" not in analyze_bodies[0]["options"].values()


@pytest.mark.asyncio
async def test_auto_previews_allowlisted_scenes_then_returns_the_deep_dive_run() -> None:
    requests: list[httpx.Request] = []
    analyze_count = 0

    def engine_handler(request: httpx.Request) -> httpx.Response:
        nonlocal analyze_count
        requests.append(request)
        if request.url.path.endswith("/traces/upload"):
            return httpx.Response(200, json=_json_fixture("trace-upload-success.json"))
        if request.url.path.endswith("/agent/analyze"):
            analyze_count += 1
            payload = _json_fixture("analyze-success.json")
            if analyze_count == 2:
                payload["sessionId"] = "session-deep-dive"
                payload["runId"] = "run-deep-dive"
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("/stream"):
            return httpx.Response(
                200,
                content=(_FIXTURE_ROOT / "smart-preview-stream.sse").read_bytes(),
                headers={"Content-Type": "text/event-stream"},
            )
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        run_ref = await adapter.submit((_input(),), _config(profile="auto"))
    finally:
        await _close(engine_client, artifact_client)

    analyze_requests = [
        request for request in requests if request.url.path.endswith("/agent/analyze")
    ]
    assert json.loads(analyze_requests[0].content) == _json_fixture(
        "analyze-smart-preview-request.json"
    )
    assert json.loads(analyze_requests[1].content) == _json_fixture(
        "analyze-smart-deep-dive-request.json"
    )
    assert requests[2].url.path.endswith("/agent/runs/run-session-synthetic-001-1/stream")
    assert run_ref.external_session_id == "session-deep-dive"
    assert run_ref.external_run_id == "run-deep-dive"
    assert run_ref.external_workspace_id == TEAM_WORKSPACE


@pytest.mark.asyncio
async def test_auto_rejects_preview_without_supported_scene_and_skips_deep_dive() -> None:
    analyze_calls = 0
    preview = (
        "id: 1\n"
        "event: analysis_completed\n"
        'data: {"data":{"smartScenePreview":{"reportId":"report-1",'
        '"scenes":[{"id":"scene-1","sceneType":"navigation"}]}}}\n\n'
    ).encode()

    def engine_handler(request: httpx.Request) -> httpx.Response:
        nonlocal analyze_calls
        if request.url.path.endswith("/traces/upload"):
            return httpx.Response(200, json=_json_fixture("trace-upload-success.json"))
        if request.url.path.endswith("/agent/analyze"):
            analyze_calls += 1
            return httpx.Response(200, json=_json_fixture("analyze-success.json"))
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, content=preview)
        raise AssertionError("unsupported preview must not call another endpoint")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.submit((_input(),), _config(profile="auto"))
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == "unsupported_trace_profile"
    assert analyze_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "status", "payload", "stable_code", "retryable"),
    [
        (
            "upload",
            200,
            _json_fixture("trace-upload-success-false.json"),
            "engine_unavailable",
            True,
        ),
        (
            "upload",
            413,
            {"success": False, "code": "TRACE_SIZE_QUOTA_EXCEEDED"},
            "engine_quota_exceeded",
            False,
        ),
        (
            "analyze",
            429,
            _json_fixture("concurrent-quota.json"),
            "capacity_exceeded",
            True,
        ),
        (
            "analyze",
            402,
            _json_fixture("monthly-quota.json"),
            "engine_quota_exceeded",
            False,
        ),
        (
            "analyze",
            423,
            {"success": False, "code": "TENANT_TOMBSTONED"},
            "engine_tenant_unavailable",
            False,
        ),
    ],
)
async def test_upload_and_analyze_failures_use_stable_mapping(
    stage: str,
    status: int,
    payload: dict[str, object],
    stable_code: str,
    retryable: bool,
) -> None:
    def engine_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/traces/upload"):
            if stage == "upload":
                return httpx.Response(status, json=payload)
            return httpx.Response(200, json=_json_fixture("trace-upload-success.json"))
        if request.url.path.endswith("/agent/analyze"):
            return httpx.Response(status, json=payload)
        raise AssertionError("failed submission must not call another endpoint")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.submit((_input(),), _config())
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == stable_code
    assert exc_info.value.retryable is retryable
    assert "Synthetic" not in str(exc_info.value)


class CancelledStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise asyncio.CancelledError
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_preview_cancellation_closes_stream_and_best_effort_cancels_session() -> None:
    cancel_paths: list[str] = []
    stream = CancelledStream()

    def engine_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/traces/upload"):
            return httpx.Response(200, json=_json_fixture("trace-upload-success.json"))
        if request.url.path.endswith("/agent/analyze"):
            return httpx.Response(200, json=_json_fixture("analyze-success.json"))
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, stream=stream)
        if request.url.path.endswith("/cancel"):
            cancel_paths.append(request.url.path)
            return httpx.Response(200, json=_json_fixture("cancel-success.json"))
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await adapter.submit((_input(),), _config(profile="auto"))
    finally:
        await _close(engine_client, artifact_client)

    assert stream.closed
    assert cancel_paths == [f"/api/workspaces/{TEAM_WORKSPACE}/agent/session-synthetic-001/cancel"]


def _run_ref(
    *,
    run_id: str = "run-session-synthetic-001-1",
    cursor: str | None = None,
) -> EngineRunRef:
    return EngineRunRef(
        engine_id="smartperfetto",
        external_session_id="session-synthetic-001",
        external_run_id=run_id,
        cursor=cursor,
        external_workspace_id=TEAM_WORKSPACE,
    )


@pytest.mark.asyncio
async def test_stream_replays_only_strictly_new_events_and_refreshes_cursor() -> None:
    requests: list[httpx.Request] = []

    def engine_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(_FIXTURE_ROOT / "progress-stream.sse").read_bytes(),
            headers={"Content-Type": "text/event-stream"},
        )

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        batch = await adapter.stream(_run_ref(cursor="1"), "1")
    finally:
        await _close(engine_client, artifact_client)

    assert requests[0].url.path.endswith("/agent/runs/run-session-synthetic-001-1/stream")
    assert requests[0].headers["Accept"] == "text/event-stream"
    assert requests[0].headers["Last-Event-ID"] == "1"
    assert [event.event_id for event in batch.events] == ["2", "3"]
    assert [event.progress_percent for event in batch.events] == [80, None]
    assert batch.events[-1].state == "completed"
    assert batch.run_ref.cursor == "3"
    assert "Synthetic conclusion" not in repr(batch)


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_stream_stops_at_batch_limit_and_closes_response() -> None:
    stream = ClosingStream([(_FIXTURE_ROOT / "progress-stream.sse").read_bytes()])

    def engine_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
        max_sse_batch_events=1,
        max_sse_batch_seconds=1.0,
    )
    try:
        batch = await adapter.stream(_run_ref(), None)
    finally:
        await _close(engine_client, artifact_client)

    assert len(batch.events) == 1
    assert batch.events[0].event_id == "1"
    assert stream.closed


@pytest.mark.asyncio
async def test_status_resumes_once_after_404_and_returns_refreshed_run() -> None:
    requests: list[httpx.Request] = []
    status_calls = 0

    def engine_handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        requests.append(request)
        if request.url.path.endswith("/status"):
            status_calls += 1
            if status_calls == 1:
                return httpx.Response(404, json={"success": False, "code": "NOT_FOUND"})
            return httpx.Response(200, json=_json_fixture("status-completed.json"))
        if request.url.path.endswith("/agent/resume"):
            return httpx.Response(200, json=_json_fixture("resume-success.json"))
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        status = await adapter.status(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert json.loads(requests[1].content) == {"sessionId": "session-synthetic-001"}
    assert "traceId" not in json.loads(requests[1].content)
    assert status.state == "completed"
    assert status.run_ref.external_run_id == "run-session-synthetic-001-2-recovered"


@pytest.mark.asyncio
async def test_second_404_after_resume_maps_to_session_lost() -> None:
    resume_calls = 0

    def engine_handler(request: httpx.Request) -> httpx.Response:
        nonlocal resume_calls
        if request.url.path.endswith("/agent/resume"):
            resume_calls += 1
            return httpx.Response(200, json=_json_fixture("resume-success.json"))
        return httpx.Response(404, json={"success": False, "code": "NOT_FOUND"})

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.status(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == "engine_session_lost"
    assert resume_calls == 1


@pytest.mark.asyncio
async def test_stream_uses_recovered_run_id_after_one_resume() -> None:
    requests: list[httpx.Request] = []

    def engine_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/agent/resume"):
            return httpx.Response(200, json=_json_fixture("resume-success.json"))
        if request.url.path.endswith("/run-session-synthetic-001-1/stream"):
            return httpx.Response(404, json={"success": False, "code": "NOT_FOUND"})
        if request.url.path.endswith("/run-session-synthetic-001-2-recovered/stream"):
            return httpx.Response(
                200,
                content=(_FIXTURE_ROOT / "progress-stream.sse").read_bytes(),
            )
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        batch = await adapter.stream(_run_ref(), None)
    finally:
        await _close(engine_client, artifact_client)

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert batch.run_ref.external_run_id == "run-session-synthetic-001-2-recovered"
    assert batch.run_ref.cursor == "3"


@pytest.mark.asyncio
async def test_malformed_or_mismatched_resume_never_changes_route_ownership() -> None:
    def engine_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent/resume"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "sessionId": "another-session",
                    "status": "running",
                    "restored": True,
                    "observability": {"runId": "attacker-run"},
                },
            )
        return httpx.Response(404, json={"success": False, "code": "NOT_FOUND"})

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await adapter.status(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert exc_info.value.stable_code == "engine_session_lost"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream", "state", "stable_code", "retryable"),
    [
        ("pending", "pending", None, False),
        ("running", "running", None, False),
        ("awaiting_user", "awaiting_user", "engine_interaction_required", False),
        ("completed", "completed", None, False),
        ("failed", "failed", "engine_failed", False),
        ("cancelled", "canceled", None, False),
        ("quota_exceeded", "failed", "capacity_exceeded", True),
    ],
)
async def test_status_maps_every_upstream_state(
    upstream: str,
    state: str,
    stable_code: str | None,
    retryable: bool,
) -> None:
    def engine_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "sessionId": "session-synthetic-001",
                "status": upstream,
            },
        )

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        result = await adapter.status(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert result.state == state
    assert result.stable_error_code == stable_code
    assert result.retryable is retryable


@pytest.mark.asyncio
async def test_cancel_sends_empty_body_and_normalizes_cancelled_spelling() -> None:
    requests: list[httpx.Request] = []

    def engine_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_json_fixture("cancel-success.json"))

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        state = await adapter.cancel(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert state == "canceled"
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/session-synthetic-001/cancel")
    assert json.loads(requests[0].content) == {}


@pytest.mark.asyncio
async def test_report_is_recursively_sanitized_and_never_follows_report_url() -> None:
    requests: list[httpx.Request] = []

    def engine_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_json_fixture("report-completed.json"))

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        result = await adapter.fetch_result(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/session-synthetic-001/report")
    assert result.contract == "workspace-agent-v1"
    assert result.state == "completed"
    assert result.payload["reportId"] == "report-synthetic-001"
    rendered = repr(result)
    for unsafe in (
        "Bearer synthetic-secret",
        "synthetic-api-key",
        "synthetic-token",
        "s3://synthetic-bucket",
        "/synthetic/private",
        "C:\\synthetic\\private",
        "X-Amz-Signature=synthetic",
        "synthetic/object/key",
    ):
        assert unsafe not in rendered
    assert "Retain this safe synthetic note" not in rendered


@pytest.mark.asyncio
async def test_report_without_usable_conclusion_or_evidence_is_insufficient() -> None:
    def engine_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "report": {
                    "sessionId": "session-synthetic-001",
                    "reportUrl": "/api/reports/report-empty",
                    "summary": {"partial": True},
                    "findings": [],
                },
            },
        )

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        result = await adapter.fetch_result(_run_ref())
    finally:
        await _close(engine_client, artifact_client)

    assert result.state == "insufficient_data"
    assert result.payload["report"]["summary"]["partial"] is True  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "report"])
async def test_cancel_and_report_each_resume_exactly_once_after_404(
    operation: str,
) -> None:
    target_calls = 0
    resume_calls = 0

    def engine_handler(request: httpx.Request) -> httpx.Response:
        nonlocal target_calls, resume_calls
        if request.url.path.endswith("/agent/resume"):
            resume_calls += 1
            return httpx.Response(200, json=_json_fixture("resume-success.json"))
        target_calls += 1
        if target_calls == 1:
            return httpx.Response(404, json={"success": False, "code": "NOT_FOUND"})
        fixture = "cancel-success.json" if operation == "cancel" else "report-completed.json"
        return httpx.Response(200, json=_json_fixture(fixture))

    adapter, engine_client, artifact_client = _adapter(
        engine_handler=engine_handler,
        artifact_handler=_successful_artifact,
    )
    try:
        if operation == "cancel":
            assert await adapter.cancel(_run_ref()) == "canceled"
        else:
            assert (await adapter.fetch_result(_run_ref())).state == "completed"
    finally:
        await _close(engine_client, artifact_client)

    assert target_calls == 2
    assert resume_calls == 1
