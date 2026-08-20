from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient as _RawTestClient
from starlette.requests import Request
from starlette.requests import ClientDisconnect
import httpx

from perfpilot_api.ai.local_report import LocalReportSynthesizer
from perfpilot_api.ai.openai_compatible import SynthesisCandidate
from perfpilot_api.engines.canonical_results import EngineResultValidationError
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.local_app import (
    LocalEngineRun,
    _LOCAL_EVIDENCE_FORMAT_VERSION,
    _InputDescriptor,
    _FinalizeUploadRequest,
    _LocalAnalysis,
    _LocalInput,
    _LocalUpload,
    _PersistedLocalEvidenceError,
    _compose_local_report,
    _evidence_manifest,
    _prepare_local_report,
    _prepared_from_persisted_documents,
    _public_origin,
    _remote_capture_question,
    _restore_ai_rounds,
    _source_code_analysis_unavailable_document,
    _source_code_analysis_document,
    _local_source_code_document,
    _normalize_local_smartperfetto_result,
    create_local_app,
)
from perfpilot_api.local_analysis_store import (
    LocalAnalysisStore,
    LocalAnalysisStoreDurabilityError,
    LocalAnalysisStoreError,
    validate_analysis_runtime_status,
)
from perfpilot_api.local_agent_artifacts import LocalAgentArtifactService
from perfpilot_api.local_control_store import LocalControlStore
from perfpilot_api.local_device_capture import (
    LocalApkMetadata,
    LocalDeviceCapture,
    LocalDeviceCaptureError,
)
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import build_ai_projection
from perfpilot_api.reports.smartperfetto_original import (
    persist_smartperfetto_original,
)
from perfpilot_api.reports.source_context import validate_source_context
from perfpilot_api.security.task_snapshots import (
    validate_source_task_snapshot,
    verify_task_jws,
)
from perfpilot_agent.capture import CaptureError, CaptureTaskRunner, ThermalReading
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import (
    ControlClient,
    SourceTaskExecuteResponse,
    TaskExecuteResponse,
)
from perfpilot_agent.credentials import AgentCredentials, TaskSigningKey
from perfpilot_agent.executor import TaskExecutor
from perfpilot_agent.service import TaskLoop
from perfpilot_agent.security import TaskVerifier
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding
from perfpilot_agent.uploads import InputDownloader, MultipartUploader
from perfpilot_api.services.agent_tasks import (
    AgentExecutionAccess,
    AgentExecutionScenario,
    AgentTaskCancellation,
    ValidatedAgentExecutionManifest,
)
from perfpilot_api.services.source_workspaces import SourceBinding
from perfpilot_api.workers.source_orchestrator import derive_source_authority
from perfpilot_api.security.agent_signatures import (
    encode_ed25519_public_key,
    encode_signature,
    refresh_proof_message,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


_create_local_app = create_local_app


def create_local_app(**kwargs):
    if kwargs.get("control_store") is None:
        data_root = kwargs.get("data_root")
        assert isinstance(data_root, Path)
        control = LocalControlStore(data_root / "control")
        seeded = control.ensure_user("ray_wu", "initial local password", True)
        if seeded.created:
            control.change_password(
                seeded.principal.user_id,
                "initial local password",
                "established local password",
            )
        kwargs["control_store"] = control
    return _create_local_app(**kwargs)


class TestClient(_RawTestClient):
    """Legacy local-runtime tests exercise authenticated browser requests."""

    def __enter__(self) -> "TestClient":
        super().__enter__()
        csrf = self.get("/v1/auth/csrf")
        logged_in = self.post(
            "/v1/auth/login",
            headers={
                "Origin": "http://localhost:3000",
                "x-csrf-token": csrf.json()["csrf_token"],
            },
            json={"username": "ray_wu", "password": "established local password"},
        )
        assert logged_in.status_code == 200, logged_in.text
        return self

    def request(self, method, url, *, headers=None, **kwargs):
        if str(method).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(headers or {})
            headers.setdefault("Origin", "http://localhost:3000")
        return super().request(method, url, headers=headers, **kwargs)


class _FakeSmartPerfettoGateway:
    def __init__(
        self,
        result: EngineResult,
        *,
        original_report_html_bytes: bytes = (
            b"<!DOCTYPE html><html><body>SmartPerfetto</body></html>"
        ),
    ) -> None:
        self.original_report_html_bytes = original_report_html_bytes
        self.result = EngineResult(
            contract=result.contract,
            state=result.state,
            payload=result.payload,
            original_report_html_bytes=original_report_html_bytes,
        )
        self.submissions: list[tuple[bytes, str, str | None]] = []
        self.cancel_calls: list[LocalEngineRun] = []

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        self.submissions.append((trace_path.read_bytes(), profile, question))
        return LocalEngineRun(session_id="session-local-1", run_id="run-local-1")

    async def status(self, run: LocalEngineRun) -> str:
        assert run.session_id == "session-local-1"
        return "completed"

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        assert run.run_id == "run-local-1"
        return self.result

    async def cancel(self, run: LocalEngineRun) -> None:
        self.cancel_calls.append(run)

    async def aclose(self) -> None:
        return None


def test_local_health_readiness_and_team_health_are_safe_and_authorized(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    for principal in (first, second):
        control.change_password(
            principal.user_id,
            "initial user password",
            "established user password",
        )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        source_code_analysis_enabled=True,
    )

    with _RawTestClient(app) as client:
        assert client.get("/v1/health").json() == {"status": "ok"}
        readiness = client.get("/v1/readiness")
        unauthorized = client.get(f"/v1/teams/{first.team_id}/health")

        assert readiness.status_code == 200
        assert set(readiness.json()) == {
            "schema_version",
            "state",
            "capabilities",
        }
        assert "token" not in readiness.text.casefold()
        assert str(tmp_path) not in readiness.text
        assert unauthorized.status_code == 401

        app.state.local_runtime.capability_overrides["smartperfetto"] = "unavailable"
        assert client.get("/v1/readiness").json()["state"] == "degraded"
        app.state.local_runtime.capability_overrides["storage"] = "unavailable"
        assert client.get("/v1/readiness").json()["state"] == "unavailable"
        app.state.local_runtime.capability_overrides.clear()

        first_headers = _authenticated_client(
            client,
            "user01",
            "established user password",
        )
        own = client.get(
            f"/v1/teams/{first.team_id}/health",
            headers=first_headers,
        )
        assert own.status_code == 200
        assert {item["name"] for item in own.json()["capabilities"]} >= {
            "agent",
            "device",
            "source",
        }

        client.cookies.clear()
        second_headers = _authenticated_client(
            client,
            "user02",
            "established user password",
        )
        hidden = client.get(
            f"/v1/teams/{first.team_id}/health",
            headers=second_headers,
        )
        assert hidden.status_code == 404


class _TransientCompletedReportGateway(_FakeSmartPerfettoGateway):
    def __init__(self, result: EngineResult) -> None:
        super().__init__(result)
        self.fetch_calls = 0

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            raise EngineResultValidationError
        return await super().fetch_result(run)


class _InvalidCompletedReportGateway(_FakeSmartPerfettoGateway):
    def __init__(self, result: EngineResult) -> None:
        super().__init__(result)
        self.fetch_calls = 0

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        self.fetch_calls += 1
        raise EngineResultValidationError


@pytest.mark.asyncio
async def test_completed_smartperfetto_report_retries_transient_publication(
    tmp_path: Path,
) -> None:
    gateway = _TransientCompletedReportGateway(_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        data_root=tmp_path,
        poll_interval_seconds=0,
    )
    descriptor = _InputDescriptor(
        kind="trace",
        mime="application/octet-stream",
        size=1,
        sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
    )
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={"trace": _LocalInput(descriptor)},
    )

    result = await app.state.local_runtime._wait_engine_result(
        analysis,
        LocalEngineRun(session_id="session-local-1", run_id="run-local-1"),
    )

    assert result == gateway.result
    assert gateway.fetch_calls == 2


@pytest.mark.asyncio
async def test_completed_smartperfetto_report_retry_is_bounded(tmp_path: Path) -> None:
    gateway = _InvalidCompletedReportGateway(_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        data_root=tmp_path,
        poll_interval_seconds=0,
    )
    descriptor = _InputDescriptor(
        kind="trace",
        mime="application/octet-stream",
        size=1,
        sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
    )
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={"trace": _LocalInput(descriptor)},
    )

    with pytest.raises(EngineResultValidationError):
        await app.state.local_runtime._wait_engine_result(
            analysis,
            LocalEngineRun(session_id="session-local-1", run_id="run-local-1"),
        )

    assert gateway.fetch_calls == 5


class _ScenarioFailingSmartPerfettoGateway(_FakeSmartPerfettoGateway):
    def __init__(self, result: EngineResult, *, failed_profile: str) -> None:
        super().__init__(result)
        self.failed_profile = failed_profile
        self._runs: dict[str, str] = {}

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        self.submissions.append((trace_path.read_bytes(), profile, question))
        run = LocalEngineRun(
            session_id=f"session-{profile}",
            run_id=f"run-{profile}",
        )
        self._runs[run.run_id] = profile
        return run

    async def status(self, run: LocalEngineRun) -> str:
        return (
            "failed"
            if self.failed_profile == "all"
            or self._runs[run.run_id] == self.failed_profile
            else "completed"
        )

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        assert self._runs[run.run_id] != self.failed_profile
        return self.result


class _ScenarioOriginalSmartPerfettoGateway(_FakeSmartPerfettoGateway):
    def __init__(
        self,
        result: EngineResult,
        *,
        original_report_html_bytes: dict[str, bytes],
    ) -> None:
        super().__init__(result)
        self.original_report_html_bytes_by_profile = original_report_html_bytes
        self._runs: dict[str, str] = {}

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        self.submissions.append((trace_path.read_bytes(), profile, question))
        run = LocalEngineRun(
            session_id=f"session-{profile}",
            run_id=f"run-{profile}",
        )
        self._runs[run.run_id] = profile
        return run

    async def status(self, run: LocalEngineRun) -> str:
        assert run.run_id in self._runs
        return "completed"

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        profile = self._runs[run.run_id]
        return EngineResult(
            contract=self.result.contract,
            state=self.result.state,
            payload=self.result.payload,
            original_report_html_bytes=self.original_report_html_bytes_by_profile[
                profile
            ],
        )


class _ScenarioSubmitFailingSmartPerfettoGateway(_ScenarioOriginalSmartPerfettoGateway):
    def __init__(
        self,
        result: EngineResult,
        *,
        failed_profile: str,
        original_report_html_bytes: dict[str, bytes],
    ) -> None:
        super().__init__(
            result,
            original_report_html_bytes=original_report_html_bytes,
        )
        self.failed_profile = failed_profile

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        if profile == self.failed_profile:
            raise RuntimeError("private gateway failure detail")
        return await super().submit(
            trace_path=trace_path,
            profile=profile,
            question=question,
        )


class _BlockingRemoteSmartPerfettoGateway(_FakeSmartPerfettoGateway):
    def __init__(self, result: EngineResult) -> None:
        super().__init__(result)
        self.status_entered = threading.Event()
        self.cancelled = threading.Event()

    async def status(self, run: LocalEngineRun) -> str:
        self.status_entered.set()
        while not self.cancelled.is_set():
            await asyncio.sleep(0.01)
        return "cancelled"

    async def cancel(self, run: LocalEngineRun) -> None:
        self.cancel_calls.append(run)
        self.cancelled.set()


def _authenticated_client(
    client: TestClient, username: str, password: str
) -> dict[str, str]:
    csrf = client.get("/v1/auth/csrf")
    assert csrf.status_code == 200
    login = client.post(
        "/v1/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "x-csrf-token": csrf.json()["csrf_token"],
        },
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    return {
        "Origin": "http://localhost:3000",
        "x-csrf-token": login.json()["csrf_token"],
    }


def test_local_remote_artifact_urls_stream_private_input_and_multipart_part(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    agent_id = UUID("71000000-0000-4000-8000-000000000001")
    execution_id = UUID("73000000-0000-4000-8000-000000000001")
    input_id = UUID("50000000-0000-4000-8000-000000000001")
    input_upload_id = UUID("51000000-0000-4000-8000-000000000001")
    artifact_id = UUID("76000000-0000-4000-8000-000000000001")
    upload_id = UUID("77000000-0000-4000-8000-000000000001")
    input_payload = b"private local apk"
    trace_payload = b"private local trace"

    class Authorizer:
        async def authorize_execution(self, **kwargs: object) -> AgentExecutionAccess:
            assert kwargs["agent_id"] == agent_id
            assert kwargs["execution_id"] == execution_id
            assert kwargs["lease_version"] == 1
            return AgentExecutionAccess(
                team_id=team_id,
                analysis_id=analysis_id,
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=1,
                lease_expires_at=now + timedelta(minutes=5),
                allowed_uploads=("startup_trace", "scroll_trace", "agent_log"),
                scenario_types=("startup", "scroll"),
                input_artifact_ids=(input_id,),
            )

    data_root = tmp_path / "data"
    source = (
        data_root
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / "uploads"
        / f"{input_upload_id}.bin"
    )
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(input_payload)
    source.chmod(0o600)
    service = LocalAgentArtifactService(
        root=data_root,
        public_origin="http://testserver",
        execution_authorizer=Authorizer(),
        clock=lambda: now,
        uuid_source=iter((artifact_id, upload_id)).__next__,
        token_source=iter(("private-input-grant", "private-part-grant")).__next__,
    )
    service.register_input(
        team_id=team_id,
        analysis_id=analysis_id,
        artifact_id=input_id,
        upload_id=input_upload_id,
        mime="application/vnd.android.package-archive",
        size=len(input_payload),
        sha256_b64=base64.b64encode(hashlib.sha256(input_payload).digest()).decode(),
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        state_root=tmp_path / "state",
    )
    app.state.agent_upload_service = service

    with _RawTestClient(app) as client:
        input_slot = asyncio.run(
            service.authorize_input(
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=1,
                artifact_id=input_id,
            )
        )
        downloaded = client.get(urlsplit(input_slot.url).path)
        assert downloaded.status_code == 200
        assert downloaded.content == input_payload
        assert (
            downloaded.headers["content-type"]
            == "application/vnd.android.package-archive"
        )

        upload = asyncio.run(
            service.create_upload(
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=1,
                artifact_kind="startup_trace",
                mime="application/x-perfetto-trace",
                size=len(trace_payload),
                sha256_b64=base64.b64encode(hashlib.sha256(trace_payload).digest()).decode(),
            )
        )
        part = asyncio.run(
            service.authorize_part(
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=1,
                upload_id=upload.upload_id,
                part_number=1,
            )
        )
        uploaded = client.put(urlsplit(part.url).path, content=trace_payload)
        assert uploaded.status_code == 200
        assert uploaded.headers["etag"].startswith('"')


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ("disconnect", "cancel"))
async def test_local_agent_input_response_closes_descriptors_on_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, termination: str
) -> None:
    now = datetime.now(UTC)
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    agent_id = UUID("71000000-0000-4000-8000-000000000001")
    execution_id = UUID("73000000-0000-4000-8000-000000000001")
    input_id = UUID("50000000-0000-4000-8000-000000000001")
    input_upload_id = UUID("51000000-0000-4000-8000-000000000001")
    payload = b"private local apk"

    class Authorizer:
        async def authorize_execution(self, **_kwargs: object) -> AgentExecutionAccess:
            return AgentExecutionAccess(
                team_id=team_id,
                analysis_id=analysis_id,
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=1,
                lease_expires_at=now + timedelta(minutes=5),
                allowed_uploads=("startup_trace",),
                scenario_types=("startup",),
                input_artifact_ids=(input_id,),
            )

    data_root = tmp_path / "data"
    source = (
        data_root
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / "uploads"
        / f"{input_upload_id}.bin"
    )
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(payload)
    source.chmod(0o600)
    service = LocalAgentArtifactService(
        root=data_root,
        public_origin="http://testserver",
        execution_authorizer=Authorizer(),
        clock=lambda: now,
        token_source=lambda: "disconnect-input-grant",
    )
    service.register_input(
        team_id=team_id,
        analysis_id=analysis_id,
        artifact_id=input_id,
        upload_id=input_upload_id,
        mime="application/vnd.android.package-archive",
        size=len(payload),
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
    )
    slot = await service.authorize_input(
        agent_id=agent_id,
        execution_id=execution_id,
        lease_version=1,
        artifact_id=input_id,
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        state_root=tmp_path / "state",
    )
    app.state.agent_upload_service = service
    descriptors: list[int] = []
    original_open = service._open_verified_input

    def observed_open(*args: object):
        result = original_open(*args)  # type: ignore[arg-type]
        descriptors.extend((result.descriptor, result.directory))
        return result

    monkeypatch.setattr(service, "_open_verified_input", observed_open)
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/local/v1/agent-inputs/{grant}"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": urlsplit(slot.url).path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
            "root_path": "",
            "http_version": "1.1",
        }
    )
    response = await route.endpoint(request.path_params.get("grant", "disconnect-input-grant"))
    first_body = asyncio.Event()

    async def receive() -> dict[str, object]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            first_body.set()
            if termination == "disconnect":
                raise OSError("client disconnected")

    response_task = asyncio.create_task(
        response(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
        )
    )
    await first_body.wait()
    if termination == "cancel":
        response_task.cancel()
    with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
        await response_task

    for descriptor in descriptors:
        with pytest.raises(OSError) as raised:
            os.fstat(descriptor)
        assert raised.value.errno == 9


def test_local_app_persists_team_owned_agents_and_source_workspaces(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    for principal in (first, second):
        control.change_password(
            principal.user_id, "initial user password", "established user password"
        )
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        state_root=state_root,
        control_store=control,
        source_code_analysis_enabled=True,
    )
    private_key = Ed25519PrivateKey.generate()
    workspace_id = UUID("73000000-0000-4000-8000-000000000001")

    with _RawTestClient(app) as first_client, _RawTestClient(app) as second_client:
        first_headers = _authenticated_client(
            first_client, "user01", "established user password"
        )
        _authenticated_client(second_client, "user02", "established user password")
        rejected = first_client.post(
            f"/v1/teams/{first.team_id}/agents/registration-codes",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "/Users/private"},
        )
        assert rejected.status_code == 422
        issued = first_client.post(
            f"/v1/teams/{first.team_id}/agents/registration-codes",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "Build Mac"},
        )
        assert issued.status_code == 201, issued.text
        registered = first_client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": issued.json()["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "build-mac",
                "os_version": "macOS 15",
            },
        )
        assert registered.status_code == 201, registered.text
        credentials = registered.json()
        heartbeat = first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "build-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [{
                    "workspace_id": str(workspace_id),
                    "name": "RivotekMedia",
                    "state": "ready",
                    "git_branch": "main",
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 0,
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [],
                }],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert (
            first_client.get(f"/v1/teams/{first.team_id}/agents").json()["agents"][0][
                "state"
            ]
            == "online"
        )
        workspaces = first_client.get(f"/v1/teams/{first.team_id}/source-workspaces")
        assert workspaces.status_code == 200
        assert workspaces.json()["workspaces"][0]["workspace_id"] == str(workspace_id)
        renamed = first_client.patch(
            f"/v1/teams/{first.team_id}/agents/{issued.json()['agent_id']}",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "Renamed Mac"},
        )
        assert renamed.status_code == 200
        foreign_binding = first_client.post(
            f"/v1/teams/{first.team_id}/analyses",
            headers=first_headers,
            json={
                "schema_version": "1.1", "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": "com.example",
                "inputs": [{
                    "kind": "trace", "mime": "application/octet-stream", "size": 1,
                    "sha256_b64": base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                }],
                "source_binding": {
                    "provider_kind": "agent_workspace",
                    "agent_id": "71000000-0000-4000-8000-000000000002",
                    "workspace_id": str(workspace_id),
                    "snapshot_policy": "tracked_worktree",
                    "validation_profile_id": None,
                },
            },
        )
        assert foreign_binding.status_code == 404
        assert second_client.get(f"/v1/teams/{first.team_id}/agents").status_code == 404
        assert (
            second_client.get(f"/v1/teams/{second.team_id}/agents").json()["agents"]
            == []
        )

    payload = (state_root / "agents" / "agents.json").read_text(encoding="utf-8")
    assert credentials["access_token"] not in payload
    assert credentials["refresh_token"] not in payload
    assert "/Users/" not in payload

    restarted = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        state_root=state_root,
        control_store=control,
        source_code_analysis_enabled=True,
    )
    with _RawTestClient(restarted) as client:
        headers = _authenticated_client(client, "user01", "established user password")
        assert (
            client.get(f"/v1/teams/{first.team_id}/agents", headers=headers).json()[
                "agents"
            ][0]["name"]
            == "Renamed Mac"
        )
        assert (
            client.get(
                f"/v1/teams/{first.team_id}/source-workspaces", headers=headers
            ).json()["workspaces"][0]["name"]
            == "RivotekMedia"
        )


def test_local_agent_control_refresh_unregister_and_team_devices(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    for principal in (first, second):
        control.change_password(principal.user_id, "initial user password", "established user password")
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
    )
    private_key = Ed25519PrivateKey.generate()
    with _RawTestClient(app) as first_client, _RawTestClient(app) as second_client:
        first_headers = _authenticated_client(first_client, "user01", "established user password")
        _authenticated_client(second_client, "user02", "established user password")
        issued = first_client.post(
            f"/v1/teams/{first.team_id}/agents/registration-codes",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "Build Mac"},
        ).json()
        registered = first_client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1", "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos", "agent_version": "1.2.3",
                "hostname": "build-mac", "os_version": "macOS 15.0",
            },
        ).json()
        timestamp = int(time.time())
        refreshed = first_client.post(
            "/v1/agent/token/refresh",
            json={
                "schema_version": "1.1", "agent_id": registered["agent_id"],
                "refresh_token": registered["refresh_token"], "nonce": "n" * 22,
                "timestamp": timestamp,
                "signature_b64": encode_signature(private_key.sign(refresh_proof_message(UUID(registered["agent_id"]), "n" * 22, timestamp))),
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        heartbeat_payload = {
            "schema_version": "1.0", "agent_version": "1.2.3", "platform": "macos",
            "hostname": "build-mac", "observed_at": datetime.now().astimezone().isoformat(),
            "clock_skew_ms": 0, "disk_available_bytes": 1024,
            "execution_slot": {"state": "idle", "execution_id": None},
            "devices": [{
                "client_ref": "74000000-0000-4000-8000-000000000001",
                    "serial": "emulator-5554",
                    "manufacturer": "Google",
                    "model": "Pixel",
                    "android_release": "16",
                    "api_level": 36,
                    "connection_type": "usb",
                    "adb_state": "device",
                    "battery_percent": 80,
                    "temperature_c": None,
                    "storage_available_bytes": 1024,
                    "property_error_code": None,
        }
            ],
        }
        assert (
            first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {registered['access_token']}"},
            json=heartbeat_payload,
            ).status_code
            == 401
        )
        assert (
            first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
            json=heartbeat_payload,
            ).status_code
            == 200
        )
        devices = first_client.get(f"/v1/teams/{first.team_id}/devices")
        assert devices.status_code == 200
        assert devices.json()["devices"][0]["serial_suffix"] == "5554"
        assert (
            second_client.get(f"/v1/teams/{second.team_id}/devices").json()["devices"]
            == []
        )
        accepted = first_client.post(
            f"/v1/teams/{first.team_id}/analyses",
            headers=first_headers,
            json={
                "schema_version": "1.0", "analysis_mode": "device",
                "device_id": devices.json()["devices"][0]["device_id"],
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": 1,
                    "sha256_b64": base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                },
            },
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["state"] == "uploading"
        first_client.cookies.clear()
        assert (
            first_client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
            ).json()["action"]
            == "wait"
        )
        revoked = first_client.post(
            "/v1/agent/unregister",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
        )
        assert revoked.status_code == 200
        assert (
            first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
            json=heartbeat_payload,
            ).status_code
            == 401
        )


def test_users_open_one_auto_enrollment_slot_and_delete_their_own_agent(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    for principal in (first, second):
        control.change_password(
            principal.user_id, "initial user password", "established user password"
        )
    app = create_local_app(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        auto_agent_enrollment=True,
    )
    private_key = Ed25519PrivateKey.generate()

    with (
        _RawTestClient(app) as first_client,
        _RawTestClient(app) as second_client,
        _RawTestClient(app) as agent_client,
    ):
        first_headers = _authenticated_client(
            first_client, "user01", "established user password"
        )
        second_headers = _authenticated_client(
            second_client, "user02", "established user password"
        )
        opened = first_client.post(
            f"/v1/teams/{first.team_id}/agent-enrollment",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "测试电脑"},
        )
        assert opened.status_code == 201, opened.text
        assert opened.json()["enrollment"]["name"] == "测试电脑"
        assert second_client.post(
            f"/v1/teams/{second.team_id}/agent-enrollment",
            headers=second_headers,
            json={"schema_version": "1.0", "name": "另一台电脑"},
        ).status_code == 409

        registered = agent_client.post(
            "/v1/agent/auto-register",
            json={
                "schema_version": "1.1",
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "linux",
                "agent_version": "0.1.0",
                "hostname": "ubuntu-lab",
                "os_version": "Ubuntu 24.04",
            },
        )

        assert registered.status_code == 201, registered.text
        credentials = registered.json()
        assert credentials["schema_version"] == "1.1"
        assert credentials["team_id"] == str(first.team_id)
        heartbeat = agent_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "0.1.0",
                "platform": "linux",
                "hostname": "ubuntu-lab",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text

        agents = first_client.get(
            f"/v1/teams/{first.team_id}/agents", headers=first_headers
        ).json()["agents"]
        assert [(item["name"], item["hostname"], item["state"]) for item in agents] == [
            ("测试电脑", "ubuntu-lab", "online")
        ]
        assert second_client.get(
            f"/v1/teams/{second.team_id}/agents", headers=second_headers
        ).json()["agents"] == []
        deleted = first_client.post(
            f"/v1/teams/{first.team_id}/agents/{credentials['agent_id']}/revoke",
            headers=first_headers,
            json={"schema_version": "1.0"},
        )
        assert deleted.status_code == 200, deleted.text
        assert first_client.get(
            f"/v1/teams/{first.team_id}/agents", headers=first_headers
        ).json()["agents"] == []
        assert agent_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "0.1.0",
                "platform": "linux",
                "hostname": "ubuntu-lab",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [],
            },
        ).status_code == 401


def test_local_agent_auto_registration_is_disabled_without_explicit_server_opt_in(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    control.ensure_user("admin", "initial admin password", True)
    app = create_local_app(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        auto_agent_enrollment=False,
    )
    private_key = Ed25519PrivateKey.generate()

    with _RawTestClient(app) as client:
        response = client.post(
            "/v1/agent/auto-register",
            json={
                "schema_version": "1.1",
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "0.1.0",
                "hostname": "developer-mac",
                "os_version": "macOS 15",
            },
        )

    assert response.status_code == 404


def test_local_agent_auto_registration_waits_for_a_user_opened_slot(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    control.ensure_user("admin", "initial admin password", True)
    app = create_local_app(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        auto_agent_enrollment=True,
    )

    with _RawTestClient(app) as client:
        response = client.post(
            "/v1/agent/auto-register",
            json={
                "schema_version": "1.1",
                "public_key_b64": encode_ed25519_public_key(
                    Ed25519PrivateKey.generate().public_key()
                ),
                "platform": "linux",
                "agent_version": "0.1.0",
                "hostname": "ubuntu-lab",
                "os_version": "Ubuntu 24.04",
            },
        )

    assert response.status_code == 409


@pytest.mark.parametrize("value", ["/tmp/agent", r"C:\\agent", r"\\\\server\\agent", "~/agent", "../agent"])
def test_local_agent_registration_rejects_path_shaped_public_metadata(
    tmp_path: Path, value: str
) -> None:
    control = LocalControlStore(tmp_path / "control")
    principal = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(principal.user_id, "initial user password", "established user password")
    state_root = tmp_path / "state"
    app = create_local_app(data_root=tmp_path / "data", state_root=state_root, control_store=control)
    with _RawTestClient(app) as client:
        headers = _authenticated_client(client, "user01", "established user password")
        issued = client.post(
            f"/v1/teams/{principal.team_id}/agents/registration-codes", headers=headers,
            json={"schema_version": "1.0", "name": "Build Mac"},
        ).json()
        rejected = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0", "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(Ed25519PrivateKey.generate().public_key()),
                "platform": "macos", "agent_version": "1.2.3", "hostname": value,
                "os_version": value,
            },
        )
    assert rejected.status_code == 401
    assert value not in (state_root / "agents" / "agents.json").read_text(encoding="utf-8")


def test_local_analyses_are_isolated_between_logged_in_user_teams(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    control.change_password(
        first.user_id, "initial user password", "established user password"
    )
    control.change_password(
        second.user_id, "initial user password", "established user password"
    )
    data_root = tmp_path / "data"
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        control_store=control,
    )

    with _RawTestClient(app) as first_client, _RawTestClient(app) as second_client:
        first_headers = _authenticated_client(
            first_client, "user01", "established user password"
        )
        second_headers = _authenticated_client(
            second_client, "user02", "established user password"
        )
        analysis_id, checksum = _create_trace_analysis(
            first_client,
            team_id=str(first.team_id),
            headers=first_headers,
        )
        created = first_client.get(f"/v1/teams/{first.team_id}/analyses/{analysis_id}")
        assert created.json()["team_id"] == str(first.team_id)

        first_slot = first_client.post(
            f"/v1/teams/{first.team_id}/analyses/{analysis_id}/uploads",
            headers=first_headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        ).json()["upload"]

        cross_paths = (
            ("GET", f"/v1/teams/{second.team_id}/analyses/{analysis_id}", None),
            (
                "POST",
                f"/v1/teams/{second.team_id}/analyses/{analysis_id}/uploads",
                {
                    "artifact_kind": "trace",
                    "mime": "application/octet-stream",
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            ),
            ("POST", f"/v1/teams/{second.team_id}/analyses/{analysis_id}/cancel", None),
            (
                "POST",
                f"/v1/teams/{second.team_id}/analyses/{analysis_id}/finalize-upload",
                {
                    "upload_id": first_slot["upload_id"],
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            ),
            ("GET", f"/v1/teams/{second.team_id}/analyses/{analysis_id}/report", None),
            (
                "POST",
                f"/v1/teams/{second.team_id}/analyses/{analysis_id}/synthesis-runs",
                None,
            ),
        )
        for method, path, body in cross_paths:
            response = second_client.request(
                method,
                path,
                headers=second_headers,
                json=body,
            )
            assert response.status_code == 404
            assert analysis_id not in response.text
            assert str(first.team_id) not in response.text

        active_first = first_client.get(
            f"/v1/teams/{first.team_id}/analyses?status=active"
        )
        active_second = second_client.get(
            f"/v1/teams/{second.team_id}/analyses?status=active"
        )
        assert [item["analysis_id"] for item in active_first.json()["analyses"]] == [
            analysis_id
        ]
        assert active_second.json()["analyses"] == []

        put = urlsplit(first_slot["put_url"])
        assert (
            first_client.put(
                f"{put.path}?{put.query}",
                content=b"background-local-trace",
                headers=first_slot["required_headers"],
            ).status_code
            == 200
        )
        assert (
            first_client.post(
                f"/v1/teams/{first.team_id}/analyses/{analysis_id}/finalize-upload",
                headers=first_headers,
                json={
                    "upload_id": first_slot["upload_id"],
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            ).status_code
            == 200
        )
        for _ in range(100):
            completed = first_client.get(
                f"/v1/teams/{first.team_id}/analyses/{analysis_id}"
            ).json()
            if completed["report_available"]:
                break
            time.sleep(0.01)
        assert completed["report_available"] is True
        assert (
            first_client.get(
                f"/v1/teams/{first.team_id}/analyses/{analysis_id}/report"
            ).status_code
            == 200
        )
        first_list = first_client.get(f"/v1/teams/{first.team_id}/analyses")
        second_list = second_client.get(f"/v1/teams/{second.team_id}/analyses")
        assert [item["analysis_id"] for item in first_list.json()["analyses"]] == [
            analysis_id
        ]
        assert second_list.json()["analyses"] == []

        first_recovery = first_client.post(
            f"/v1/teams/{first.team_id}/local-recoveries",
            headers=first_headers,
            json=_recovery_request(),
        )
        second_recovery = second_client.post(
            f"/v1/teams/{second.team_id}/local-recoveries",
            headers=second_headers,
            json=_recovery_request(),
        )
        assert first_recovery.status_code == 201
        assert second_recovery.status_code == 201
        assert (
            first_recovery.json()["analysis_id"]
            != second_recovery.json()["analysis_id"]
        )
        assert first_recovery.json()["team_id"] == str(first.team_id)
        assert second_recovery.json()["team_id"] == str(second.team_id)
        for client, owner, recovery in (
            (first_client, first, first_recovery),
            (second_client, second, second_recovery),
        ):
            for _ in range(100):
                recovered_state = client.get(
                    f"/v1/teams/{owner.team_id}/analyses/{recovery.json()['analysis_id']}"
                ).json()
                if recovered_state["report_available"]:
                    break
                time.sleep(0.01)
            assert recovered_state["report_available"] is True

    assert (
        data_root
        / "teams"
        / str(first.team_id)
        / "analyses"
        / analysis_id
        / "state.json"
    ).is_file()
    assert not (data_root / "analyses" / analysis_id).exists()


def test_local_auth_requires_login_and_exposes_current_principal(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    admin = control.ensure_user("ray_wu", "initial admin password", True).principal
    ordinary = control.ensure_user("user01", "initial user password", False).principal
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        assert client.get("/v1/me").status_code == 401
        csrf = client.get("/v1/auth/csrf")
        assert csrf.status_code == 200
        assert "HttpOnly" in csrf.headers["set-cookie"]
        assert "SameSite=strict" in csrf.headers["set-cookie"]
        assert "Secure" not in csrf.headers["set-cookie"]
        logged_in = client.post(
            "/v1/auth/login",
            headers={
                "Origin": "http://localhost:3000",
                "x-csrf-token": csrf.json()["csrf_token"],
            },
            json={"username": "user01", "password": "initial user password"},
        )
        assert logged_in.status_code == 200
        me = client.get("/v1/me")
        assert me.status_code == 200
        assert me.headers["cache-control"] == "no-store"
        assert me.json()["user"] == {
            "id": str(ordinary.user_id),
            "username": "user01",
            "is_platform_admin": False,
            "must_change_password": True,
        }
        assert me.json()["memberships"] == [
            {
                "id": str(ordinary.team_id),
                "team": {"id": str(ordinary.team_id), "name": "user01 local team"},
                "role": "owner",
            }
        ]
        blocked = client.get(f"/v1/teams/{ordinary.team_id}/devices")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "password_change_required"
        assert client.get(f"/v1/teams/{admin.team_id}/devices").status_code == 404


def test_local_auth_changes_initial_password_and_invalidates_old_session(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        headers = _authenticated_client(client, "user01", "initial user password")
        changed = client.post(
            "/v1/auth/change-password",
            headers=headers,
            json={
                "current_password": "initial user password",
                "new_password": "changed user password",
            },
        )
        assert changed.status_code == 200
        assert set(changed.json()) == {"schema_version", "csrf_token"}
        assert client.get("/v1/me").json()["user"]["must_change_password"] is False
        devices = client.get(f"/v1/teams/{user.team_id}/devices")
        assert devices.status_code == 200
        logout = client.post(
            "/v1/auth/logout",
            headers={
                "Origin": "http://localhost:3000",
                "x-csrf-token": changed.json()["csrf_token"],
            },
        )
        assert logout.status_code == 204
        assert client.get("/v1/me").status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"username": "user01"},
        {"username": "user01", "password": "initial user password", "extra": "no"},
        {"username": 1, "password": "initial user password"},
    ],
)
def test_local_login_rejects_malformed_bodies_as_redacted_invalid_credentials(
    tmp_path: Path,
    body: dict[str, object],
) -> None:
    control = LocalControlStore(tmp_path / "control")
    control.ensure_user("user01", "initial user password", False)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        response = client.post(
            "/v1/auth/login",
            headers={"Origin": "http://localhost:3000", "x-csrf-token": csrf},
            json=body,
        )

    assert response.status_code == 401
    assert set(response.json()) == {"schema_version", "error"}
    assert set(response.json()["error"]) == {
        "code",
        "message",
        "retryable",
        "request_id",
    }
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert "user01" not in response.text
    assert "initial user password" not in response.text


def test_local_device_requires_a_changed_authenticated_principal(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        assert client.get("/v1/device").status_code == 401
        headers = _authenticated_client(client, "user01", "initial user password")
        blocked = client.get("/v1/device")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "password_change_required"
        changed = client.post(
            "/v1/auth/change-password",
            headers=headers,
            json={
                "current_password": "initial user password",
                "new_password": "changed user password",
            },
        )
        assert changed.status_code == 200
        assert client.get("/v1/device").status_code == 200
        assert (
            control.resolve_session(
                client.cookies.get("perfpilot_local_session", "")
            ).username
            == user.username
        )


def test_concurrent_authenticated_csrf_bootstraps_keep_the_same_session(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    seeded = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(
        seeded.user_id,
        "initial user password",
        "changed user password",
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        _authenticated_client(client, "user01", "changed user password")
        session_token = client.cookies["perfpilot_local_session"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: client.get("/v1/auth/csrf"), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["csrf_token"] for response in responses}) == 1
    assert all("set-cookie" not in response.headers for response in responses)
    assert control.resolve_session(session_token) is not None


@pytest.mark.parametrize(
    "body",
    [
        {"current_password": "initial user password"},
        {"new_password": "changed user password"},
        {
            "current_password": "initial user password",
            "new_password": "changed user password",
            "marker": "secret-marker",
        },
        {"current_password": 1, "new_password": "changed user password"},
        {"current_password": "x" * 1025, "new_password": "changed user password"},
        {"current_password": "initial user password", "new_password": "x" * 1025},
    ],
)
def test_change_password_validation_is_a_closed_redacted_422(
    tmp_path: Path,
    body: dict[str, object],
) -> None:
    control = LocalControlStore(tmp_path / "control")
    control.ensure_user("user01", "initial user password", False)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        headers = _authenticated_client(client, "user01", "initial user password")
        response = client.post("/v1/auth/change-password", headers=headers, json=body)

    assert response.status_code == 422
    assert set(response.json()) == {"schema_version", "error"}
    assert set(response.json()["error"]) == {
        "code",
        "message",
        "retryable",
        "request_id",
    }
    assert response.json()["error"]["code"] == "credential_validation_failed"
    assert "secret-marker" not in response.text
    assert "initial user password" not in response.text


def test_local_runtime_accepts_https_or_private_network_http_origins() -> None:
    assert _public_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert _public_origin("http://10.166.0.125:8000") == "http://10.166.0.125:8000"
    assert _public_origin("https://api.example.test") == "https://api.example.test"

    with pytest.raises(ValueError, match="HTTPS or private-network HTTP"):
        _public_origin("http://8.8.8.8:8000")
    with pytest.raises(ValueError, match="HTTPS or private-network HTTP"):
        _public_origin("https://api.example.test/path")


def test_local_runtime_rejects_malformed_persisted_ai_round_state() -> None:
    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        _restore_ai_rounds(
            [{"round": 1, "role": "report", "state": [], "attempts": 0}]
        )


def test_local_source_binding_degrades_when_no_source_agent_is_available() -> None:
    document = _source_code_analysis_unavailable_document(
        SourceBinding(
            provider_kind="agent_workspace",
            agent_id=UUID("91000000-0000-4000-8000-000000000001"),
            workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
            snapshot_policy="tracked_worktree",
            validation_profile_id=None,
        )
    )

    assert document["context_state"] == "unavailable"
    assert document["failure_code"] == "source_agent_unavailable"
    assert document["match_summary"] == "none"


def test_local_bound_trace_dispatches_signed_source_context_task(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user(
        "user01", "initial user password", False
    ).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    smartperfetto_result = _smartperfetto_result()
    smartperfetto_result.payload["report"]["dataEnvelopes"][0]["evidence"][0][
        "fields"
    ]["mapped_symbol"] = "demo.Startup.init"
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(smartperfetto_result),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        source_code_analysis_enabled=True,
        poll_interval_seconds=0.001,
    )
    private_key = Ed25519PrivateKey.generate()
    workspace_id = UUID("73000000-0000-4000-8000-000000000001")
    validation_profile_id = UUID("96000000-0000-4000-8000-000000000001")
    trace = b"bound-local-trace"
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Source Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "source-mac",
                "os_version": "macOS 15",
            },
        ).json()
        agent_headers = {
            "Authorization": f"Bearer {credentials['access_token']}"
        }
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers=agent_headers,
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "source-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [
                    {
                        "workspace_id": str(workspace_id),
                        "name": "RivotekMedia",
                        "state": "ready",
                        "git_branch": "main",
                        "git_head": "a" * 40,
                        "tracked_dirty_count": 0,
                        "snapshot_policy": "tracked_worktree",
                        "validation_profiles": [
                            {
                                "profile_id": str(validation_profile_id),
                                "name": "Unit tests",
                            }
                        ],
                    }
                ],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        created = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.1",
                "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": "com.example",
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
                "source_binding": {
                    "provider_kind": "agent_workspace",
                    "agent_id": credentials["agent_id"],
                    "workspace_id": str(workspace_id),
                    "snapshot_policy": "tracked_worktree",
                    "validation_profile_id": str(validation_profile_id),
                },
            },
        )
        assert created.status_code == 201, created.text
        analysis_id = created.json()["analysis_id"]
        _upload_and_finalize_trace(
            client,
            team_id=str(user.team_id),
            analysis_id=analysis_id,
            headers=browser_headers,
            checksum=checksum,
            trace=trace,
        )

        client.cookies.clear()
        for _ in range(100):
            delivery = client.get(
                "/v1/agent/tasks/next?wait_seconds=0", headers=agent_headers
            )
            if delivery.json().get("task_kind") == "source":
                break
            time.sleep(0.01)

        assert delivery.status_code == 200, delivery.text
        assert delivery.json()["schema_version"] == "1.1"
        assert delivery.json()["task_kind"] == "source"
        assert delivery.json()["snapshot"]["task_type"] == "source_context"
        assert delivery.json()["snapshot"]["analysis_id"] == analysis_id
        assert delivery.json()["snapshot"]["aud"] == "perfpilot-agent"
        assert delivery.json()["signature_b64"]
        assert delivery.json()["lease_token"]
        validate_source_task_snapshot(
            delivery.json()["snapshot"], now=datetime.now().astimezone()
        )
        snapshot = delivery.json()["snapshot"]
        hint = snapshot["finding_hints"][0]
        content = "fun init() = loadNow()"
        result = {
            "snapshot_id": "94000000-0000-4000-8000-000000000001",
            "snapshot_hash": "b" * 64,
            "git_head": "a" * 40,
            "tracked_dirty_count": 0,
            "fragments": [
                {
                    "source_ref_id": "97000000-0000-4000-8000-000000000001",
                    "relative_path": "app/src/main/java/demo/Startup.kt",
                    "language": "kotlin",
                    "symbol": "demo.Startup.init",
                    "start_line": 1,
                    "end_line": 1,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "snapshot_hash": "b" * 64,
                    "finding_ids": [hint["finding_id"]],
                    "evidence_ids": hint["evidence_ids"],
                    "rule_ids": ["android.startup.eager_initialization"],
                    "match_signals": ["trace_symbol"],
                }
            ],
            "exclusions": [],
            "truncated": False,
        }
        unsigned = {
            "schema_version": "1.0",
            "task_type": "source_context",
            "execution_id": snapshot["execution_id"],
            "analysis_id": analysis_id,
            "team_id": str(user.team_id),
            "agent_id": credentials["agent_id"],
            "workspace_id": str(workspace_id),
            "lease_version": snapshot["lease_version"],
            "state": "completed",
            "result": result,
        }
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        completion = client.post(
            f"/v1/agent/tasks/{snapshot['execution_id']}/complete",
            headers={
                **agent_headers,
                "x-perfpilot-lease-token": delivery.json()["lease_token"],
            },
            json={
                **unsigned,
                "signature_b64": base64.b64encode(private_key.sign(canonical)).decode(),
            },
        )
        assert completion.status_code == 200, completion.text
        _authenticated_client(client, "user01", "established user password")
        for _ in range(100):
            report_response = client.get(
                f"/v1/teams/{user.team_id}/analyses/{analysis_id}/report"
            )
            if report_response.status_code == 200:
                break
            time.sleep(0.01)
    assert report_response.status_code == 200, report_response.text
    source_code = report_response.json()["source_code"]
    assert source_code["context_state"] == "available"
    assert source_code["match_summary"] == "strong"
    assert source_code["source_refs"][0]["relative_path"] == (
        "app/src/main/java/demo/Startup.kt"
    )
    assert source_code["source_refs"][0]["symbol"] == "demo.Startup.init"
    assert source_code["fixes"][0]["diff"].startswith("diff --git a/app/")
    assert "重复冷启动" in source_code["fixes"][0]["retest_target"]
    private_context = (
        tmp_path
        / "data"
        / "teams"
        / str(user.team_id)
        / "analyses"
        / analysis_id
        / "source-context.json"
    )
    assert private_context.is_file()
    assert str(tmp_path) not in report_response.text


@pytest.mark.asyncio
async def test_local_bound_source_timeout_degrades_without_blocking_report(
    tmp_path: Path,
) -> None:
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=UUID("91000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
        source_code_analysis_enabled=True,
        source_wait_seconds=0.01,
        poll_interval_seconds=0.001,
    )
    runtime = app.state.local_runtime
    trace = _InputDescriptor(
        kind="trace",
        mime="application/octet-stream",
        size=1,
        sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
    )
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={"trace": _LocalInput(trace)},
        source_binding=binding,
        source_code_analysis=_source_code_analysis_document(binding),
    )

    result = _smartperfetto_result()
    context = await runtime._await_source_context(analysis, result)
    prepared = _prepare_local_report(analysis, result)
    report = _compose_local_report(
        analysis,
        prepared,
        generation=1,
        synthesis=None,
        synthesis_failure_code="ai_not_configured",
        rounds=(),
        synthesizer=None,
    )

    assert context is None
    assert analysis.source_code_analysis["context_state"] == "unavailable"
    assert analysis.source_code_analysis["failure_code"] == "source_context_timeout"
    assert report["state"] == "partially_completed"
    assert report["source_code"]["context_state"] == "unavailable"


def test_local_weak_source_context_never_publishes_paths_or_symbols() -> None:
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=UUID("91000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
    )
    path_marker = "private/path/WeakSource.kt"
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={
            "trace": _LocalInput(
                _InputDescriptor(
                    kind="trace",
                    mime="application/octet-stream",
                    size=1,
                    sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                )
            )
        },
        source_binding=binding,
        source_code_analysis={
            **_source_code_analysis_document(binding),
            "context_state": "available",
            "match_summary": "weak",
        },
        source_context={
            "snapshot_id": "94000000-0000-4000-8000-000000000001",
            "snapshot_hash": "b" * 64,
            "git_head": "a" * 40,
            "tracked_dirty_count": 0,
            "trust": "untrusted_data_not_instructions",
            "match_summary": "weak",
            "fragments": [
                {
                    "source_ref_id": "97000000-0000-4000-8000-000000000001",
                    "relative_path": path_marker,
                    "language": "kotlin",
                    "symbol": "demo.WeakSource.init",
                    "start_line": 1,
                    "end_line": 1,
                    "content_sha256": hashlib.sha256(b"weak").hexdigest(),
                    "content": "weak",
                    "finding_ids": [],
                    "evidence_ids": [],
                    "rule_ids": ["android.startup.eager_initialization"],
                    "match_grade": "weak",
                }
            ],
            "exclusions": [],
            "truncated": False,
        },
    )

    public_source = _local_source_code_document(analysis)
    serialized = json.dumps(public_source, sort_keys=True)

    assert public_source["match_summary"] == "weak"
    assert public_source["source_refs"] == []
    assert public_source["fixes"] == []
    assert path_marker not in serialized
    assert "demo.WeakSource.init" not in serialized


def test_persisted_source_strong_projection_rebuild_retains_validated_context() -> None:
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=UUID("91000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
        snapshot_policy="tracked_worktree",
        validation_profile_id=UUID("96000000-0000-4000-8000-000000000001"),
    )
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={
            "trace": _LocalInput(
                _InputDescriptor(
                    kind="trace",
                    mime="application/octet-stream",
                    size=1,
                    sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                )
            )
        },
        source_binding=binding,
    )
    result = _smartperfetto_result()
    result.payload["report"]["dataEnvelopes"][0]["evidence"][0]["fields"][
        "mapped_symbol"
    ] = "demo.Startup.init"
    core = _prepare_local_report(analysis, result)
    authority = derive_source_authority(core.core_document)
    content = "fun init() = loadNow()"
    context = validate_source_context(
        {
            "snapshot_id": "94000000-0000-4000-8000-000000000001",
            "snapshot_hash": "b" * 64,
            "git_head": "a" * 40,
            "tracked_dirty_count": 0,
            "fragments": [{
                "source_ref_id": "97000000-0000-4000-8000-000000000001",
                "relative_path": "app/src/main/java/demo/Startup.kt",
                "language": "kotlin",
                "symbol": "demo.Startup.init",
                "start_line": 1,
                "end_line": 1,
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "snapshot_hash": "b" * 64,
                "finding_ids": [authority.finding_ids[0]],
                "evidence_ids": [authority.evidence_ids[0]],
                "rule_ids": ["android.startup.eager_initialization"],
                "match_signals": ["trace_symbol"],
            }],
            "exclusions": [],
            "truncated": False,
        },
        direct_identifiers=authority.direct_identifiers,
        allowed_finding_ids=authority.finding_ids,
        allowed_evidence_ids=authority.evidence_ids,
    )
    analysis.source_context = context
    analysis.source_code_analysis = {
        **_source_code_analysis_document(binding),
        "context_state": "available",
        "match_summary": "strong",
    }
    prepared = _prepare_local_report(analysis, result, source_context=context)

    restored = _prepared_from_persisted_documents(
        analysis,
        source_value=prepared.source_report,
        core_value=prepared.core_document,
        projection_value=prepared.projection.document,
        report_value=None,
    )

    assert restored.projection.canonical_bytes == prepared.projection.canonical_bytes
    assert restored.projection.document["source_context"]["match_summary"] == "strong"

    tampered_context = copy.deepcopy(context)
    tampered_content = "fun init() = executeUntrustedPayload()"
    tampered_context["fragments"][0]["content"] = tampered_content
    tampered_context["fragments"][0]["content_sha256"] = hashlib.sha256(
        tampered_content.encode()
    ).hexdigest()
    analysis.source_context = tampered_context
    with pytest.raises(
        _PersistedLocalEvidenceError,
        match="ai_source_evidence_invalid",
    ):
        _prepared_from_persisted_documents(
            analysis,
            source_value=prepared.source_report,
            core_value=prepared.core_document,
            projection_value=prepared.projection.document,
            report_value=None,
        )


def test_script_capture_normalization_uses_completed_trace_digest_without_apk() -> None:
    digest = base64.b64encode(hashlib.sha256(b"captured trace").digest()).decode()
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={},
        analysis_mode="device",
        capture_configuration={
            "test_type": "cold_start",
            "launch_mode": "automatic",
            "duration_seconds": 15,
            "package_name": "com.rivotek.mediacenter",
            "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        },
    )

    normalized = _normalize_local_smartperfetto_result(
        analysis,
        _smartperfetto_result(),
        profile="startup",
        input_sha256_b64=digest,
    )
    startup_only = normalized.report.document
    startup_only["scenario_reports"] = [
        item
        for item in startup_only["scenario_reports"]
        if item["scenario_type"] == "startup"
    ]
    startup_only["core_state"] = "complete"
    startup_only_bytes = canonical_json_bytes(
        validate_contract("normalized-trace-report", startup_only)
    )
    normalized = replace(
        normalized,
        report=NormalizedTraceReport(
            canonical_bytes=startup_only_bytes,
            sha256_b64=base64.b64encode(
                hashlib.sha256(startup_only_bytes).digest()
            ).decode(),
        ),
    )
    prepared = _prepare_local_report(
        analysis,
        _smartperfetto_result(),
        primary_profile="startup",
        primary_normalized=normalized,
        include_memory=False,
        remote_completed_scenarios=frozenset({"startup"}),
    )

    assert normalized.report.document["schema_version"] == "1.0"
    assert [
        item["scenario_type"] for item in prepared.core_document["scenario_reports"]
    ] == ["startup"]
    assert all(
        "滑动" not in item["summary"]
        for item in prepared.core_document["limitations"]
    )


def test_script_capture_question_fences_smartperfetto_to_selected_package() -> None:
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={},
        analysis_mode="device",
        capture_configuration={
            "test_type": "cold_start",
            "launch_mode": "automatic",
            "duration_seconds": 15,
            "package_name": "com.rivotek.mediacenter",
            "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        },
    )

    question = _remote_capture_question(analysis, scenario_type="startup")

    assert "com.rivotek.mediacenter" in question
    assert "不要替换成其他应用" in question


def test_uploaded_trace_drops_findings_for_another_target_package() -> None:
    analysis = _LocalAnalysis(
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={
            "trace": _LocalInput(
                _InputDescriptor(
                    kind="trace",
                    mime="application/octet-stream",
                    size=1,
                    sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                )
            )
        },
        trace_test_type="cold_start",
        target_package_name="com.rivotek.mediacenter",
    )

    normalized = _normalize_local_smartperfetto_result(
        analysis,
        _live_smartperfetto_result(),
        profile="startup",
    ).report.document

    assert normalized["core_state"] == "partial"
    assert normalized["scenario_reports"][0]["findings"] == []
    assert normalized["scenario_reports"][0]["metrics"] == []
    assert {item["code"] for item in normalized["limitations"]} >= {
        "smartperfetto.target_package_mismatch"
    }


def test_local_restart_degrades_waiting_source_and_finishes_persisted_report(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user(
        "user01", "initial user password", False
    ).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=UUID("91000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
    )
    analysis = _LocalAnalysis(
        team_id=user.team_id,
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        profile="startup",
        question=None,
        inputs={
            "trace": _LocalInput(
                _InputDescriptor(
                    kind="trace",
                    mime="application/octet-stream",
                    size=1,
                    sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                ),
                artifact_id="50000000-0000-4000-8000-000000000001",
                finalized=True,
            )
        },
        state="analyzing",
        source_binding=binding,
        source_code_analysis=_source_code_analysis_document(binding),
        stages={
            "input_validation": "completed",
            "smartperfetto": "completed",
            "perfpilot_ai": "pending",
            "report": "pending",
        },
    )
    source_result = _smartperfetto_result()
    prepared = _prepare_local_report(analysis, source_result)
    store = LocalAnalysisStore(data_root)
    analysis.smartperfetto_original = persist_smartperfetto_original(
        root=data_root,
        team_id=user.team_id,
        analysis_id=analysis.analysis_id,
        payload=b"<!DOCTYPE html><html><body>SmartPerfetto</body></html>",
    )
    store.save_document(
        user.team_id,
        analysis.analysis_id,
        "normalized-core.json",
        prepared.core_document,
    )
    store.save_document(
        user.team_id,
        analysis.analysis_id,
        "smartperfetto-report.json",
        prepared.source_report,
    )
    store.save_document(
        user.team_id,
        analysis.analysis_id,
        "projection.json",
        prepared.projection.document,
    )
    analysis.evidence_format_version = _LOCAL_EVIDENCE_FORMAT_VERSION
    analysis.evidence_manifest = _evidence_manifest(
        core=prepared.core_document,
        source=prepared.source_report,
        projection=prepared.projection.document,
    )
    seed_runtime = create_local_app(
        gateway=_UnavailableAfterRestartSmartPerfettoGateway(),
        data_root=data_root,
        control_store=control,
    ).state.local_runtime
    store.save_state(
        user.team_id,
        analysis.analysis_id,
        seed_runtime._state_document(analysis),
    )
    provider = _CountingProjectionReportProvider()
    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    restarted = create_local_app(
        gateway=restarted_gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=data_root,
        control_store=control,
        source_code_analysis_enabled=True,
        poll_interval_seconds=0.001,
    )

    with _RawTestClient(restarted) as client:
        _authenticated_client(client, "user01", "established user password")
        for _ in range(100):
            response = client.get(
                f"/v1/teams/{user.team_id}/analyses/{analysis.analysis_id}"
            )
            if response.json()["report_available"]:
                break
            time.sleep(0.01)
        report = client.get(
            f"/v1/teams/{user.team_id}/analyses/{analysis.analysis_id}/report"
        )

    assert response.json()["source_code_analysis"]["context_state"] == "unavailable"
    assert response.json()["source_code_analysis"]["failure_code"] == (
        "source_agent_unavailable"
    )
    assert report.status_code == 200, report.text
    assert report.json()["source_code"]["context_state"] == "unavailable"
    assert provider.calls == 1
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0


def test_local_runtime_allows_configured_private_lan_web_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PERFPILOT_LOCAL_WEB_ORIGIN", "http://10.166.0.125:3000")
    app = create_local_app(
        data_root=tmp_path,
        public_origin="http://10.166.0.125:8000",
    )

    with TestClient(app) as client:
        response = client.options(
            "/local/v1/uploads/example",
            headers={
                "Origin": "http://10.166.0.125:3000",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "content-type,x-amz-checksum-sha256"
                ),
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://10.166.0.125:3000"


class _BlockingSmartPerfettoGateway(_FakeSmartPerfettoGateway):
    async def status(self, run: LocalEngineRun) -> str:
        assert run.session_id == "session-local-1"
        return "running"


class _UnavailableAfterRestartSmartPerfettoGateway:
    def __init__(self) -> None:
        self.submissions: list[tuple[bytes, str, str | None]] = []
        self.status_calls = 0
        self.fetch_calls = 0

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        self.submissions.append((trace_path.read_bytes(), profile, question))
        raise AssertionError("AI-only rerun must not submit SmartPerfetto work")

    async def status(self, run: LocalEngineRun) -> str:
        del run
        self.status_calls += 1
        raise AssertionError("AI-only rerun must not query a prior SmartPerfetto session")

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        del run
        self.fetch_calls += 1
        raise AssertionError("AI-only rerun must not fetch a prior SmartPerfetto session")

    async def cancel(self, run: LocalEngineRun) -> None:
        del run
        raise AssertionError("AI-only rerun must not cancel a prior SmartPerfetto session")

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class _FakeDevice:
    serial: str
    manufacturer: str
    model: str
    android_version: str
    api_level: int


@dataclass(frozen=True)
class _FakeDeviceStatus:
    state: str
    device: _FakeDevice | None


class _FakeDeviceProbe:
    def __init__(self, status: _FakeDeviceStatus) -> None:
        self.status = status
        self.calls = 0

    async def inspect(self) -> _FakeDeviceStatus:
        self.calls += 1
        return self.status


class _FakeLocalDeviceCaptureGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, Path]] = []

    async def capture(
        self,
        *,
        apk_path: Path,
        serial: str,
        workspace: Path,
    ) -> LocalDeviceCapture:
        self.calls.append((apk_path.read_bytes(), serial, workspace))
        startup_trace = workspace / "startup.perfetto-trace"
        scroll_trace = workspace / "scroll.perfetto-trace"
        memory_evidence = workspace / "memory-evidence.tar"
        startup_trace.write_bytes(b"captured-startup-trace")
        scroll_trace.write_bytes(b"captured-scroll-trace")
        memory_evidence.write_bytes(b"captured-memory-evidence")
        return LocalDeviceCapture(
            metadata=LocalApkMetadata(
                package_name="com.example.perfpilot",
                version_name="1.2.3",
                version_code=123,
                launch_activity="com.example.perfpilot/.MainActivity",
                min_sdk=26,
                target_sdk=35,
                supported_abis=("arm64-v8a",),
                has_native_libraries=True,
            ),
            startup_trace=startup_trace,
            scroll_trace=scroll_trace,
            memory_evidence=memory_evidence,
        )


class _FakeLocalApkInspector:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def inspect(self, apk_path: Path) -> LocalApkMetadata:
        self.calls.append(apk_path.read_bytes())
        return LocalApkMetadata(
            package_name="com.example.perfpilot",
            version_name="1.2.3",
            version_code=123,
            launch_activity=(
                "com.example.perfpilot/com.example.perfpilot.MainActivity"
            ),
            min_sdk=26,
            target_sdk=35,
            supported_abis=("arm64-v8a",),
            has_native_libraries=True,
        )


class _GatedLocalApkInspector(_FakeLocalApkInspector):
    def __init__(self, *, expected_entries: int = 1) -> None:
        super().__init__()
        self.first_entered = asyncio.Event()
        self.expected_entered = asyncio.Event()
        self.release = asyncio.Event()
        self._expected_entries = expected_entries

    async def inspect(self, apk_path: Path) -> LocalApkMetadata:
        self.calls.append(apk_path.read_bytes())
        self.first_entered.set()
        if len(self.calls) >= self._expected_entries:
            self.expected_entered.set()
        await self.release.wait()
        return LocalApkMetadata(
            package_name="com.example.perfpilot",
            version_name="1.2.3",
            version_code=123,
            launch_activity=(
                "com.example.perfpilot/com.example.perfpilot.MainActivity"
            ),
            min_sdk=26,
            target_sdk=35,
            supported_abis=("arm64-v8a",),
            has_native_libraries=True,
        )


class _InvalidLocalApkInspector:
    def __init__(self) -> None:
        self.calls = 0

    async def inspect(self, apk_path: Path) -> LocalApkMetadata:
        assert apk_path.is_file()
        self.calls += 1
        raise LocalDeviceCaptureError("apk_metadata_invalid")


class _EndToEndCaptureDevice:
    def __init__(
        self,
        *,
        block_capture: bool = False,
        fail_capture: bool = False,
        fail_scenario: str | None = None,
    ) -> None:
        self.installed: list[bytes] = []
        self.captured: list[str] = []
        self.cleaned = 0
        self.capture_started = asyncio.Event()
        self._block_capture = block_capture
        self._fail_capture = fail_capture
        self._fail_scenario = fail_scenario

    async def adb_version(self) -> str:
        return "Android Debug Bridge version 1.0.41"

    async def thermal_reading(self) -> ThermalReading:
        return ThermalReading(temperature_c=31.5, thermal_status=0)

    async def install(self, apk: Path) -> None:
        self.installed.append(apk.read_bytes())

    async def capture_trace(
        self, *, scenario_type: str, output: Path, **_kwargs: object
    ) -> None:
        self.captured.append(scenario_type)
        self.capture_started.set()
        if self._block_capture:
            await asyncio.Event().wait()
        if self._fail_capture or self._fail_scenario == scenario_type:
            raise CaptureError("trace_capture_failed")
        output.write_bytes(f"{scenario_type}-trace".encode())

    async def collect_memory_samples(self, **_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("memory capture must not run")

    async def cleanup(self) -> None:
        self.cleaned += 1

    async def uninstall(self, package_name: str) -> None:
        assert package_name == "com.example.perfpilot"


async def _run_real_remote_agent_capture(
    *,
    app,
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    credentials: dict[str, object],
    team_id: UUID,
    device_id: UUID,
    device_digest: str,
    block_capture: bool = False,
    fail_capture: bool = False,
    fail_scenario: str | None = None,
    cancel_analysis_id: str | None = None,
    browser_cookies: dict[str, str] | None = None,
    csrf_token: str | None = None,
    wait_for_report: bool = False,
    analysis_id: UUID | None = None,
    complete_strong_source: bool = False,
    cancel_during_analysis: bool = False,
) -> _EndToEndCaptureDevice:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    config = AgentConfig(
        server_url="https://testserver",
        ca_bundle=ca,
        workspace_root=tmp_path / "agent-work",
    )
    bound = AgentCredentials(
        schema_version="1.1",
        agent_id=UUID(str(credentials["agent_id"])),
        team_id=team_id,
        private_key_b64=base64.b64encode(
            private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode("ascii"),
        access_token=str(credentials["access_token"]),
        access_token_expires_at=datetime.fromisoformat(
            str(credentials["access_token_expires_at"])
        ),
        refresh_token=str(credentials["refresh_token"]),
        refresh_token_expires_at=datetime.fromisoformat(
            str(credentials["refresh_token_expires_at"])
        ),
        task_signing_key=TaskSigningKey.model_validate(
            credentials["task_signing_key"]
        ),
        heartbeat_interval_seconds=10,
    )
    state = AgentRuntimeState()
    state.replace_device_bindings(
        (
            DeviceBinding(
                client_ref=UUID("74000000-0000-4000-8000-000000000001"),
                device_id=device_id,
                device_digest=device_digest,
                serial="emulator-5554",
            ),
        )
    )
    observed_responses: list[tuple[str, int]] = []

    async def observe_response(response: httpx.Response) -> None:
        await response.aread()
        observed_responses.append((response.request.url.path, response.status_code))

    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        follow_redirects=False,
        event_hooks={"response": [observe_response]},
    )
    control = ControlClient(config, http_client=http_client, credentials=bound)
    device = _EndToEndCaptureDevice(
        block_capture=block_capture,
        fail_capture=fail_capture,
        fail_scenario=fail_scenario,
    )
    runner = CaptureTaskRunner(
        config=config,
        adb_binary=tmp_path / "agent-adb",
        control=control,
        state=state,
        redactor=None,
        device_factory=lambda **_kwargs: device,
        downloader_factory=lambda **kwargs: InputDownloader(
            control=control,
            http_client=http_client,
            **kwargs,
        ),
        uploader_factory=lambda **kwargs: MultipartUploader(
            control=control,
            http_client=http_client,
            **kwargs,
        ),
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    executor = TaskExecutor(
        control=control,
        runner=runner,
        state=state,
        control_poll_interval_seconds=0.01,
        renewal_interval_seconds=20,
    )
    loop = TaskLoop(
        control=control,
        executor=executor,
        state=state,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    try:
        delivery = await control.poll_task(wait_seconds=0)
        assert isinstance(delivery, TaskExecuteResponse), delivery
        TaskVerifier(
            public_key_b64=bound.task_signing_key.public_key_b64,
            kid=bound.task_signing_key.kid,
        ).verify(
            delivery.snapshot_jws,
            expected_agent_id=bound.agent_id,
            expected_team_id=bound.team_id,
            expected_lease_version=None,
            known_device_digests=state.known_device_digests(),
        )
        if cancel_analysis_id is None:
            assert await loop.poll_once() is True
        else:
            task = asyncio.create_task(loop.poll_once())
            await asyncio.wait_for(device.capture_started.wait(), timeout=2)
            assert browser_cookies is not None and csrf_token is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://testserver",
                cookies=browser_cookies,
            ) as browser:
                canceled = await browser.post(
                    f"/v1/teams/{team_id}/analyses/{cancel_analysis_id}/cancel",
                    headers={
                        "origin": "http://localhost:3000",
                        "x-csrf-token": csrf_token,
                    },
                )
            assert canceled.status_code == 202, canceled.text
            assert await task is True
            assert any(
                path.endswith("/cancel-ack") and status_code == 200
                for path, status_code in observed_responses
            )
        completion_responses = [
            item for item in observed_responses if item[0].endswith("/complete")
        ]
        if cancel_analysis_id is None:
            assert completion_responses and completion_responses[-1][1] == 200, (
                observed_responses
            )
            if complete_strong_source:
                source_delivery = None
                for _ in range(100):
                    candidate = await control.poll_task(wait_seconds=0)
                    if isinstance(candidate, SourceTaskExecuteResponse):
                        source_delivery = candidate
                        break
                    await asyncio.sleep(0.01)
                source_repository = app.state.source_task_service._repository
                capture_repository = app.state.agent_task_service._repository
                assert source_delivery is not None, {
                    "source_tasks": [
                        (str(item.analysis_id), item.state)
                        for item in source_repository.tasks.values()
                    ],
                    "capture_leases": [
                        (str(item.definition.analysis_id), item.state)
                        for item in capture_repository._leases.values()
                    ],
                    "analysis_stage": app.state.local_runtime.analyses[
                        (team_id, analysis_id)
                    ].stages,
                }
                snapshot = source_delivery.snapshot
                hints = snapshot["finding_hints"]
                assert isinstance(hints, list) and hints
                hint = hints[0]
                assert isinstance(hint, dict)
                content = "fun init() = loadNow()"
                await control.complete_source_task(
                    execution_id=UUID(str(snapshot["execution_id"])),
                    lease_version=int(snapshot["lease_version"]),
                    lease_token=source_delivery.lease_token,
                    completion={
                        "schema_version": "1.0",
                        "task_type": "source_context",
                        "execution_id": str(snapshot["execution_id"]),
                        "analysis_id": str(snapshot["analysis_id"]),
                        "workspace_id": str(snapshot["workspace_id"]),
                        "lease_version": int(snapshot["lease_version"]),
                        "state": "completed",
                        "result": {
                            "snapshot_id": "94000000-0000-4000-8000-000000000001",
                            "snapshot_hash": "b" * 64,
                            "git_head": "a" * 40,
                            "tracked_dirty_count": 0,
                            "fragments": [
                                {
                                    "source_ref_id": "97000000-0000-4000-8000-000000000001",
                                    "relative_path": "app/src/main/java/demo/Startup.kt",
                                    "language": "kotlin",
                                    "symbol": "demo.Startup.init",
                                    "start_line": 1,
                                    "end_line": 1,
                                    "content": content,
                                    "content_sha256": hashlib.sha256(
                                        content.encode()
                                    ).hexdigest(),
                                    "snapshot_hash": "b" * 64,
                                    "finding_ids": [hint["finding_id"]],
                                    "evidence_ids": hint["evidence_ids"],
                                    "rule_ids": [
                                        "android.startup.eager_initialization"
                                    ],
                                    "match_signals": ["trace_symbol"],
                                }
                            ],
                            "exclusions": [],
                            "truncated": False,
                        },
                    },
                )
            if wait_for_report:
                assert analysis_id is not None
                analysis = app.state.local_runtime.analyses[(team_id, analysis_id)]
                assert analysis.task is not None
                await asyncio.wait_for(asyncio.shield(analysis.task), timeout=2)
            elif cancel_during_analysis:
                assert analysis_id is not None
                gateway = app.state.local_runtime.gateway
                assert isinstance(gateway, _BlockingRemoteSmartPerfettoGateway)
                for _ in range(200):
                    if gateway.status_entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert gateway.status_entered.is_set()
                assert browser_cookies is not None and csrf_token is not None
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://testserver",
                    cookies=browser_cookies,
                ) as browser:
                    canceled = await browser.post(
                        f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel",
                        headers={
                            "origin": "http://localhost:3000",
                            "x-csrf-token": csrf_token,
                        },
                    )
                assert canceled.status_code == 202, canceled.text
        else:
            assert completion_responses == []
    finally:
        await control.aclose()
        await http_client.aclose()
    return device


class _FakeLocalMemoryAnalysisGateway:
    engine_commit_sha = "d5514972ced78c3faa7fc17589c1ea9231645056"

    def __init__(self, result: EngineResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def analyze(self, **kwargs: object) -> EngineResult:
        self.calls.append(kwargs)
        return self.result

    async def aclose(self) -> None:
        self.close_calls += 1


class _ProjectionReportProvider:
    provider_name = "test-provider"
    model = "test-model"
    prompt_version = "perfpilot-report-v3-test"
    prompt_sha256_b64 = base64.b64encode(hashlib.sha256(b"test-prompt").digest()).decode(
        "ascii"
    )

    async def complete(self, *, projection) -> SynthesisCandidate:
        projected = projection.document
        findings = []
        conclusions = []
        recommendations = []
        retest_plan = []
        key_metric_ids = []
        for scenario in projected["scenarios"]:
            for finding in scenario["findings"]:
                evidence_ids = list(finding["evidence_ids"])
                findings.append(
                    {
                        "finding_id": finding["finding_id"],
                        "evidence_ids": evidence_ids,
                        "user_impact": "该问题会延迟用户看到可交互界面的时间。",
                    }
                )
                if finding["status"] in {"confirmed", "suspected"} and evidence_ids:
                    conclusions.append(
                        {
                            "finding_id": finding["finding_id"],
                            "evidence_ids": evidence_ids,
                            "source_ref_ids": [],
                            "problem": "SmartPerfetto 发现该路径存在性能问题。",
                            "cause": "Trace 证据表明关键执行被阻塞。",
                            "source_root_cause": "当前没有足够源码证据定位具体实现。",
                            "recommendation": "缩短关键路径，并用相同场景复测。",
                        }
                    )
                    priority = ("p0", "p1", "p2")[
                        min(len(recommendations), 2)
                    ]
                    recommendations.append(
                        {
                            "priority": priority,
                            "title": "处理已确认的性能瓶颈",
                            "action": "修复该性能问题，并使用相同场景复测。",
                            "expected_effect": "降低该问题对用户体验的影响。",
                            "finding_ids": [finding["finding_id"]],
                            "evidence_ids": evidence_ids,
                        }
                    )
            metric_ids = [
                metric["metric_id"]
                for metric in scenario["metrics"]
                if metric["status"] == "available"
            ]
            if metric_ids:
                key_metric_ids.extend(metric_ids)
                retest_plan.append(
                    {
                        "mode": "verify_metric",
                        "scenario_type": scenario["scenario_type"],
                        "metric_ids": metric_ids,
                        "limitation_ids": [],
                        "steps": "使用相同设备和场景重新采集 Trace。",
                        "success_condition": "improve_from_baseline",
                        "failure_condition": "threshold_missed",
                    }
                )
        document = {
            "schema_version": "2.0",
            "verdict": "存在证据支持的应用性能瓶颈。",
            "executive_summary": "单次测试 AI 已完成证据复核。",
            "key_metric_ids": key_metric_ids[:3],
            "conclusions": conclusions,
            "top_findings": findings[:3],
            "recommendations": recommendations[:3],
            "source_fixes": [],
            "retest_plan": retest_plan[:3],
            "limitations": [
                {
                    "limitation_id": item["limitation_id"],
                    "summary": "当前证据存在已标记的覆盖限制。",
                }
                for item in projected["limitations"]
            ],
        }
        source_context = projected.get("source_context")
        if (
            isinstance(source_context, dict)
            and source_context.get("match_summary") == "strong"
            and source_context.get("fragments")
            and recommendations
        ):
            source_ref = source_context["fragments"][0]
            path = source_ref["relative_path"]
            matching_conclusion = next(
                item
                for item in conclusions
                if item["finding_id"] in source_ref["finding_ids"]
            )
            matching_conclusion["source_ref_ids"] = [source_ref["source_ref_id"]]
            matching_conclusion["source_root_cause"] = (
                "源码中的启动方法在主线程执行可延迟初始化。"
            )
            document["source_fixes"] = [
                {
                    "fix_id": "95000000-0000-4000-8000-000000000001",
                    "finding_id": source_ref["finding_ids"][0],
                    "evidence_ids": source_ref["evidence_ids"],
                    "recommendation_priority": recommendations[0]["priority"],
                    "source_ref_ids": [source_ref["source_ref_id"]],
                    "rule_id": source_ref["rule_ids"][0],
                    "match_grade": "strong",
                    "relative_path": path,
                    "symbol": source_ref["symbol"],
                    "diagnosis": "启动路径在主线程执行了可延迟初始化。",
                    "diff": (
                        f"diff --git a/{path} b/{path}\n"
                        f"--- a/{path}\n"
                        f"+++ b/{path}\n"
                        "@@ -1 +1 @@\n"
                        "-fun init() = loadNow()\n"
                        "+fun init() = loadLazily()\n"
                    ),
                    "validation_profile_id": None,
                    "retest_target": "重复冷启动并对比首帧耗时。",
                }
            ]
        return SynthesisCandidate(
            candidate_json=canonical_json_bytes(document),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=5,
        )

    async def aclose(self) -> None:
        return None


class _CountingProjectionReportProvider(_ProjectionReportProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        return await super().complete(projection=projection)


class _PersistedEvidenceBarrierProvider(_ProjectionReportProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return await super().complete(projection=projection)


def _test_synthesizer() -> LocalReportSynthesizer:
    return LocalReportSynthesizer(provider=_ProjectionReportProvider())


class _InvalidReportProvider:
    provider_name = "invalid-test-provider"
    model = "invalid-test-model"
    prompt_version = "perfpilot-report-v3-test"
    prompt_sha256_b64 = base64.b64encode(
        hashlib.sha256(b"invalid-test-prompt").digest()
    ).decode("ascii")

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        del projection
        self.calls += 1
        return SynthesisCandidate(
            candidate_json=b"{}",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
        )

    async def aclose(self) -> None:
        return None


class _RerunBarrierReportProvider(_ProjectionReportProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.rerun_calls = 0
        self.first_rerun_started = threading.Event()
        self.second_rerun_started = threading.Event()
        self.release_reruns = threading.Event()

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        if self.calls > 1:
            self.rerun_calls += 1
            if self.rerun_calls == 1:
                self.first_rerun_started.set()
            else:
                self.second_rerun_started.set()
            while not self.release_reruns.is_set():
                await asyncio.sleep(0.001)
        return await super().complete(projection=projection)


class _AggregateTokenUsageReportProvider(_ProjectionReportProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        if self.calls == 1:
            return SynthesisCandidate(
                candidate_json=b"{}",
                prompt_tokens=3,
                completion_tokens=5,
                latency_ms=7,
            )
        valid = await super().complete(projection=projection)
        return SynthesisCandidate(
            candidate_json=valid.candidate_json,
            prompt_tokens=11,
            completion_tokens=13,
            latency_ms=17,
        )


def _smartperfetto_result() -> EngineResult:
    fixture = Path(__file__).resolve().parents[1] / (
        "fixtures/canonical_results/smartperfetto-result-contract-1.0.0.json"
    )
    canonical = json.loads(fixture.read_text("utf-8"))
    payload = canonical["result"]["payload"]
    for private_key in ("actions", "workspaceId", "runId"):
        payload["report"].pop(private_key, None)
    payload["report"]["reportId"] = payload["reportId"]
    return EngineResult(
        contract="workspace-agent-v1",
        state="completed",
        payload=payload,
        original_report_html_bytes=(
            b"<!DOCTYPE html><html><body>SmartPerfetto</body></html>"
        ),
    )


def _smartperfetto_scenario_result(scenario_type: str) -> EngineResult:
    result = _smartperfetto_result()
    report = result.payload["report"]
    report["dataEnvelopes"] = [
        item for item in report["dataEnvelopes"] if item["scenario"] == scenario_type
    ]
    report["diagnostics"] = [
        item for item in report["diagnostics"] if item["scenario"] == scenario_type
    ]
    envelope_evidence = {
        item["id"]
        for envelope in report["dataEnvelopes"]
        for item in envelope["evidence"]
    }
    report["claimVerificationResult"] = [
        item
        for item in report["claimVerificationResult"]
        if set(item["evidenceIds"]).issubset(envelope_evidence)
    ]
    return result


def _live_smartperfetto_result() -> EngineResult:
    return EngineResult(
        contract="workspace-agent-v1",
        state="completed",
        payload={
            "reportId": "live-report-1",
            "report": {
                "reportId": "live-report-1",
                "summary": {
                    "conclusion": "冷启动为 301.84ms；JIT 编译和 Debug 模式是主要优化点。",
                    "confidence": 0.9,
                },
                "resultContract": {
                    "version": "1.0.0",
                    "dataEnvelopes": [
                        {
                            "meta": {
                                "type": "skill_result",
                                "source": "startup_analysis:get_startups",
                                "stepId": "get_startups",
                                "evidenceRefId": "data:skill:startup_analysis:get_startups",
                                "identityResolution": {
                                    "version": "identity_contract@1",
                                    "status": "verified",
                                    "target": {
                                        "packageName": "com.example.app",
                                        "processName": "com.example.app",
                                    },
                                    "processes": [{"upid": 9, "pid": 99}],
                                    "threads": [{"tid": 99, "role": "app_main"}],
                                },
                            },
                            "data": {
                                "columns": [
                                    "startup_id",
                                    "dur_ms",
                                    "ttid_ms",
                                    "start_ts",
                                    "end_ts",
                                ],
                                "rows": [[1, 301.84, 299.82, 1000, 302840000]],
                            },
                            "display": {
                                "title": "检测到的启动事件",
                                "columns": [
                                    {
                                        "name": "dur_ms",
                                        "label": "启动耗时",
                                        "unit": "ms",
                                    },
                                    {"name": "ttid_ms", "label": "TTID", "unit": "ms"},
                                ],
                            },
                        }
                    ],
                    "diagnostics": [
                        {
                            "id": "jit-active",
                            "severity": "critical",
                            "title": "JIT 编译活跃",
                            "description": "启动期间存在大量 JIT 编译。",
                            "confidence": 0.95,
                            "evidence": [
                                {
                                    "text": "JIT 编译 1904 次。\n\n**建议**：部署 Baseline Profile。"
                                }
                            ],
                        }
                    ],
                    "actions": [
                        {
                            "id": "fix-jit",
                            "label": "部署 Baseline Profile",
                            "priority": "high",
                            "sourceDiagnosticId": "jit-active",
                        }
                    ],
                },
                "identityResolutions": [],
            },
        },
    )


def _android_memory_result() -> EngineResult:
    return EngineResult(
        contract="android-memory-ai-context-1.2",
        state="completed",
        payload={
            "context_type": "android-memory-ai-context",
            "schema_version": "1.2",
            "generator": {"name": "android-memory-ai", "version": "1.2.0"},
            "request": {
                "intent": "quick-triage",
                "evaluated_intents": ["quick-triage"],
            },
            "evidence": {
                "coverage": {
                    "level": "strong",
                    "available": ["meminfo"],
                    "missing_required": [],
                    "missing_supporting": [],
                    "missing_any_of": [],
                    "inadequate": [],
                },
                "accounting_ledger": {
                    "schema_version": "1.0",
                    "status": "available",
                    "rows": [
                        {
                            "name": "TOTAL",
                            "meminfo": {
                                "pss_total_kb": 123456,
                                "private_dirty_kb": 45678,
                                "private_clean_kb": 987,
                                "swap_pss_kb": 321,
                                "rss_total_kb": 150000,
                            },
                        },
                        {
                            "name": "Native Heap",
                            "meminfo": {
                                "pss_total_kb": 32000,
                                "private_dirty_kb": 30000,
                                "private_clean_kb": 0,
                                "swap_pss_kb": 12,
                                "rss_total_kb": 34000,
                            },
                        },
                    ],
                },
            },
            "analysis_contract": {
                "support_level": "strong",
                "primary_intent_support_level": "strong",
                "privacy": {
                    "raw_contents_embedded": False,
                    "local_paths_included": False,
                },
            },
            "next_evidence": [],
            "limitations": [],
        },
    )


def _create_trace_analysis(
    client: TestClient,
    *,
    team_id: str,
    headers: dict[str, str],
    trace: bytes = b"background-local-trace",
    question: str = "首帧为什么慢？",
    package_name: str = "com.example",
) -> tuple[str, str]:
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")
    response = client.post(
        f"/v1/teams/{team_id}/analyses",
        headers=headers,
        json={
            "schema_version": "1.0",
            "analysis_mode": "trace_upload",
            "test_type": "cold_start",
            "package_name": package_name,
            "question": question,
            "inputs": [
                {
                    "kind": "trace",
                    "mime": "application/octet-stream",
                    "size": len(trace),
                    "sha256_b64": checksum,
                }
            ],
        },
    )
    assert response.status_code == 201
    assert response.json()["team_id"] == team_id
    return response.json()["analysis_id"], checksum


def _upload_and_finalize_trace(
    client: TestClient,
    *,
    team_id: str,
    analysis_id: str,
    headers: dict[str, str],
    checksum: str,
    trace: bytes = b"background-local-trace",
) -> None:
    slot_response = client.post(
        f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
        headers=headers,
        json={
            "artifact_kind": "trace",
            "mime": "application/octet-stream",
            "size": len(trace),
            "sha256_b64": checksum,
        },
    )
    assert slot_response.status_code == 201
    slot = slot_response.json()["upload"]
    put = urlsplit(slot["put_url"])
    assert (
        client.put(
        f"{put.path}?{put.query}",
        content=trace,
        headers=slot["required_headers"],
        ).status_code
        == 200
    )
    assert (
        client.post(
        f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
        headers=headers,
        json={
            "upload_id": slot["upload_id"],
            "sha256_b64": checksum,
            "size": len(trace),
        },
        ).status_code
        == 200
    )


def test_local_analysis_v13_persists_authoritative_runtime_status(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
    )
    trace = b"runtime-status-trace"
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.3",
                "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": "com.rivotek.mediacenter",
                "question": None,
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
            },
        )

        assert created.status_code == 201, created.text
        payload = created.json()
        analysis_id = payload["analysis_id"]
        assert payload["schema_version"] == "1.3"
        assert payload["runtime_status"] == {
            "current_stage": "input_validation",
            "stage_state": "running",
            "started_at": payload["created_at"],
            "updated_at": payload["created_at"],
            "last_progress_at": payload["created_at"],
            "attempt": 1,
            "max_attempts": 2,
            "generation": 1,
            "waiting_for": None,
            "progress_summary": "正在校验分析输入",
            "available_actions": ["cancel"],
        }

    persisted = LocalAnalysisStore(tmp_path).load_document(
        UUID(team_id),
        UUID(analysis_id),
        "state.json",
    )
    assert persisted is not None
    assert persisted["response_schema_version"] == "1.3"
    assert persisted["runtime_status"] == payload["runtime_status"]

    restarted = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
    )
    with TestClient(restarted) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")

    assert restored.status_code == 200
    assert restored.json()["schema_version"] == "1.3"
    assert restored.json()["runtime_status"] == payload["runtime_status"]


def test_local_supervisor_marks_idle_upstream_without_failing_analysis(
    tmp_path: Path,
) -> None:
    gateway = _BlockingSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=60,
    )
    trace = b"supervised-trace"
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.3",
                "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": "com.rivotek.mediacenter",
                "question": None,
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
            },
        ).json()
        analysis_id = UUID(created["analysis_id"])
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=str(analysis_id),
            headers=headers,
            checksum=checksum,
            trace=trace,
        )
        runtime = app.state.local_runtime
        assert client.portal is not None
        client.portal.call(asyncio.sleep, 0.01)

        async def make_idle() -> None:
            analysis = runtime.analyses[(UUID(team_id), analysis_id)]
            async with runtime.lock:
                analysis.runtime_status = validate_analysis_runtime_status(
                    {
                        **(analysis.runtime_status or {}),
                        "current_stage": "smartperfetto",
                        "stage_state": "running",
                        "started_at": (now - timedelta(hours=1)).isoformat(),
                        "updated_at": (now - timedelta(minutes=10)).isoformat(),
                        "last_progress_at": (now - timedelta(minutes=10)).isoformat(),
                        "waiting_for": "smartperfetto",
                        "progress_summary": "正在分析 Trace",
                    }
                )
            await runtime._persist(analysis)

        client.portal.call(make_idle)
        client.portal.call(runtime.supervise_activity_once, now)
        idle = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}").json()

        assert idle["state"] == "analyzing"
        assert idle["runtime_status"]["stage_state"] == "waiting_for_upstream"
        assert idle["runtime_status"]["waiting_for"] == "smartperfetto"

        async def record_heartbeat() -> None:
            analysis = runtime.analyses[(UUID(team_id), analysis_id)]
            async with runtime.lock:
                analysis.runtime_status = validate_analysis_runtime_status(
                    {
                        **(analysis.runtime_status or {}),
                        "updated_at": now.isoformat(),
                        "last_progress_at": now.isoformat(),
                    }
                )

        client.portal.call(record_heartbeat)
        client.portal.call(runtime.supervise_activity_once, now)
        active = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}").json()

        assert active["state"] == "analyzing"
        assert active["runtime_status"]["stage_state"] == "running"
        assert active["runtime_status"]["waiting_for"] is None


def test_successful_trace_submission_removes_only_current_team_previous_analysis(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    first_trace = b"first-account-trace"
    second_trace = b"second-account-trace"
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        first_id, first_checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
            trace=first_trace,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=first_id,
            headers=headers,
            checksum=first_checksum,
            trace=first_trace,
        )
        for _ in range(100):
            if client.get(
                f"/v1/teams/{team_id}/analyses/{first_id}/report"
            ).status_code == 200:
                break
            time.sleep(0.01)
        assert client.get(
            f"/v1/teams/{team_id}/analyses/{first_id}/report"
        ).status_code == 200

        other_team_id = UUID("82000000-0000-4000-8000-000000000099")
        other_analysis_id = UUID("91000000-0000-4000-8000-000000000099")
        extra_store = LocalAnalysisStore(tmp_path)
        extra_store.save_state(
            other_team_id,
            other_analysis_id,
            {
                "team_id": str(other_team_id),
                "analysis_id": str(other_analysis_id),
                "state": "completed",
            },
        )
        extra_store.close()

        second_id, second_checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
            trace=second_trace,
        )
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{second_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(second_trace),
                "sha256_b64": second_checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert client.put(
            f"{put.path}?{put.query}",
            content=second_trace,
            headers=slot["required_headers"],
        ).status_code == 200

        failed = client.post(
            f"/v1/teams/{team_id}/analyses/{second_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": base64.b64encode(b"x" * 32).decode("ascii"),
                "size": len(second_trace),
            },
        )
        assert failed.status_code == 409
        assert client.get(
            f"/v1/teams/{team_id}/analyses/{first_id}/report"
        ).status_code == 200

        succeeded = client.post(
            f"/v1/teams/{team_id}/analyses/{second_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": second_checksum,
                "size": len(second_trace),
            },
        )
        assert succeeded.status_code == 200
        for _ in range(100):
            if client.get(
                f"/v1/teams/{team_id}/analyses/{second_id}/report"
            ).status_code == 200:
                break
            time.sleep(0.01)
        assert client.get(
            f"/v1/teams/{team_id}/analyses/{second_id}/report"
        ).status_code == 200
        assert client.get(
            f"/v1/teams/{team_id}/analyses/{first_id}"
        ).status_code == 404

    reopened = LocalAnalysisStore(tmp_path)
    assert (UUID(team_id), UUID(first_id)) not in reopened.load_states()
    assert reopened.load_document(
        other_team_id, other_analysis_id, "state.json"
    ) == {
        "team_id": str(other_team_id),
        "analysis_id": str(other_analysis_id),
        "state": "completed",
    }
    reopened.close()


def _persist_created_trace_analysis(
    tmp_path: Path,
) -> tuple[str, UUID, dict[str, object]]:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, _ = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
    parsed_analysis_id = UUID(analysis_id)
    state = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), parsed_analysis_id)]
    return team_id, parsed_analysis_id, state


def _recovery_request() -> dict[str, object]:
    trace = b"recovered-trace"
    return {
        "schema_version": "1.0",
        "session_id": "session-local-1",
        "run_id": "run-local-1",
        "analysis_profile": "auto",
        "question": "给出最终优化方案",
        "trace_size": len(trace),
        "trace_sha256_b64": base64.b64encode(hashlib.sha256(trace).digest()).decode(
            "ascii"
        ),
    }


def test_local_app_restores_duplicate_upload_ids_without_cross_team_aliasing(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    other_team_id = UUID("82000000-0000-4000-8000-000000000099")
    upload_id = "93000000-0000-4000-8000-000000000001"
    state["inputs"][0]["upload_id"] = upload_id
    state["state"] = "uploading"
    store = LocalAnalysisStore(tmp_path)
    store.save_state(UUID(team_id), analysis_id, state)
    other_state = copy.deepcopy(state)
    other_state["team_id"] = str(other_team_id)
    store.save_state(other_team_id, analysis_id, other_state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with TestClient(app):
        uploads = app.state.local_runtime.uploads
        first_key = (UUID(team_id), analysis_id, upload_id)
        second_key = (other_team_id, analysis_id, upload_id)
        assert set(uploads) == {first_key, second_key}
        assert uploads[first_key].path != uploads[second_key].path
        assert uploads[first_key].token != uploads[second_key].token


def test_local_app_defaults_missing_persisted_ai_rounds_after_restart(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    state.pop("ai_rounds")
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")

    assert restored.status_code == 200
    assert restored.json()["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "pending", "attempts": 0}
    ]


def test_local_source_binding_disabled_persistence_and_old_json_compatibility(
    tmp_path: Path,
) -> None:
    binding = {
        "provider_kind": "agent_workspace",
        "agent_id": "91000000-0000-4000-8000-000000000001",
        "workspace_id": "92000000-0000-4000-8000-000000000001",
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
    }
    body = {
        "schema_version": "1.1",
        "analysis_mode": "trace_upload",
        "test_type": "cold_start",
        "package_name": "com.example",
        "question": None,
        "inputs": [
            {
                "kind": "trace",
                "mime": "application/octet-stream",
                "size": 4,
                "sha256_b64": base64.b64encode(hashlib.sha256(b"data").digest()).decode(),
            }
        ],
        "source_binding": binding,
    }
    disabled_app = create_local_app(data_root=tmp_path / "disabled")
    with TestClient(disabled_app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        disabled = client.post(
            f"/v1/teams/{team_id}/analyses",
            json=body,
            headers={"x-csrf-token": csrf},
        )
    assert disabled.status_code == 422
    assert disabled.json()["error"]["code"] == "source_code_analysis_disabled"

    enabled_app = create_local_app(
        data_root=tmp_path / "enabled",
        source_code_analysis_enabled=True,
    )
    with TestClient(enabled_app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        unavailable = client.post(
            f"/v1/teams/{team_id}/analyses",
            json=body,
            headers={"x-csrf-token": csrf},
        )
    assert unavailable.status_code == 404
    assert unavailable.json()["error"]["code"] == "resource_not_found"


def test_local_source_code_analysis_environment_enables_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED", "true")
    body = {
        "schema_version": "1.1",
        "analysis_mode": "trace_upload",
        "test_type": "cold_start",
        "package_name": "com.example",
        "question": None,
        "inputs": [
            {
                "kind": "trace",
                "mime": "application/octet-stream",
                "size": 4,
                "sha256_b64": base64.b64encode(hashlib.sha256(b"data").digest()).decode(),
            }
        ],
        "source_binding": {
            "provider_kind": "agent_workspace",
            "agent_id": "91000000-0000-4000-8000-000000000001",
            "workspace_id": "92000000-0000-4000-8000-000000000001",
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": None,
        },
    }
    app = create_local_app(data_root=tmp_path)

    with TestClient(app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        response = client.post(
            f"/v1/teams/{team_id}/analyses",
            json=body,
            headers={"x-csrf-token": csrf},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_local_source_code_analysis_environment_rejects_unknown_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED", "TRUE")

    with pytest.raises(
        ValueError,
        match="^PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED must be true or false$",
    ):
        create_local_app(data_root=tmp_path)


def test_local_app_rejects_present_null_persisted_ai_rounds_after_restart(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    state["ai_rounds"] = None
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


@pytest.mark.parametrize(
    "location",
    [
        "top_level",
        "input",
        "input_descriptor",
        "stages",
        "source_run",
        "source_binding",
        "source_code_analysis",
        "ai_round",
    ],
)
def test_local_app_rejects_unknown_persisted_state_fields_after_restart(
    tmp_path: Path,
    location: str,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    if location == "top_level":
        state["unexpected"] = True
    elif location == "input":
        state["inputs"][0]["unexpected"] = True
    elif location == "input_descriptor":
        state["inputs"][0]["descriptor"]["unexpected"] = True
    elif location == "stages":
        state["stages"]["unexpected"] = "pending"
    elif location == "source_run":
        state["source_run"] = {
            "session_id": "session-local-1",
            "run_id": "run-local-1",
            "unexpected": True,
        }
    elif location == "source_binding":
        state["source_binding"] = {
            "provider_kind": "agent_workspace",
            "agent_id": "91000000-0000-4000-8000-000000000001",
            "workspace_id": "92000000-0000-4000-8000-000000000001",
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": None,
            "unexpected": True,
        }
    elif location == "source_code_analysis":
        state["source_code_analysis"]["unexpected"] = True
    else:
        state["ai_rounds"][0]["unexpected"] = True
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


def test_local_app_rejects_present_null_evidence_format_after_restart(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    state["evidence_format_version"] = None
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("mutation", ["marker_only", "manifest_only", "malformed"])
def test_local_app_requires_a_strict_evidence_marker_manifest_pair_after_restart(
    tmp_path: Path,
    mutation: str,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    checksum = base64.b64encode(hashlib.sha256(b"manifest").digest()).decode(
        "ascii"
    )
    manifest = {
        "schema_version": "1.0",
        "normalized_core_sha256_b64": checksum,
        "smartperfetto_report_sha256_b64": checksum,
        "projection_sha256_b64": checksum,
    }
    if mutation != "manifest_only":
        state["evidence_format_version"] = "normalized-core-v1"
    if mutation != "marker_only":
        state["evidence_manifest"] = manifest
    if mutation == "malformed":
        manifest["unexpected"] = True
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


def test_local_app_reports_the_device_currently_connected_over_adb(
    tmp_path: Path,
) -> None:
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial="0123456789ABCDEF",
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        response = client.get("/v1/device")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "state": "connected",
        "device": {
            "serial": "0123456789ABCDEF",
            "manufacturer": "UNISOC",
            "model": "uis7870_2h10_car_c200_6",
            "name": "UNISOC uis7870_2h10_car_c200_6",
            "os": "Android 13",
            "api_level": 33,
        },
    }
    assert device_probe.calls == 1


def test_local_app_does_not_expose_host_adb_through_team_directory(
    tmp_path: Path,
) -> None:
    serial = "0123456789ABCDEF"
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial=serial,
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        first = client.get(f"/v1/teams/{team_id}/devices")
        second = client.get(f"/v1/teams/{team_id}/devices")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"schema_version": "1.1", "devices": []}
    assert second.json() == {"schema_version": "1.1", "devices": []}
    assert device_probe.calls == 0


def test_local_team_device_directory_is_empty_without_one_ready_device(
    tmp_path: Path,
) -> None:
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(state="disconnected", device=None)
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        response = client.get(f"/v1/teams/{team_id}/devices")

    assert response.status_code == 200
    assert response.json() == {"schema_version": "1.1", "devices": []}


def test_local_app_rejects_host_device_analysis_without_remote_capture(
    tmp_path: Path,
) -> None:
    serial = "0123456789ABCDEF"
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial=serial,
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    apk = b"local-device-apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
    )

    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        device_id = "72000000-0000-4000-8000-000000000001"
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "device",
                "device_id": device_id,
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        )

    assert created.status_code == 409
    assert created.json()["error"]["code"] == "remote_device_capture_unavailable"


@pytest.mark.parametrize(
    ("test_type", "launch_mode", "target", "expected_scenario"),
    [
        (
            "cold_start",
            "automatic",
            {
                "package_name": "com.rivotek.mediacenter",
                "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
            },
            "startup",
        ),
        ("hot_start", "manual", None, "startup"),
        (
            "scroll",
            "manual",
            {
                "package_name": "com.rivotek.mediacenter",
                "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
            },
            "scroll",
        ),
    ],
)
def test_local_script_capture_queues_without_apk_upload(
    tmp_path: Path,
    test_type: str,
    launch_mode: str,
    target: dict[str, str] | None,
    expected_scenario: str,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
    )
    private_key = Ed25519PrivateKey.generate()

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Capture Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "capture-mac",
                "os_version": "macOS 15",
            },
        ).json()
        agent_headers = {"Authorization": f"Bearer {credentials['access_token']}"}
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers=agent_headers,
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "capture-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [
                    {
                        "client_ref": "74000000-0000-4000-8000-000000000001",
                        "serial": "emulator-5554",
                        "manufacturer": "Rivotek",
                        "model": "Media Center",
                        "android_release": "13",
                        "api_level": 33,
                        "connection_type": "usb",
                        "adb_state": "device",
                        "battery_percent": 80,
                        "temperature_c": None,
                        "storage_available_bytes": 1024,
                        "property_error_code": None,
                        "launch_targets": [
                            {
                                "package_name": "com.rivotek.mediacenter",
                                "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
                            }
                        ],
                    }
                ],
                "workspaces": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        device_id = heartbeat.json()["devices"][0]["device_id"]
        created = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.2",
                "analysis_mode": "device",
                "device_id": device_id,
                "test_type": test_type,
                "launch_mode": launch_mode,
                "duration_seconds": 15,
                "target": target,
            },
        )

        assert created.status_code == 201, created.text
        document = created.json()
        assert document["schema_version"] == "1.2"
        assert "apk_upload" not in document
        assert document["capture_configuration"] == {
            "test_type": test_type,
            "launch_mode": launch_mode,
            "duration_seconds": 15,
            "target": target,
        }
        assert [item["scenario_type"] for item in document["scenarios"]] == [
            test_type
        ]
        client.cookies.clear()
        delivery = client.get(
            "/v1/agent/tasks/next?wait_seconds=0", headers=agent_headers
        )
        assert delivery.status_code == 200, delivery.text
        claims = verify_task_jws(
            delivery.json()["snapshot_jws"],
            credentials["task_signing_key"]["public_key_b64"],
            expected_kid=credentials["task_signing_key"]["kid"],
            expected_team_id=user.team_id,
        )
        assert claims["schema_version"] == "1.2"
        assert claims["input_artifacts"] == []
        assert claims["test_type"] == test_type
        assert claims["launch_mode"] == launch_mode
        assert [item["scenario_type"] for item in claims["scenarios"]] == [
            expected_scenario
        ]
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        running = client.get(
            f"/v1/teams/{user.team_id}/analyses/{document['analysis_id']}",
            headers=browser_headers,
        )
        assert running.status_code == 200, running.text
        assert running.json()["state"] == "scheduled"


@pytest.mark.asyncio
async def test_script_capture_cancel_is_immediate_after_agent_lease(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
    )
    runtime = app.state.local_runtime
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    agent_id = UUID("71000000-0000-4000-8000-000000000001")
    analysis = _LocalAnalysis(
        team_id=team_id,
        analysis_id=analysis_id,
        profile="startup",
        question=None,
        inputs={},
        analysis_mode="device",
        device_id=UUID("72000000-0000-4000-8000-000000000001"),
        device_agent_id=agent_id,
        device_digest="device-digest",
        capture_configuration={
            "test_type": "cold_start",
            "launch_mode": "automatic",
            "duration_seconds": 15,
            "package_name": "com.rivotek.mediacenter",
            "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        },
        remote_publication="published",
        state="queued",
    )
    runtime.analyses[(team_id, analysis_id)] = analysis
    runtime.agent_tasks._repository._capture_lease_projection = None
    await runtime.agent_tasks.enqueue(runtime._script_device_definition(analysis))
    scheduled = await runtime.agent_tasks.schedule(
        analysis_id=analysis_id,
        agent_id=agent_id,
    )
    assert scheduled is not None
    original_request_cancel = runtime.agent_tasks.request_cancel
    cancel_calls = 0

    async def fail_once_request_cancel(**kwargs: object):
        nonlocal cancel_calls
        cancel_calls += 1
        if cancel_calls == 1:
            raise RuntimeError("transient cancellation dispatch failure")
        return await original_request_cancel(**kwargs)

    runtime.agent_tasks.request_cancel = fail_once_request_cancel

    canceled, accepted = await runtime.cancel(analysis)
    renewed = None
    for _ in range(100):
        renewed = await runtime.agent_tasks.renew(
            agent_id=agent_id,
            execution_id=scheduled.execution_id,
            lease_version=scheduled.lease_version,
        )
        if isinstance(renewed, AgentTaskCancellation):
            break
        await asyncio.sleep(0.01)
    await runtime.close()
    app.state.agent_upload_service.close()
    app.state.local_agent_store.close()

    assert accepted is True
    assert canceled.state == "canceled"
    assert canceled.cancel_requested_at is not None
    assert cancel_calls == 2
    assert isinstance(renewed, AgentTaskCancellation)


def test_local_remote_capture_finalizes_apk_and_publishes_agent_task(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user(
        "user01", "initial user password", False
    ).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    inspector = _FakeLocalApkInspector()
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(state="disconnected", device=None)
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        apk_inspector=inspector,
        device_probe=device_probe,
    )
    private_key = Ed25519PrivateKey.generate()
    apk = b"valid remote apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Capture Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "capture-mac",
                "os_version": "macOS 15",
            },
        ).json()
        agent_headers = {
            "Authorization": f"Bearer {credentials['access_token']}"
        }
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers=agent_headers,
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "capture-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [{
                    "client_ref": "74000000-0000-4000-8000-000000000001",
                    "serial": "emulator-5554",
                    "manufacturer": "Google",
                    "model": "Pixel",
                    "android_release": "16",
                    "api_level": 36,
                    "connection_type": "usb",
                    "adb_state": "device",
                    "battery_percent": 80,
                    "temperature_c": None,
                    "storage_available_bytes": 1024,
                    "property_error_code": None,
                }],
                "workspaces": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        device_id = heartbeat.json()["devices"][0]["device_id"]

        created = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "device",
                "device_id": device_id,
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        )
        assert created.status_code == 201, created.text
        created_document = created.json()
        assert created_document["schema_version"] == "1.1"
        assert created_document["source_code_analysis"] == {
            "requested": False,
            "provider_kind": None,
            "agent_id": None,
            "workspace_id": None,
            "snapshot_policy": None,
            "validation_profile_id": None,
            "context_state": "not_requested",
            "match_summary": "none",
            "verification_state": "not_requested",
            "failure_code": None,
        }
        assert [item["state"] for item in created_document["scenarios"]] == [
            "awaiting_input",
            "awaiting_input",
            "not_requested",
        ]
        analysis_id = created_document["analysis_id"]
        slot = created_document["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
            f"{put.path}?{put.query}",
            content=apk,
            headers=slot["required_headers"],
            ).status_code
            == 200
        )
        finalized = client.post(
            f"/v1/teams/{user.team_id}/analyses/{analysis_id}/finalize-upload",
            headers=browser_headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(apk),
            },
        )
        assert finalized.status_code == 200, finalized.text
        replayed = client.post(
            f"/v1/teams/{user.team_id}/analyses/{analysis_id}/finalize-upload",
            headers=browser_headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(apk),
            },
        )
        assert replayed.status_code == 200, replayed.text
        assert (
            replayed.json()["upload"]["artifact_id"]
            == (finalized.json()["upload"]["artifact_id"])
        )
        analysis = client.get(
            f"/v1/teams/{user.team_id}/analyses/{analysis_id}"
        ).json()
        client.cookies.clear()
        delivery = client.get(
            "/v1/agent/tasks/next?wait_seconds=0", headers=agent_headers
        )
        assert delivery.status_code == 200, delivery.text
        assert delivery.json()["action"] == "execute"
        claims = verify_task_jws(
            delivery.json()["snapshot_jws"],
            credentials["task_signing_key"]["public_key_b64"],
            expected_kid=credentials["task_signing_key"]["kid"],
            expected_team_id=user.team_id,
        )
        startup_trace = b"startup trace"
        startup_checksum = base64.b64encode(
            hashlib.sha256(startup_trace).digest()
        ).decode("ascii")
        execution_id = claims["execution_id"]
        upload = client.post(
            f"/v1/agent/tasks/{execution_id}/uploads",
            headers=agent_headers,
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "artifact_kind": "startup_trace",
                "mime": "application/x-perfetto-trace",
                "size": len(startup_trace),
                "sha256_b64": startup_checksum,
            },
        ).json()
        part = client.post(
            f"/v1/agent/tasks/{execution_id}/uploads/{upload['upload_id']}/parts",
            headers=agent_headers,
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "part_number": 1,
            },
        ).json()
        uploaded = client.put(urlsplit(part["put_url"]).path, content=startup_trace)
        completed_upload = client.post(
            f"/v1/agent/tasks/{execution_id}/uploads/{upload['upload_id']}/complete",
            headers=agent_headers,
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "parts": [{"part_number": 1, "etag": uploaded.headers["etag"]}],
            },
        ).json()
        started_at = datetime.now().astimezone()
        completed_at = started_at + timedelta(seconds=2)
        completion = client.post(
            f"/v1/agent/tasks/{execution_id}/complete",
            headers=agent_headers,
            json={
                "schema_version": "1.0",
                "execution_id": execution_id,
                "lease_version": 1,
                "state": "completed",
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "agent_version": "1.2.3",
                "adb_version": "Android Debug Bridge version 1.0.41",
                "artifacts": [{
                    "artifact_id": completed_upload["artifact_id"],
                    "kind": "startup_trace",
                    "mime": "application/x-perfetto-trace",
                    "size": len(startup_trace),
                    "sha256_b64": startup_checksum,
                }],
                "scenarios": [
                    {
                        "scenario_type": "startup",
                        "state": "completed",
                        "started_at": started_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                        "temperature_start_c": None,
                        "temperature_end_c": None,
                        "artifact_ids": [completed_upload["artifact_id"]],
                        "diagnostic_code": None,
                    },
                    {
                        "scenario_type": "scroll",
                        "state": "failed",
                        "started_at": started_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                        "temperature_start_c": None,
                        "temperature_end_c": None,
                        "artifact_ids": [],
                        "diagnostic_code": "scroll_capture_failed",
                    },
                ],
                "diagnostic_code": None,
            },
        )
        assert completion.status_code == 200, completion.text
        projected = _authenticated_client(
            client, "user01", "established user password"
        )
        projected_analysis = client.get(
            f"/v1/teams/{user.team_id}/analyses/{analysis_id}",
            headers=projected,
        ).json()

    assert inspector.calls == [apk]
    assert device_probe.calls == 0
    assert analysis["state"] == "queued"
    assert analysis["application_metadata"]["package_name"] == (
        "com.example.perfpilot"
    )
    assert claims["analysis_id"] == analysis_id
    assert claims["agent_id"] == credentials["agent_id"]
    assert claims["device_digest"] == heartbeat.json()["devices"][0]["device_digest"]
    assert [scenario["scenario_type"] for scenario in claims["scenarios"]] == [
        "startup",
        "scroll",
    ]
    assert claims["allowed_uploads"] == [
        "startup_trace",
        "scroll_trace",
        "agent_log",
    ]
    assert projected_analysis["state"] == "analyzing"


def test_local_remote_capture_rejects_invalid_apk_without_agent_task(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user(
        "user01", "initial user password", False
    ).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    inspector = _InvalidLocalApkInspector()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        apk_inspector=inspector,
    )
    private_key = Ed25519PrivateKey.generate()
    apk = b"invalid remote apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Capture Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "capture-mac",
                "os_version": "macOS 15",
            },
        ).json()
        agent_headers = {
            "Authorization": f"Bearer {credentials['access_token']}"
        }
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers=agent_headers,
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "capture-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [{
                    "client_ref": "74000000-0000-4000-8000-000000000001",
                    "serial": "emulator-5554",
                    "manufacturer": "Google",
                    "model": "Pixel",
                    "android_release": "16",
                    "api_level": 36,
                    "connection_type": "usb",
                    "adb_state": "device",
                    "battery_percent": 80,
                    "temperature_c": None,
                    "storage_available_bytes": 1024,
                    "property_error_code": None,
                }],
                "workspaces": [],
            },
        ).json()
        created = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.1",
                "analysis_mode": "device",
                "device_id": heartbeat["devices"][0]["device_id"],
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        ).json()
        slot = created["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
            f"{put.path}?{put.query}",
            content=apk,
            headers=slot["required_headers"],
            ).status_code
            == 200
        )
        finalized = client.post(
            (
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/finalize-upload"
            ),
            headers=browser_headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(apk),
            },
        )
        analysis = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
        ).json()
        client.cookies.clear()
        delivery = client.get(
            "/v1/agent/tasks/next?wait_seconds=0", headers=agent_headers
        )

    assert finalized.status_code == 409
    assert finalized.json()["error"]["code"] == "apk_metadata_invalid"
    assert inspector.calls == 1
    assert analysis["state"] == "failed"
    assert analysis["failure"]["code"] == "apk_metadata_invalid"
    assert delivery.json()["action"] == "wait"


async def _prepare_remote_finalize(
    runtime,
    *,
    team_id: UUID,
    analysis_id: UUID,
    upload_id: str,
    apk: bytes,
) -> tuple[_LocalAnalysis, _LocalUpload, _FinalizeUploadRequest]:
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")
    descriptor = _InputDescriptor(
        kind="apk",
        mime="application/vnd.android.package-archive",
        size=len(apk),
        sha256_b64=checksum,
    )
    target = _LocalInput(descriptor, upload_id=upload_id)
    analysis = _LocalAnalysis(
        team_id=team_id,
        analysis_id=analysis_id,
        profile="startup",
        question=None,
        inputs={"apk": target},
        analysis_mode="device",
        device_id=UUID("72000000-0000-4000-8000-000000000001"),
        device_agent_id=UUID("71000000-0000-4000-8000-000000000001"),
        device_digest="a" * 64,
        state="uploading",
    )
    path = runtime.store.upload_path(team_id, analysis_id, upload_id)
    path.write_bytes(apk)
    path.chmod(0o600)
    upload = _LocalUpload(
        upload_id=upload_id,
        team_id=team_id,
        analysis_id=analysis_id,
        kind="apk",
        mime=descriptor.mime,
        size=descriptor.size,
        sha256_b64=checksum,
        token=f"private-finalize-token-{analysis_id}",
        path=path,
        bytes_ready=True,
    )
    runtime.analyses[(team_id, analysis_id)] = analysis
    runtime._register_upload(upload)
    await runtime._persist(analysis)
    request = _FinalizeUploadRequest(
        upload_id=upload_id,
        sha256_b64=checksum,
        size=len(apk),
    )
    return analysis, upload, request


@pytest.mark.asyncio
async def test_local_remote_finalize_serializes_exact_concurrent_requests(
    tmp_path: Path,
) -> None:
    inspector = _GatedLocalApkInspector(expected_entries=2)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=inspector,
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    apk = b"concurrent remote apk"
    analysis, upload, request = await _prepare_remote_finalize(
        runtime,
        team_id=team_id,
        analysis_id=analysis_id,
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=apk,
    )
    first = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    await inspector.first_entered.wait()
    second = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    try:
        await asyncio.sleep(0.05)
        assert inspector.calls == [apk]
    finally:
        inspector.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        await runtime.close()
        app.state.agent_upload_service.close()
        app.state.local_agent_store.close()

    assert results == [upload, upload]
    repository = app.state.agent_task_service._repository
    assert list(repository._definitions) == [analysis_id]


@pytest.mark.asyncio
async def test_local_remote_finalize_waits_for_durable_intent_before_publication(
    tmp_path: Path,
) -> None:
    inspector = _FakeLocalApkInspector()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=inspector,
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    analysis, upload, request = await _prepare_remote_finalize(
        runtime,
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=analysis_id,
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=b"durable intent remote apk",
    )
    original_persist = runtime._persist
    intent_entered = asyncio.Event()
    release_intent = asyncio.Event()
    failed = False

    async def fail_first_intent(current: _LocalAnalysis) -> None:
        nonlocal failed
        if not failed and current.remote_publication == "publishing":
            failed = True
            intent_entered.set()
            await release_intent.wait()
            raise LocalAnalysisStoreError("injected intent failure")
        await original_persist(current)

    runtime._persist = fail_first_intent
    first = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    await intent_entered.wait()
    second = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    try:
        await asyncio.sleep(0.05)
        assert second.done() is False
        assert app.state.agent_upload_service._inputs == {}
        assert app.state.agent_task_service._repository._definitions == {}
    finally:
        release_intent.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        await runtime.close()
        app.state.agent_upload_service.close()
        app.state.local_agent_store.close()

    assert isinstance(results[0], LocalAnalysisStoreError)
    assert results[1] is upload
    assert inspector.calls == [b"durable intent remote apk"] * 2
    assert list(app.state.agent_task_service._repository._definitions) == [analysis_id]


@pytest.mark.asyncio
async def test_local_remote_finalize_locks_are_scoped_per_analysis(
    tmp_path: Path,
) -> None:
    inspector = _GatedLocalApkInspector(expected_entries=2)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=inspector,
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    first_analysis, _, first_request = await _prepare_remote_finalize(
        runtime,
        team_id=team_id,
        analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=b"first independent apk",
    )
    second_analysis, _, second_request = await _prepare_remote_finalize(
        runtime,
        team_id=team_id,
        analysis_id=UUID("30000000-0000-4000-8000-000000000002"),
        upload_id="51000000-0000-4000-8000-000000000002",
        apk=b"second independent apk",
    )
    first = asyncio.create_task(
        runtime._finalize_remote_device(first_analysis, first_request)
    )
    await inspector.first_entered.wait()
    second = asyncio.create_task(
        runtime._finalize_remote_device(second_analysis, second_request)
    )
    try:
        await asyncio.wait_for(inspector.expected_entered.wait(), timeout=1)
        assert inspector.calls == [b"first independent apk", b"second independent apk"]
    finally:
        inspector.release.set()
        await asyncio.gather(first, second)
        await runtime.close()
        app.state.agent_upload_service.close()
        app.state.local_agent_store.close()


@pytest.mark.asyncio
async def test_cancelled_remote_finalize_releases_analysis_lock_for_retry(
    tmp_path: Path,
) -> None:
    inspector = _GatedLocalApkInspector(expected_entries=2)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=inspector,
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    analysis, upload, request = await _prepare_remote_finalize(
        runtime,
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=analysis_id,
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=b"canceled finalize apk",
    )
    first = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    await inspector.first_entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    retry = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    try:
        await asyncio.wait_for(inspector.expected_entered.wait(), timeout=1)
    finally:
        inspector.release.set()
        result = await retry
        await runtime.close()
        app.state.agent_upload_service.close()
        app.state.local_agent_store.close()

    assert result is upload
    assert analysis.remote_publication == "published"
    assert list(app.state.agent_task_service._repository._definitions) == [analysis_id]


@pytest.mark.asyncio
async def test_analysis_cancel_wins_race_with_remote_apk_inspection(
    tmp_path: Path,
) -> None:
    inspector = _GatedLocalApkInspector()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=inspector,
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    analysis, _, request = await _prepare_remote_finalize(
        runtime,
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=analysis_id,
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=b"cancel race apk",
    )
    finalize = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    await inspector.first_entered.wait()
    cancellation = asyncio.create_task(runtime.cancel(analysis))
    for _ in range(100):
        if analysis.cancel_requested_at is not None:
            break
        await asyncio.sleep(0.001)
    assert analysis.cancel_requested_at is not None
    canceled, accepted = await asyncio.wait_for(
        asyncio.shield(cancellation),
        timeout=0.2,
    )
    assert inspector.release.is_set() is False
    inspector.release.set()
    result = await asyncio.gather(finalize, return_exceptions=True)
    await runtime.close()
    app.state.agent_upload_service.close()
    app.state.local_agent_store.close()

    assert accepted is True
    assert canceled.state == "canceled"
    assert canceled.cancel_requested_at is not None
    assert isinstance(result[0], HTTPException)
    assert result[0].status_code == 409
    assert app.state.agent_upload_service._inputs == {}
    assert app.state.agent_task_service._repository._definitions == {}


@pytest.mark.asyncio
async def test_analysis_cancel_after_enqueue_wins_before_published_persist(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=_FakeLocalApkInspector(),
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    analysis, _, request = await _prepare_remote_finalize(
        runtime,
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        analysis_id=analysis_id,
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=b"cancel after enqueue apk",
    )
    original_persist = runtime._persist
    published_entered = asyncio.Event()
    release_published = asyncio.Event()

    async def gate_published(current: _LocalAnalysis) -> None:
        if current.remote_publication == "published":
            published_entered.set()
            await release_published.wait()
        await original_persist(current)

    runtime._persist = gate_published
    finalize = asyncio.create_task(runtime._finalize_remote_device(analysis, request))
    await published_entered.wait()
    cancellation = asyncio.create_task(runtime.cancel(analysis))
    for _ in range(100):
        if analysis.cancel_requested_at is not None:
            break
        await asyncio.sleep(0.001)
    assert analysis.cancel_requested_at is not None
    release_published.set()
    finalize_result = await asyncio.gather(finalize, return_exceptions=True)
    canceled, accepted = await cancellation
    await runtime.close()
    app.state.agent_upload_service.close()
    app.state.local_agent_store.close()

    assert accepted is True
    assert canceled.state == "canceled"
    assert isinstance(finalize_result[0], HTTPException)
    assert finalize_result[0].status_code == 409
    assert app.state.agent_upload_service._inputs == {}
    assert app.state.agent_task_service._repository._definitions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("observer", ["completion", "cancellation"])
async def test_remote_terminal_observer_replay_persists_after_first_failure(
    tmp_path: Path,
    observer: str,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        apk_inspector=_FakeLocalApkInspector(),
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
    )
    runtime = app.state.local_runtime
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    analysis, _, _ = await _prepare_remote_finalize(
        runtime,
        team_id=team_id,
        analysis_id=analysis_id,
        upload_id="51000000-0000-4000-8000-000000000001",
        apk=b"observer replay apk",
    )
    access = AgentExecutionAccess(
        team_id=team_id,
        analysis_id=analysis_id,
        agent_id=analysis.device_agent_id,
        execution_id=UUID("73000000-0000-4000-8000-000000000001"),
        lease_version=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        scenario_types=("startup", "scroll"),
    )
    now = datetime.now(UTC)
    original_persist = runtime._persist
    failed = False

    async def fail_once(current: _LocalAnalysis) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise LocalAnalysisStoreError("injected observer persistence failure")
        await original_persist(current)

    runtime._persist = fail_once
    if observer == "completion":
        manifest = ValidatedAgentExecutionManifest(
            execution_id=access.execution_id,
            lease_version=1,
            state="failed",
            started_at=now,
            completed_at=now + timedelta(seconds=1),
            agent_version="1.2.3",
            adb_version="Android Debug Bridge version 1.0.41",
            artifacts=(),
            scenarios=(
                AgentExecutionScenario(
                    scenario_type="startup",
                    state="failed",
                    started_at=now,
                    completed_at=now + timedelta(seconds=1),
                    temperature_start_c=None,
                    temperature_end_c=None,
                    artifact_ids=(),
                    diagnostic_code="startup_capture_failed",
                ),
                AgentExecutionScenario(
                    scenario_type="scroll",
                    state="failed",
                    started_at=now,
                    completed_at=now + timedelta(seconds=1),
                    temperature_start_c=None,
                    temperature_end_c=None,
                    artifact_ids=(),
                    diagnostic_code="scroll_capture_failed",
                ),
            ),
            diagnostic_code="capture_failed",
            document_hash="a" * 64,
        )
        with pytest.raises(LocalAnalysisStoreError):
            await runtime.observe_agent_completion(access, manifest, now)
        await runtime.observe_agent_completion(access, manifest, now)
        expected_state = "failed"
    else:
        with pytest.raises(LocalAnalysisStoreError):
            await runtime.observe_agent_cancellation(access, now)
        await runtime.observe_agent_cancellation(access, now)
        expected_state = "canceled"
    persisted = runtime.store.load_states()[(team_id, analysis_id)]
    await runtime.close()
    app.state.agent_upload_service.close()
    app.state.local_agent_store.close()

    assert analysis.state == expected_state
    assert persisted["state"] == expected_state


@pytest.mark.parametrize(
    "outcome",
    [
        "completed",
        "source_strong",
        "smartperfetto_partial",
        "smartperfetto_all_failed",
        "analyzing_canceled",
        "accepted_restart_resumed",
        "failed",
        "canceled",
        "enqueue_failed",
        "intent_persist_failed",
        "published_persist_failed",
        "restart_reconciled",
        "published_restart_reconciled",
    ],
)
def test_local_remote_capture_report_runs_real_agent_lifecycle(
    tmp_path: Path,
    outcome: str,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user(
        "user01", "initial user password", False
    ).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    host_probe = _FakeDeviceProbe(_FakeDeviceStatus(state="disconnected", device=None))
    inspector = _FakeLocalApkInspector()
    smartperfetto_result = _smartperfetto_result()
    if outcome == "source_strong":
        smartperfetto_result.payload["report"]["dataEnvelopes"][0]["evidence"][0][
            "fields"
        ]["mapped_symbol"] = "demo.Startup.init"
    scenario_originals = {
        scenario_type: (
            b"<!DOCTYPE html><html><body><h1>SmartPerfetto "
            + scenario_type.encode("ascii")
            + b"</h1>"
            + (b"x" * 1_100_000 if outcome == "completed" else b"")
            + b"</body></html>"
        )
        for scenario_type in ("startup", "scroll")
    }
    smartperfetto = (
        _ScenarioFailingSmartPerfettoGateway(
            smartperfetto_result,
            failed_profile=("scroll" if outcome == "smartperfetto_partial" else "all"),
    )
        if outcome in {"smartperfetto_partial", "smartperfetto_all_failed"}
        else None
    )
    if smartperfetto is None:
        smartperfetto = (
            _ScenarioOriginalSmartPerfettoGateway(
                smartperfetto_result,
                original_report_html_bytes=scenario_originals,
            )
            if outcome == "completed"
            else _BlockingRemoteSmartPerfettoGateway(smartperfetto_result)
            if outcome in {"analyzing_canceled", "accepted_restart_resumed"}
            else _FakeSmartPerfettoGateway(smartperfetto_result)
        )
    provider = _CountingProjectionReportProvider()
    app = create_local_app(
        gateway=smartperfetto,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        apk_inspector=inspector,
        device_probe=host_probe,
        public_origin="https://testserver",
        source_code_analysis_enabled=outcome == "source_strong",
    )
    private_key = Ed25519PrivateKey.generate()
    apk = b"real agent remote apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Capture Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "capture-mac",
                "os_version": "macOS 15",
            },
        ).json()
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "capture-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [{
                    "client_ref": "74000000-0000-4000-8000-000000000001",
                    "serial": "emulator-5554",
                    "manufacturer": "Google",
                    "model": "Pixel",
                    "android_release": "16",
                    "api_level": 36,
                    "connection_type": "usb",
                    "adb_state": "device",
                    "battery_percent": 80,
                    "temperature_c": None,
                    "storage_available_bytes": 1024,
                    "property_error_code": None,
                    }
                ],
                "workspaces": (
                    [
                        {
                            "workspace_id": "92000000-0000-4000-8000-000000000001",
                            "name": "RivotekMedia",
                            "state": "ready",
                            "git_branch": "main",
                            "git_head": "a" * 40,
                            "tracked_dirty_count": 0,
                            "snapshot_policy": "tracked_worktree",
                            "validation_profiles": [
                                {
                                    "profile_id": "96000000-0000-4000-8000-000000000001",
                                    "name": "Unit tests",
                                }
                            ],
                        }
                    ]
                    if outcome == "source_strong"
                    else []
                ),
            },
        ).json()
        device = heartbeat["devices"][0]
        created_response = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.1",
                "analysis_mode": "device",
                "device_id": device["device_id"],
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
                **(
                    {
                        "source_binding": {
                            "provider_kind": "agent_workspace",
                            "agent_id": credentials["agent_id"],
                            "workspace_id": "92000000-0000-4000-8000-000000000001",
                            "snapshot_policy": "tracked_worktree",
                            "validation_profile_id": "96000000-0000-4000-8000-000000000001",
                        }
                    }
                    if outcome == "source_strong"
                    else {}
                ),
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        slot = created["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
            f"{put.path}?{put.query}",
            content=apk,
            headers=slot["required_headers"],
            ).status_code
            == 200
        )
        original_enqueue = app.state.local_runtime.agent_tasks.enqueue
        original_persist = app.state.local_runtime._persist
        if outcome in {"enqueue_failed", "restart_reconciled"}:

            async def fail_enqueue(_definition: object) -> bool:
                raise RuntimeError("injected enqueue failure")

            app.state.local_runtime.agent_tasks.enqueue = fail_enqueue
        elif outcome in {"intent_persist_failed", "published_persist_failed"}:
            failure_phase = (
                "publishing" if outcome == "intent_persist_failed" else "published"
            )
            failed = False

            async def fail_selected_persist(analysis) -> None:
                nonlocal failed
                if not failed and analysis.remote_publication == failure_phase:
                    failed = True
                    raise LocalAnalysisStoreError("injected publication persistence failure")
                await original_persist(analysis)

            app.state.local_runtime._persist = fail_selected_persist

        def finalize_request():
            return client.post(
                (
                    f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/finalize-upload"
                ),
                headers=browser_headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(apk),
                },
            )

        if outcome in {
            "enqueue_failed",
            "intent_persist_failed",
            "published_persist_failed",
            "restart_reconciled",
        }:
            error_type = (
                RuntimeError
                if outcome in {"enqueue_failed", "restart_reconciled"}
                else LocalAnalysisStoreError
            )
            with pytest.raises(error_type):
                finalize_request()
            if outcome == "intent_persist_failed":
                failed_analysis = app.state.local_runtime.analyses[
                    (user.team_id, UUID(created["analysis_id"]))
                ]
                assert failed_analysis.remote_publication == "not_requested"
                assert failed_analysis.inputs["apk"].finalized is False
                assert failed_analysis.application_metadata is None
            projected = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            ).json()
            if outcome != "published_persist_failed":
                client.cookies.clear()
                delivery = client.get(
                    "/v1/agent/tasks/next?wait_seconds=0",
                    headers={"Authorization": f"Bearer {credentials['access_token']}"},
                )
                assert delivery.json()["action"] == "wait"
                assert app.state.agent_upload_service._inputs == {}
                browser_headers = _authenticated_client(
                    client, "user01", "established user password"
                )
            if outcome != "restart_reconciled":
                app.state.local_runtime.agent_tasks.enqueue = original_enqueue
                app.state.local_runtime._persist = original_persist
                assert finalize_request().status_code == 200
                assert inspector.calls == (
                    [apk, apk] if outcome == "intent_persist_failed" else [apk]
                )
                repository = app.state.agent_task_service._repository
                assert list(repository._definitions) == [UUID(created["analysis_id"])]
                projected = client.get(
                    f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
                ).json()
            captured = None
        elif outcome == "published_restart_reconciled":
            assert finalize_request().status_code == 200
            projected = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            ).json()
            captured = None
        else:
            assert finalize_request().status_code == 200
            captured = asyncio.run(
                _run_real_remote_agent_capture(
                    app=app,
                    tmp_path=tmp_path,
                    private_key=private_key,
                    credentials=credentials,
                    team_id=user.team_id,
                    device_id=UUID(device["device_id"]),
                    device_digest=device["device_digest"],
                    block_capture=outcome == "canceled",
                    fail_capture=outcome == "failed",
                    cancel_analysis_id=(
                        created["analysis_id"] if outcome == "canceled" else None
                    ),
                    browser_cookies=(
                        {cookie.name: cookie.value for cookie in client.cookies.jar}
                        if outcome in {"canceled", "analyzing_canceled"}
                        else None
                    ),
                    csrf_token=(
                        browser_headers["x-csrf-token"]
                        if outcome in {"canceled", "analyzing_canceled"}
                        else None
                    ),
                    wait_for_report=outcome
                    in {
                        "completed",
                        "source_strong",
                        "smartperfetto_partial",
                        "smartperfetto_all_failed",
                    },
                    analysis_id=UUID(created["analysis_id"]),
                    complete_strong_source=outcome == "source_strong",
                    cancel_during_analysis=outcome == "analyzing_canceled",
                )
            )
            projected = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            ).json()
            if outcome in {
                "completed",
                "source_strong",
                "smartperfetto_partial",
                "smartperfetto_all_failed",
            }:
                for _ in range(200):
                    report_response = client.get(
                        (
                            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/report"
                        )
                    )
                    if report_response.status_code == 200:
                        break
                    time.sleep(0.01)
            if outcome == "source_strong":
                for _ in range(200):
                    active_task = app.state.local_runtime.analyses[
                        (user.team_id, UUID(created["analysis_id"]))
                    ].task
                    if active_task is None or active_task.done():
                        break
                    time.sleep(0.01)
                rerun = client.post(
                    f"/v1/teams/{user.team_id}/analyses/"
                    f"{created['analysis_id']}/synthesis-runs",
                    headers=browser_headers,
                )
                assert rerun.status_code == 201, rerun.text
                assert rerun.json()["generation"] == 2
                for _ in range(200):
                    report_response = client.get(
                        f"/v1/teams/{user.team_id}/analyses/"
                        f"{created['analysis_id']}/report"
                    )
                    if (
                        report_response.status_code == 200
                        and report_response.json()["report_version"] == 2
                    ):
                        break
                    time.sleep(0.01)
            projected = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            ).json()

    if outcome == "accepted_restart_resumed":
        restart_gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
        restart_provider = _CountingProjectionReportProvider()
        restarted = create_local_app(
            gateway=restart_gateway,
            synthesizer=LocalReportSynthesizer(provider=restart_provider),
            data_root=tmp_path / "data",
            state_root=tmp_path / "state",
            control_store=control,
            apk_inspector=inspector,
            device_probe=host_probe,
            public_origin="https://testserver",
        )
        with _RawTestClient(restarted) as client:
            browser_headers = _authenticated_client(
                client, "user01", "established user password"
            )
            for _ in range(200):
                report_response = client.get(
                    f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/report",
                    headers=browser_headers,
                )
                if report_response.status_code == 200:
                    break
                time.sleep(0.01)
            projected = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}",
                headers=browser_headers,
            ).json()
            client.cookies.clear()
            delivery = client.get(
                "/v1/agent/tasks/next?wait_seconds=0",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
            )
        assert report_response.status_code == 200, report_response.text
        assert projected["state"] == "completed"
        assert restart_gateway.submissions == [
            (b"startup-trace", "startup", None),
            (b"scroll-trace", "scroll", None),
        ]
        assert restart_provider.calls == 1
        assert delivery.json()["action"] == "wait"
        converged_gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
        converged_provider = _CountingProjectionReportProvider()
        converged = create_local_app(
            gateway=converged_gateway,
            synthesizer=LocalReportSynthesizer(provider=converged_provider),
            data_root=tmp_path / "data",
            state_root=tmp_path / "state",
            control_store=control,
            apk_inspector=inspector,
            device_probe=host_probe,
            public_origin="https://testserver",
        )
        with _RawTestClient(converged) as client:
            browser_headers = _authenticated_client(
                client, "user01", "established user password"
            )
            converged_report = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/report",
                headers=browser_headers,
            )
        assert converged_report.status_code == 200
        assert converged_gateway.submissions == []
        assert converged_provider.calls == 0
    elif outcome in {"restart_reconciled", "published_restart_reconciled"}:
        restarted = create_local_app(
            gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
            data_root=tmp_path / "data",
            state_root=tmp_path / "state",
            control_store=control,
            apk_inspector=inspector,
            device_probe=host_probe,
            public_origin="https://testserver",
        )
        with _RawTestClient(restarted) as client:
            browser_headers = _authenticated_client(
                client, "user01", "established user password"
            )
            projected = client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            ).json()
            client.cookies.clear()
            delivery = client.get(
                "/v1/agent/tasks/next?wait_seconds=0",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
            )
            assert delivery.status_code == 200, delivery.text
            assert delivery.json()["action"] == "execute"
            repository = restarted.state.agent_task_service._repository
            assert list(repository._definitions) == [UUID(created["analysis_id"])]
            assert restarted.state.agent_upload_service._inputs
            assert inspector.calls == [apk]
    assert host_probe.calls == 0
    if outcome in {
        "enqueue_failed",
        "intent_persist_failed",
        "published_persist_failed",
        "restart_reconciled",
        "published_restart_reconciled",
    }:
        assert projected["state"] == "queued"
    elif outcome == "completed":
        assert projected["scenarios"][2]["state"] == "not_requested"
        assert captured is not None
        assert captured.installed == [apk]
        assert captured.captured == ["startup", "scroll"]
        assert captured.cleaned >= 1
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        assert report["schema_version"] == "1.2"
        assert [item["scenario_type"] for item in report["scenario_reports"]] == [
            "startup",
            "scroll",
        ]
        assert smartperfetto.submissions == [
            (b"startup-trace", "startup", None),
            (b"scroll-trace", "scroll", None),
        ]
        assert provider.calls == 1
        assert projected["state"] == "completed"
        assert projected["failure"] is None
        original_metadata = report["smartperfetto_original"]
        assert original_metadata["mime"] == "text/html"
        assert original_metadata["version"] == 2
        originals = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/smartperfetto-original"
        )
        assert originals.status_code == 200, originals.text
        assert originals.content == scenario_originals["startup"]
        downloaded = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            "/smartperfetto-original?download=true"
        )
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == scenario_originals["startup"]
        assert downloaded.headers["content-disposition"] == (
            f'attachment; filename="smartperfetto-{created["analysis_id"]}.html"'
        )
    elif outcome == "source_strong":
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        assert report["state"] == "completed"
        assert [item["scenario_type"] for item in report["scenario_reports"]] == [
            "startup",
            "scroll",
        ]
        assert report["source_code"]["context_state"] == "available"
        assert report["source_code"]["match_summary"] == "strong"
        fix = report["source_code"]["fixes"][0]
        assert fix["relative_path"] == "app/src/main/java/demo/Startup.kt"
        assert fix["symbol"] == "demo.Startup.init"
        assert fix["diff"].startswith("diff --git a/app/")
        assert report["report_version"] == 2
        assert smartperfetto.submissions == [
            (b"startup-trace", "startup", None),
            (b"scroll-trace", "scroll", None),
        ]
        assert provider.calls == 2
    elif outcome == "smartperfetto_partial":
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        assert report["state"] == "partially_completed"
        assert [item["scenario_type"] for item in report["scenario_reports"]] == [
            "startup",
            "scroll",
        ]
        assert report["scenario_reports"][0]["result_state"] == "completed"
        assert report["scenario_reports"][1]["result_state"] == "failed"
        assert smartperfetto.submissions == [
            (b"startup-trace", "startup", None),
            (b"scroll-trace", "scroll", None),
        ]
        assert provider.calls == 1
        assert projected["state"] == "partially_completed"
        assert projected["scenarios"][2]["state"] == "not_requested"
        original_metadata = report["smartperfetto_original"]
        assert original_metadata["mime"] == "text/html"
        assert original_metadata["version"] == 2
        original = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            "/smartperfetto-original"
        )
        assert original.status_code == 200
        assert original.headers["content-type"].startswith("text/html")
    elif outcome == "smartperfetto_all_failed":
        assert projected["state"] == "failed"
        assert projected["failure"]["code"] == "smartperfetto_all_failed"
        assert projected["report_available"] is False
        assert smartperfetto.submissions == [
            (b"startup-trace", "startup", None),
            (b"scroll-trace", "scroll", None),
        ]
        assert provider.calls == 0
        assert report_response.status_code == 404
        original = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/smartperfetto-original"
        )
        assert original.status_code == 404
    elif outcome == "analyzing_canceled":
        assert projected["state"] == "canceled"
        assert provider.calls == 0
        for _ in range(100):
            if smartperfetto.cancel_calls:
                break
            time.sleep(0.01)
        assert len(smartperfetto.cancel_calls) == 1
        assert (
            client.get(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/report"
            ).status_code
            == 404
        )
    elif outcome == "accepted_restart_resumed":
        assert report_response.status_code == 200
    elif outcome == "failed":
        assert projected["scenarios"][2]["state"] == "not_requested"
        assert captured is not None
        assert captured.installed == [apk]
        assert captured.captured == ["startup", "scroll"]
        assert captured.cleaned >= 1
        assert projected["state"] == "failed"
        assert projected["failure"]["code"] == "remote_capture_failed"
    else:
        assert projected["scenarios"][2]["state"] == "not_requested"
        assert captured is not None
        assert captured.installed == [apk]
        assert captured.captured == ["startup"]
        assert captured.cleaned >= 1
        assert projected["state"] == "canceled"
        assert projected["cancel_requested_at"] is not None


def test_local_remote_device_response_marks_memory_cycle_not_requested(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    runtime = app.state.local_runtime
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("82000000-0000-4000-8000-000000000001")
    upload_id = "85000000-0000-4000-8000-000000000001"
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=UUID("91000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
    )
    analysis = _LocalAnalysis(
        team_id=team_id,
        analysis_id=analysis_id,
        profile="startup",
        question=None,
        inputs={
            "apk": _LocalInput(
                _InputDescriptor(
                    kind="apk",
                    mime="application/vnd.android.package-archive",
                    size=1,
                    sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                ),
                upload_id=upload_id,
            )
        },
        analysis_mode="device",
        device_id=UUID("72000000-0000-4000-8000-000000000001"),
        source_binding=binding,
        source_code_analysis=_source_code_analysis_document(binding),
        state="queued",
    )
    runtime.analyses[(team_id, analysis_id)] = analysis
    runtime._register_upload(
        _LocalUpload(
            upload_id=upload_id,
            team_id=team_id,
            analysis_id=analysis_id,
            kind="apk",
            mime="application/vnd.android.package-archive",
            size=1,
            sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
            token="remote-device-upload-token",
            path=tmp_path / "apk",
        )
    )

    response = runtime.response(analysis)

    assert response["schema_version"] == "1.1"
    assert [item["state"] for item in response["scenarios"]] == [
        "queued",
        "queued",
        "not_requested",
    ]
    assert response["scenarios"][2]["sample_verdict_counts"]["total"] == 0
    assert response["scenarios"][2]["failure"] is None


def test_local_nonremote_device_response_preserves_legacy_1_0_memory_cycle(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    runtime = app.state.local_runtime
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("82000000-0000-4000-8000-000000000001")
    upload_id = "85000000-0000-4000-8000-000000000001"
    checksum = base64.b64encode(hashlib.sha256(b"x").digest()).decode()
    analysis = _LocalAnalysis(
        team_id=team_id,
        analysis_id=analysis_id,
        profile="startup",
        question=None,
        inputs={
            "apk": _LocalInput(
                _InputDescriptor(
                    kind="apk",
                    mime="application/vnd.android.package-archive",
                    size=1,
                    sha256_b64=checksum,
                ),
                upload_id=upload_id,
            )
        },
        analysis_mode="device",
        device_id=UUID("72000000-0000-4000-8000-000000000001"),
        state="queued",
    )
    runtime.analyses[(team_id, analysis_id)] = analysis
    runtime._register_upload(
        _LocalUpload(
            upload_id=upload_id,
            team_id=team_id,
            analysis_id=analysis_id,
            kind="apk",
            mime="application/vnd.android.package-archive",
            size=1,
            sha256_b64=checksum,
            token="legacy-device-upload-token",
            path=tmp_path / "apk",
        )
    )

    response = runtime.response(analysis)

    assert response["schema_version"] == "1.0"
    assert [item["state"] for item in response["scenarios"]] == [
        "queued",
        "queued",
        "queued",
    ]


def test_local_remote_report_preserves_completed_startup_when_scroll_capture_fails(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    inspector = _FakeLocalApkInspector()
    smartperfetto = _FakeSmartPerfettoGateway(_smartperfetto_result())
    provider = _CountingProjectionReportProvider()
    app = create_local_app(
        gateway=smartperfetto,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        apk_inspector=inspector,
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
        public_origin="https://testserver",
    )
    private_key = Ed25519PrivateKey.generate()
    apk = b"partial remote agent apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Partial Capture Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "partial-capture-mac",
                "os_version": "macOS 15",
            },
        ).json()
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "partial-capture-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [
                    {
                        "client_ref": "74000000-0000-4000-8000-000000000001",
                        "serial": "emulator-5554",
                        "manufacturer": "Google",
                        "model": "Pixel",
                        "android_release": "16",
                        "api_level": 36,
                        "connection_type": "usb",
                        "adb_state": "device",
                        "battery_percent": 80,
                        "temperature_c": None,
                        "storage_available_bytes": 1024,
                        "property_error_code": None,
                    }
                ],
                "workspaces": [],
            },
        ).json()
        device = heartbeat["devices"][0]
        created = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.1",
                "analysis_mode": "device",
                "device_id": device["device_id"],
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        ).json()
        slot = created["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
                f"{put.path}?{put.query}",
                content=apk,
                headers=slot["required_headers"],
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/finalize-upload",
                headers=browser_headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(apk),
                },
            ).status_code
            == 200
        )
        asyncio.run(
            _run_real_remote_agent_capture(
                app=app,
                tmp_path=tmp_path,
                private_key=private_key,
                credentials=credentials,
                team_id=user.team_id,
                device_id=UUID(device["device_id"]),
                device_digest=device["device_digest"],
                fail_scenario="scroll",
                wait_for_report=True,
                analysis_id=UUID(created["analysis_id"]),
            )
        )
        response = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
        ).json()
        report_response = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/report"
        )

    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["state"] == "partially_completed"
    assert [item["scenario_type"] for item in report["scenario_reports"]] == [
        "startup",
        "scroll",
    ]
    assert report["scenario_reports"][0]["result_state"] == "completed"
    assert report["scenario_reports"][1]["result_state"] == "failed"
    assert smartperfetto.submissions == [(b"startup-trace", "startup", None)]
    assert provider.calls == 1
    assert response["scenarios"][2]["state"] == "not_requested"


@pytest.mark.parametrize(
    ("failed_profile", "failure_boundary"),
    [
        ("startup", "submit"),
        ("scroll", "submit"),
        ("startup", "copy"),
        ("scroll", "copy"),
    ],
)
def test_local_remote_scenario_failure_preserves_other_scenario_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_profile: str,
    failure_boundary: str,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(
        user.user_id, "initial user password", "established user password"
    )
    successful = "scroll" if failed_profile == "startup" else "startup"
    result = _smartperfetto_scenario_result(successful)
    originals = {
        scenario: (
            b"<!DOCTYPE html><html><body><h1>SmartPerfetto "
            + scenario.encode("ascii")
            + b"</h1></body></html>"
        )
        for scenario in ("startup", "scroll")
    }
    gateway = (
        _ScenarioSubmitFailingSmartPerfettoGateway(
            result,
            failed_profile=failed_profile,
            original_report_html_bytes=originals,
        )
        if failure_boundary == "submit"
        else _ScenarioOriginalSmartPerfettoGateway(
            result,
            original_report_html_bytes=originals,
        )
    )
    provider = _CountingProjectionReportProvider()
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
        apk_inspector=_FakeLocalApkInspector(),
        device_probe=_FakeDeviceProbe(
            _FakeDeviceStatus(state="disconnected", device=None)
        ),
        public_origin="https://testserver",
    )
    private_key = Ed25519PrivateKey.generate()
    apk = b"inverse remote partial apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")
    if failure_boundary == "copy":
        copy_completed_artifact = (
            app.state.local_runtime.agent_artifacts.copy_completed_artifact_to_fd
        )

        async def fail_selected_copy(**kwargs):
            if kwargs["artifact_kind"] == f"{failed_profile}_trace":
                raise RuntimeError("private artifact read failure detail")
            return await copy_completed_artifact(**kwargs)

        monkeypatch.setattr(
            app.state.local_runtime.agent_artifacts,
            "copy_completed_artifact_to_fd",
            fail_selected_copy,
        )

    with _RawTestClient(app) as client:
        browser_headers = _authenticated_client(
            client, "user01", "established user password"
        )
        registration = client.post(
            f"/v1/teams/{user.team_id}/agents/registration-codes",
            headers=browser_headers,
            json={"schema_version": "1.0", "name": "Inverse Capture Mac"},
        ).json()
        credentials = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": registration["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "inverse-capture-mac",
                "os_version": "macOS 15",
            },
        ).json()
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "inverse-capture-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [
                    {
                        "client_ref": "74000000-0000-4000-8000-000000000001",
                        "serial": "emulator-5554",
                        "manufacturer": "Google",
                        "model": "Pixel",
                        "android_release": "16",
                        "api_level": 36,
                        "connection_type": "usb",
                        "adb_state": "device",
                        "battery_percent": 80,
                        "temperature_c": None,
                        "storage_available_bytes": 1024,
                        "property_error_code": None,
                    }
                ],
                "workspaces": [],
            },
        ).json()
        device = heartbeat["devices"][0]
        created = client.post(
            f"/v1/teams/{user.team_id}/analyses",
            headers=browser_headers,
            json={
                "schema_version": "1.1",
                "analysis_mode": "device",
                "device_id": device["device_id"],
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        ).json()
        slot = created["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
                f"{put.path}?{put.query}", content=apk, headers=slot["required_headers"]
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/finalize-upload",
                headers=browser_headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(apk),
                },
            ).status_code
            == 200
        )
        asyncio.run(
            _run_real_remote_agent_capture(
                app=app,
                tmp_path=tmp_path,
                private_key=private_key,
                credentials=credentials,
                team_id=user.team_id,
                device_id=UUID(device["device_id"]),
                device_digest=device["device_digest"],
                wait_for_report=True,
                analysis_id=UUID(created["analysis_id"]),
            )
        )
        report_response = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}/report"
        )
        analysis_response = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
        )
        downloaded = client.get(
            f"/v1/teams/{user.team_id}/analyses/{created['analysis_id']}"
            "/smartperfetto-original?download=true"
        )
        core = app.state.local_runtime.store.load_document(
            user.team_id,
            UUID(created["analysis_id"]),
            "normalized-core.json",
        )

    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["state"] == "partially_completed"
    assert [item["scenario_type"] for item in report["scenario_reports"]] == [
        "startup",
        "scroll",
    ]
    states = {
        item["scenario_type"]: item["result_state"]
        for item in report["scenario_reports"]
    }
    assert states == {failed_profile: "failed", successful: "completed"}
    scenario_reports = {
        item["scenario_type"]: item for item in report["scenario_reports"]
    }
    assert scenario_reports[successful]["bundle"]["metrics"]
    assert scenario_reports[successful]["bundle"]["evidence"]
    assert scenario_reports[failed_profile]["bundle"]["metrics"] == []
    assert [item[0] for item in gateway.submissions] == [f"{successful}-trace".encode()]
    assert provider.calls == 1
    assert report["smartperfetto_original"]["mime"] == "text/html"
    assert report["smartperfetto_original"]["version"] == 2
    assert analysis_response.json()["scenarios"][2]["state"] == "not_requested"
    assert downloaded.status_code == 200
    assert downloaded.content == originals[successful]
    assert "private" not in report_response.text.lower()
    assert core is not None
    assert {item["scenario_type"] for item in core["scenario_reports"]} == {
        "startup",
        "scroll",
    }
    assert all(
        item["code"] != "android_memory.result_unavailable"
        for item in core["limitations"]
    )
    assert f"smartperfetto.{failed_profile}_result_unavailable" in {
        item["code"] for item in core["limitations"]
    }


def test_local_restart_resumes_persisted_remote_evidence_without_source(
    tmp_path: Path,
) -> None:
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("82000000-0000-4000-8000-000000000001")
    descriptor = _InputDescriptor(
        kind="apk",
        mime="application/vnd.android.package-archive",
        size=1,
        sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
    )
    analysis = _LocalAnalysis(
        team_id=team_id,
        analysis_id=analysis_id,
        profile="startup",
        question=None,
        inputs={"apk": _LocalInput(descriptor)},
        analysis_mode="device",
        state="analyzing",
        remote_publication="published",
    )
    analysis.stages.update(
        {
            "input_validation": "completed",
            "smartperfetto": "completed",
            "perfpilot_ai": "pending",
            "report": "pending",
        }
    )
    barrier = _PersistedEvidenceBarrierProvider()
    first = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=barrier),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
    )
    runtime = first.state.local_runtime
    capture_enqueue_calls = 0

    async def reject_capture_enqueue(_definition) -> None:
        nonlocal capture_enqueue_calls
        capture_enqueue_calls += 1
        raise AssertionError("persisted synthesis must not enqueue agent capture")

    runtime.agent_tasks.enqueue = reject_capture_enqueue
    runtime.analyses[(team_id, analysis_id)] = analysis
    prepared = _prepare_local_report(
        analysis,
        _smartperfetto_scenario_result("startup"),
        include_memory=False,
        remote_completed_scenarios=frozenset({"startup"}),
        remote_scenario_failures={"scroll": "smartperfetto_failed"},
    )

    async def reach_barrier() -> None:
        task = asyncio.create_task(
            runtime._publish_prepared(analysis, prepared, generation=1)
        )
        for _ in range(200):
            if barrier.entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert barrier.entered.is_set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()

    asyncio.run(reach_barrier())
    assert capture_enqueue_calls == 0
    gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    provider = _CountingProjectionReportProvider()
    restarted = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
    )
    with _RawTestClient(restarted):
        for _ in range(200):
            restored = restarted.state.local_runtime.analyses[(team_id, analysis_id)]
            if restored.report is not None:
                break
            time.sleep(0.01)
        assert restored.report is not None
        assert restored.state == "partially_completed"
        assert gateway.submissions == []
        assert provider.calls == 1
        assert capture_enqueue_calls == 0


@pytest.mark.asyncio
async def test_stale_generation_cannot_overwrite_newer_report(
    tmp_path: Path,
) -> None:
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    analysis_id = UUID("82000000-0000-4000-8000-000000000002")
    descriptor = _InputDescriptor(
        kind="trace",
        mime="application/octet-stream",
        size=1,
        sha256_b64=base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
    )
    analysis = _LocalAnalysis(
        team_id=team_id,
        analysis_id=analysis_id,
        profile="startup",
        question=None,
        inputs={"trace": _LocalInput(descriptor)},
        state="analyzing",
    )
    analysis.stages.update(
        {
            "input_validation": "completed",
            "smartperfetto": "completed",
            "perfpilot_ai": "pending",
            "report": "pending",
        }
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=None,
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
    )
    runtime = app.state.local_runtime
    runtime.analyses[(team_id, analysis_id)] = analysis
    await runtime._persist(analysis)
    prepared = _prepare_local_report(analysis, _smartperfetto_result())
    original_save_document = runtime.store.save_document
    stale_report_entered = threading.Event()
    release_stale_report = threading.Event()

    def gate_stale_report(*args, **kwargs) -> None:
        name = args[2] if len(args) >= 3 else kwargs.get("name")
        if name == "report.json":
            stale_report_entered.set()
            if not release_stale_report.wait(timeout=2):
                raise AssertionError("stale report gate timed out")
        original_save_document(*args, **kwargs)

    runtime.store.save_document = gate_stale_report
    stale_publish = asyncio.create_task(
        runtime._publish_prepared(analysis, prepared, generation=1)
    )
    assert await asyncio.to_thread(stale_report_entered.wait, 2)
    newer_report = {"schema_version": "1.2", "report_version": 2}

    async def publish_newer_generation() -> None:
        async with runtime._terminal_commit_lock(analysis):
            async with runtime.lock:
                analysis.generation = 2
                analysis.report = newer_report
            await asyncio.to_thread(
                original_save_document,
                team_id,
                analysis_id,
                "report.json",
                newer_report,
            )

    newer_publish = asyncio.create_task(publish_newer_generation())
    await asyncio.sleep(0.02)
    newer_completed_before_stale_commit = newer_publish.done()
    release_stale_report.set()
    await asyncio.gather(stale_publish, return_exceptions=True)
    await newer_publish
    persisted_report = await asyncio.to_thread(
        runtime.store.load_document,
        team_id,
        analysis_id,
        "report.json",
    )
    await runtime.close()
    app.state.agent_upload_service.close()
    app.state.local_agent_store.close()

    assert newer_completed_before_stale_commit is False
    assert persisted_report == newer_report

    converged_provider = _CountingProjectionReportProvider()
    converged = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=converged_provider),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
    )
    with _RawTestClient(converged):
        restored = converged.state.local_runtime.analyses[(team_id, analysis_id)]
        assert restored.report is not None
    assert converged_provider.calls == 0

@pytest.mark.skip(
    reason="remote device capture is not wired in the local control plane"
)
def test_local_device_analysis_captures_in_background_and_publishes_report(
    tmp_path: Path,
) -> None:
    serial = "0123456789ABCDEF"
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial=serial,
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    capture_gateway = _FakeLocalDeviceCaptureGateway()
    memory_gateway = _FakeLocalMemoryAnalysisGateway(_android_memory_result())
    smartperfetto = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    apk = b"installable-local-device-apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")
    app = create_local_app(
        gateway=smartperfetto,
        synthesizer=_test_synthesizer(),
        device_probe=device_probe,
        device_capture_gateway=capture_gateway,
        memory_analysis_gateway=memory_gateway,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        device_id = client.get(f"/v1/teams/{team_id}/devices").json()["devices"][0][
            "device_id"
        ]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "device",
                "device_id": device_id,
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        ).json()
        analysis_id = created["analysis_id"]
        slot = created["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
            f"{put.path}?{put.query}", content=apk, headers=slot["required_headers"]
            ).status_code
            == 200
        )
        assert (
            client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(apk),
            },
            ).status_code
            == 200
        )

        terminal = None
        for _ in range(200):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        assert terminal is not None
        assert terminal["state"] == "partially_completed"
        assert terminal["report_available"] is True
        assert terminal["application_version_id"] is not None
        assert terminal["application_metadata"] == {
            "package_name": "com.example.perfpilot",
            "version_name": "1.2.3",
            "version_code": 123,
            "launch_activity": "com.example.perfpilot/.MainActivity",
            "min_sdk": 26,
            "target_sdk": 35,
            "supported_abis": ["arm64-v8a"],
            "has_native_libraries": True,
        }
        assert terminal["started_at"] is not None
        assert terminal["completed_at"] is not None
        assert "ai_rounds" not in terminal
        assert "source_analysis" not in terminal
        assert [item["state"] for item in terminal["scenarios"]] == [
            "failed",
            "failed",
            "completed",
        ]
        assert terminal["scenarios"][0]["failure"]["code"] == "insufficient_data"
        assert terminal["scenarios"][1]["failure"]["code"] == "insufficient_data"
        assert terminal["scenarios"][2]["failure"] is None

        report_response = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert capture_gateway.calls
    captured_apk, captured_serial, workspace = capture_gateway.calls[0]
    assert captured_apk == apk
    assert captured_serial == serial
    assert workspace.name == "device-captures"
    assert workspace.parent.name == analysis_id
    assert workspace.parents[2].name == team_id
    assert smartperfetto.submissions == [
        (b"captured-startup-trace", "startup", None),
        (b"captured-scroll-trace", "scroll", None),
    ]
    assert len(memory_gateway.calls) == 1
    assert memory_gateway.calls[0]["analysis_id"] == UUID(analysis_id)
    assert memory_gateway.calls[0]["package_name"] == "com.example.perfpilot"
    assert memory_gateway.calls[0]["android_release"] == "13"
    assert memory_gateway.calls[0]["api_level"] == 33
    evidence_path = memory_gateway.calls[0]["evidence_path"]
    assert isinstance(evidence_path, Path)
    assert evidence_path.name == "memory-evidence.tar"
    assert memory_gateway.close_calls == 1
    assert report_response.status_code == 200
    report = validate_contract("analysis-report", report_response.json())
    assert report["analysis_mode"] == "device"
    assert [item["scenario_type"] for item in report["scenario_reports"]] == [
        "startup",
        "scroll",
        "memory_cycle",
    ]
    memory = report["scenario_reports"][2]
    assert memory["result_state"] == "completed"
    assert memory["failure"] is None
    assert memory["bundle"]["metrics"]
    metric_values = {
        metric["name"]: metric["numeric_value"]
        for metric in memory["bundle"]["metrics"]
    }
    assert metric_values["memory.meminfo.total.pss_kb"] == 123456
    assert metric_values["memory.meminfo.native_heap.private_dirty_kb"] == 30000
    assert serial not in json.dumps(report)

    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    restarted_app = create_local_app(
        gateway=restarted_gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(restarted_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 2
        for _ in range(100):
            rerun_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if rerun_state["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert rerun_state["state"] == "partially_completed"
    assert rerun_report.status_code == 200
    assert rerun_report.json()["report_version"] == 2
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0

    store = LocalAnalysisStore(tmp_path)
    parsed_analysis_id = UUID(analysis_id)
    legacy_state = store.load_states()[(UUID(team_id), parsed_analysis_id)]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    store.save_state(UUID(team_id), parsed_analysis_id, legacy_state)
    analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    (analysis_directory / "normalized-core.json").unlink()
    old_report_bytes = (analysis_directory / "report.json").read_bytes()
    old_report = rerun_report.json()
    legacy_provider = _InvalidReportProvider()
    legacy_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    legacy_app = create_local_app(
        gateway=legacy_gateway,
        synthesizer=LocalReportSynthesizer(provider=legacy_provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(legacy_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 3
        for _ in range(100):
            legacy_rerun_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if legacy_rerun_state["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)
        preserved_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert legacy_rerun_state["state"] == "partially_completed"
    assert legacy_rerun_state["failure"]["code"] == "ai_source_evidence_unavailable"
    assert preserved_report.json() == old_report
    assert (analysis_directory / "report.json").read_bytes() == old_report_bytes
    assert legacy_provider.calls == 0
    assert legacy_gateway.submissions == []
    assert legacy_gateway.status_calls == 0
    assert legacy_gateway.fetch_calls == 0
    assert store.load_states()[(UUID(team_id), parsed_analysis_id)]["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "failed", "attempts": 0}
    ]


def test_local_app_lists_one_active_analysis_and_rejects_a_second(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, _ = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )

        active = client.get(
            f"/v1/teams/{team_id}/analyses?status=active&limit=1"
        )
        duplicate = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": "com.example",
                "question": None,
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": 1,
                        "sha256_b64": base64.b64encode(
                            hashlib.sha256(b"x").digest()
                        ).decode("ascii"),
                    }
                ],
            },
        )

    assert active.status_code == 200
    assert [item["analysis_id"] for item in active.json()["analyses"]] == [
        analysis_id
    ]
    assert active.json()["analyses"][0]["cancel_requested_at"] is None
    assert duplicate.status_code == 409


def test_local_app_rolls_back_create_and_reserve_when_persistence_fails(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(app) as client:
        runtime = app.state.local_runtime
        original_save_state = runtime.store.save_state

        def fail_save_state(*_args) -> None:
            raise LocalAnalysisStoreError("local analysis persistence failed")

        runtime.store.save_state = fail_save_state
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        with pytest.raises(LocalAnalysisStoreError):
            _create_trace_analysis(client, team_id=team_id, headers=headers)
        assert runtime.analyses == {}
        assert runtime.uploads == {}

        runtime.store.save_state = original_save_state
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        runtime.store.save_state = fail_save_state
        with pytest.raises(LocalAnalysisStoreError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
                headers=headers,
                json={
                    "artifact_kind": "trace",
                    "mime": "application/octet-stream",
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            )
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        assert analysis.inputs["trace"].upload_id is None
        assert runtime.uploads == {}
        runtime.store.save_state = original_save_state
        retry = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        )
        assert retry.status_code == 201


def test_local_app_does_not_launch_finalize_task_before_persistence(
    tmp_path: Path,
) -> None:
    gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(gateway=gateway, data_root=tmp_path)
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        runtime = app.state.local_runtime
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
            f"{put.path}?{put.query}",
            content=b"background-local-trace",
            headers=slot["required_headers"],
            ).status_code
            == 200
        )
        original_save_state = runtime.store.save_state

        def fail_save_state(*_args) -> None:
            raise LocalAnalysisStoreError("local analysis persistence failed")

        runtime.store.save_state = fail_save_state
        with pytest.raises(LocalAnalysisStoreError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
                headers=headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(b"background-local-trace"),
                },
            )
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        assert analysis.inputs["trace"].finalized is False
        assert analysis.task is None
        assert gateway.submissions == []
        runtime.store.save_state = original_save_state
        retry = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(b"background-local-trace"),
            },
        )
        assert retry.status_code == 200
        for _ in range(100):
            if gateway.submissions:
                break
            time.sleep(0.01)
        assert gateway.submissions


def test_local_app_keeps_committed_finalize_state_when_durability_is_uncertain(
    tmp_path: Path,
) -> None:
    gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(gateway=gateway, data_root=tmp_path)
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        runtime = app.state.local_runtime
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
            f"{put.path}?{put.query}",
            content=b"background-local-trace",
            headers=slot["required_headers"],
            ).status_code
            == 200
        )
        original_persist = runtime._persist

        async def persist_then_report_uncertain(analysis) -> None:
            await original_persist(analysis)
            raise LocalAnalysisStoreDurabilityError("local analysis durability uncertain")

        runtime._persist = persist_then_report_uncertain
        with pytest.raises(LocalAnalysisStoreDurabilityError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
                headers=headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(b"background-local-trace"),
                },
            )
        runtime._persist = original_persist
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        persisted = LocalAnalysisStore(tmp_path).load_states()[
            (UUID(team_id), UUID(analysis_id))
        ]
        assert analysis.inputs["trace"].finalized is True
        assert persisted["inputs"][0]["finalized"] is True
        for _ in range(100):
            if gateway.submissions:
                break
            time.sleep(0.01)
        assert gateway.submissions


def test_local_app_cancel_stops_smartperfetto_and_persists_the_terminal_state(
    tmp_path: Path,
) -> None:
    gateway = _BlockingSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )

        running = None
        for _ in range(100):
            running = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if running["source_analysis"]["session_id"] is not None:
                break
            time.sleep(0.01)
        assert running is not None
        assert running["state"] == "analyzing"

        assert (
            client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel"
            ).status_code
            == 403
        )
        canceled = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel",
            headers=headers,
        )
        repeated = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel",
            headers=headers,
        )
        after = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}"
        )
        active = client.get(
            f"/v1/teams/{team_id}/analyses?status=active&limit=1"
        )

    assert canceled.status_code == 202
    assert canceled.json()["state"] == "canceled"
    assert isinstance(canceled.json()["cancel_requested_at"], str)
    assert all(
        stage["state"] in {"completed", "canceled"}
        for stage in canceled.json()["stages"]
    )
    assert repeated.status_code == 200
    assert (
        repeated.json()["cancel_requested_at"] == canceled.json()["cancel_requested_at"]
    )
    assert after.json()["state"] == "canceled"
    assert active.json()["analyses"] == []
    assert gateway.cancel_calls == [
        LocalEngineRun(session_id="session-local-1", run_id="run-local-1")
    ]

    restored_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(restored_app) as client:
        restored = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}"
        )
    assert restored.status_code == 200
    assert restored.json()["state"] == "canceled"
    assert (
        restored.json()["cancel_requested_at"] == canceled.json()["cancel_requested_at"]
    )


@pytest.mark.parametrize(
    ("result_factory", "expected_state", "expected_finding"),
    [
        (_smartperfetto_result, "completed", None),
        (_live_smartperfetto_result, "partially_completed", "JIT 编译活跃"),
    ],
)
def test_local_app_accepts_a_trace_and_publishes_a_real_contract_report(
    tmp_path: Path,
    result_factory,
    expected_state: str,
    expected_finding: str | None,
) -> None:
    trace = b"local-perfetto-trace"
    target_package = (
        "com.example.app"
        if result_factory is _live_smartperfetto_result
        else "com.example"
    )
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")
    gateway = _FakeSmartPerfettoGateway(result_factory())
    app = create_local_app(
        gateway=gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf")
        assert csrf.status_code == 200
        csrf_token = csrf.json()["csrf_token"]
        headers = {"x-csrf-token": csrf_token}

        me = client.get("/v1/me")
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "ray_wu"
        team_id = me.json()["memberships"][0]["team"]["id"]

        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": target_package,
                "question": "首帧为什么慢？",
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
            },
        )
        assert created.status_code == 201
        created_document = created.json()
        assert created_document["test_type"] == "cold_start"
        assert created_document["package_name"] == target_package
        assert created_document["custom_test_name"] is None
        assert created_document["custom_test_description"] is None
        analysis_id = created_document["analysis_id"]

        reserved = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(trace),
                "sha256_b64": checksum,
            },
        )
        assert reserved.status_code == 201
        slot = reserved.json()["upload"]
        parsed_put = urlsplit(slot["put_url"])
        uploaded = client.put(
            f"{parsed_put.path}?{parsed_put.query}",
            content=trace,
            headers=slot["required_headers"],
        )
        assert uploaded.status_code == 200

        finalized = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(trace),
            },
        )
        assert finalized.status_code == 200

        terminal = None
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}",
                headers=headers,
            )
            if terminal.json()["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)

        assert terminal is not None
        assert terminal.status_code == 200
        assert terminal.json()["state"] == expected_state
        assert terminal.json()["report_available"] is True
        assert terminal.json()["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        assert gateway.submissions == [
            (
                trace,
                "startup",
                f"Only analyze Android package {target_package}.\n\n"
                "The captured scenario is cold start.\n\n"
                "Ignore unrelated processes and state when target evidence is insufficient.\n\n"
                "Additional analysis context: 首帧为什么慢？",
            )
        ]

        report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report",
            headers=headers,
        )
        assert report.status_code == 200
        validated = validate_contract("analysis-report", report.json())
        assert validated["analysis_id"] == analysis_id
        assert validated["synthesis"]["state"] == "completed"
        assert (
            validated["synthesis"]["provenance"]["prompt_template_version"]
            == "perfpilot-report-v3-test"
        )
        assert validated["schema_version"] == "1.2"
        assert len(validated["synthesis"]["output"]["key_metric_ids"]) <= 3
        assert len(validated["synthesis"]["output"]["top_findings"]) <= 3
        assert len(validated["synthesis"]["output"]["recommendations"]) <= 3
        assert validated["synthesis"]["output"]["recommendations"]
        assert validated["smartperfetto_original"]["available"] is True
        original = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original"
        )
        downloaded = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original?download=true"
        )
        assert original.status_code == 200
        assert original.content == gateway.original_report_html_bytes
        assert original.headers["content-type"].startswith("text/html")
        assert original.headers["cache-control"] == "private, no-store"
        assert original.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in original.headers
        assert downloaded.content == original.content
        assert downloaded.headers["content-disposition"] == (
            f'attachment; filename="smartperfetto-{analysis_id}.html"'
        )
        analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
        assert (analysis_directory / "round-1.json").is_file()
        assert not (analysis_directory / "round-2.json").exists()
        assert not (analysis_directory / "round-3.json").exists()
        if expected_finding is not None:
            bundle = validated["scenario_reports"][0]["bundle"]
            assert bundle is not None
            assert bundle["findings"][0]["title"] == expected_finding
            assert {metric["name"] for metric in bundle["metrics"]} >= {
                "startup.startup_analysis_get_startups.dur_ms",
                "startup.startup_analysis_get_startups.ttid_ms",
            }


def test_local_app_downloads_exact_upstream_smartperfetto_html_bytes(
    tmp_path: Path,
) -> None:
    result = _smartperfetto_result()
    raw = (
        b'<!DOCTYPE html>\n<html><body data-order="b a">'
        b'\xe4\xb8\xad\\u6587</body></html>\n'
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(
            result,
            original_report_html_bytes=raw,
        ),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )
        for _ in range(200):
            state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()["state"]
            if state in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        downloaded = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original?download=true"
        )
        report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    assert downloaded.status_code == 200
    assert downloaded.content == raw
    assert downloaded.headers["content-type"].startswith("text/html")
    assert downloaded.headers["content-disposition"] == (
        f'attachment; filename="smartperfetto-{analysis_id}.html"'
    )
    assert report["smartperfetto_original"]["size"] == len(raw)
    assert report["smartperfetto_original"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_local_app_publishes_core_report_when_ai_projection_is_privacy_blocked(
    tmp_path: Path,
) -> None:
    result = _live_smartperfetto_result()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(result),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
            question="Trace evidence is stored at /Users/example/private/trace.pb",
            package_name="com.example.app",
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )

        terminal = None
        for _ in range(200):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)

        assert terminal is not None
        assert terminal["state"] == "partially_completed"
        assert terminal["report_available"] is True
        assert terminal["stages"][-2]["state"] == "failed"
        assert terminal["stages"][-1]["state"] == "completed"
        published = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert published.status_code == 200
    validated = validate_contract("analysis-report", published.json())
    assert validated["synthesis"]["state"] == "failed"
    assert validated["synthesis"]["failure_code"] == "ai_projection_private_data"
    bundle = validated["scenario_reports"][0]["bundle"]
    assert bundle is not None
    assert bundle["findings"][0]["title"] == "JIT 编译活跃"
    projection = LocalAnalysisStore(tmp_path).load_document(
        UUID(team_id),
        UUID(analysis_id),
        "projection.json",
    )
    assert projection["question"] is None
    assert projection["scenarios"] == []
    assert "/Users/example/private/trace.pb" not in json.dumps(projection)


def test_local_app_persists_bounded_single_pass_failure(tmp_path: Path) -> None:
    provider = _InvalidReportProvider()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )

        terminal = None
        for _ in range(200):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)

        assert terminal is not None
        assert terminal["state"] == "partially_completed"
        assert terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "failed", "attempts": 2}
        ]
        published = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )
        original = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original"
        )

    assert provider.calls == 2
    assert published.status_code == 200
    assert original.status_code == 200
    assert original.content == (
        b"<!DOCTYPE html><html><body>SmartPerfetto</body></html>"
    )
    report = validate_contract("analysis-report", published.json())
    assert report["synthesis"]["state"] == "failed"
    assert report["synthesis"]["failure_code"] == "ai_output_invalid"
    assert report["scenario_reports"][0]["bundle"] is not None
    expected_failed_rounds = [
        {"round": 1, "role": "report", "state": "failed", "attempts": 2}
    ]
    persisted = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))]
    assert persisted["ai_rounds"] == expected_failed_rounds

    restarted_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(restarted_app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")

    assert restored.status_code == 200
    assert restored.json()["ai_rounds"] == expected_failed_rounds


def test_local_app_publishes_aggregate_token_usage_after_a_valid_retry(
    tmp_path: Path,
) -> None:
    provider = _AggregateTokenUsageReportProvider()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )
        terminal = None
        for _ in range(200):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["report_available"]:
                break
            time.sleep(0.01)
        assert terminal is not None
        assert terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 2}
        ]
        published = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert provider.calls == 2
    assert published.status_code == 200
    report = validate_contract("analysis-report", published.json())
    provenance = report["synthesis"]["provenance"]
    assert provenance["prompt_tokens"] == 14
    assert provenance["completion_tokens"] == 18
    assert provenance["total_tokens"] == 32


def test_local_app_restores_a_completed_report_after_restart(tmp_path: Path) -> None:
    trace = b"persistent-local-trace"
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")

    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
        csrf_token = client.get("/v1/auth/csrf").json()["csrf_token"]
        headers = {"x-csrf-token": csrf_token}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "trace_upload",
                "test_type": "cold_start",
                "package_name": "com.example",
                "question": None,
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
            },
        ).json()
        analysis_id = created["analysis_id"]
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(trace),
                "sha256_b64": checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert (
            client.put(
                f"{put.path}?{put.query}",
                content=trace,
                headers=slot["required_headers"],
            ).status_code
            == 200
        )
        assert (
            client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(trace),
                },
            ).status_code
            == 200
        )
        for _ in range(100):
            state = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}").json()
            if state["report_available"]:
                break
            time.sleep(0.01)
        expected_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    store = LocalAnalysisStore(tmp_path)
    states = store.load_states()
    original_id = UUID(analysis_id)
    parsed_team_id = UUID(team_id)
    original_state = states[(parsed_team_id, original_id)]
    assert isinstance(original_state.get("created_at"), str)
    assert original_state["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]

    legacy_id = UUID("92000000-0000-4000-8000-000000000004")
    legacy_state = copy.deepcopy(original_state)
    legacy_state["analysis_id"] = str(legacy_id)
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    expected_legacy_rounds = [
        {"round": 1, "role": "extract", "state": "completed", "attempts": 1},
        {"round": 2, "role": "review", "state": "completed", "attempts": 1},
        {"round": 3, "role": "finalize", "state": "completed", "attempts": 1},
    ]
    legacy_state["ai_rounds"] = expected_legacy_rounds
    legacy_report = copy.deepcopy(expected_report)
    legacy_report["analysis_id"] = str(legacy_id)
    assert legacy_report["report_version"] == 1
    store.save_state(parsed_team_id, legacy_id, legacy_state)
    store.save_document(parsed_team_id, legacy_id, "report.json", legacy_report)
    legacy_projection = store.load_document(parsed_team_id, original_id, "projection.json")
    legacy_core = store.load_document(parsed_team_id, original_id, "normalized-core.json")
    legacy_source_report = store.load_document(
        parsed_team_id,
        original_id,
        "smartperfetto-report.json",
    )
    assert legacy_projection is not None
    assert legacy_core is not None
    assert legacy_source_report is not None
    legacy_projection["analysis_id"] = str(legacy_id)
    legacy_core["analysis_id"] = str(legacy_id)
    store.save_document(parsed_team_id, legacy_id, "projection.json", legacy_projection)
    store.save_document(parsed_team_id, legacy_id, "normalized-core.json", legacy_core)
    store.save_document(
        parsed_team_id,
        legacy_id,
        "smartperfetto-report.json",
        legacy_source_report,
    )
    legacy_state["evidence_format_version"] = "normalized-core-v1"
    legacy_state["evidence_manifest"] = _evidence_manifest(
        core=legacy_core,
        source=legacy_source_report,
        projection=legacy_projection,
    )
    store.save_state(parsed_team_id, legacy_id, legacy_state)
    legacy_round_2 = {"sentinel": "legacy-round-2"}
    legacy_round_3 = {"sentinel": "legacy-round-3"}
    store.save_document(parsed_team_id, legacy_id, "round-2.json", legacy_round_2)
    store.save_document(parsed_team_id, legacy_id, "round-3.json", legacy_round_3)
    legacy_directory = tmp_path / "teams" / team_id / "analyses" / str(legacy_id)
    legacy_round_2_bytes = (legacy_directory / "round-2.json").read_bytes()
    legacy_round_3_bytes = (legacy_directory / "round-3.json").read_bytes()

    newer_id = UUID("92000000-0000-4000-8000-000000000002")
    newer_state = copy.deepcopy(original_state)
    newer_state["analysis_id"] = str(newer_id)
    newer_state["created_at"] = "2099-01-01T00:00:00+00:00"
    newer_report = copy.deepcopy(expected_report)
    newer_report["analysis_id"] = str(newer_id)
    store.save_state(parsed_team_id, newer_id, newer_state)
    store.save_document(parsed_team_id, newer_id, "report.json", newer_report)

    pending_id = UUID("92000000-0000-4000-8000-000000000003")
    pending_state = copy.deepcopy(original_state)
    pending_state["analysis_id"] = str(pending_id)
    pending_state["created_at"] = "2100-01-01T00:00:00+00:00"
    pending_state["report_available"] = False
    store.save_state(parsed_team_id, pending_id, pending_state)

    migrated_state = copy.deepcopy(original_state)
    migrated_state.pop("created_at")
    store.save_state(parsed_team_id, original_id, migrated_state)

    restored_gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    second_app = create_local_app(
        gateway=restored_gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")
        assert restored.status_code == 200
        assert restored.json()["report_available"] is True
        assert restored.json()["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        assert datetime.fromisoformat(restored.json()["created_at"]) == datetime.fromisoformat(
            expected_report["generated_at"].replace("Z", "+00:00")
        )
        assert (
            client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}/report").json()
            == expected_report
        )
        latest = client.get(
            f"/v1/teams/{team_id}/analyses?report_available=true&limit=1"
        )
        assert latest.status_code == 200
        assert latest.json()["schema_version"] == "1.0"
        assert [item["analysis_id"] for item in latest.json()["analyses"]] == [
            str(newer_id)
        ]
        assert all(item["report_available"] for item in latest.json()["analyses"])
        legacy = client.get(f"/v1/teams/{team_id}/analyses/{legacy_id}")
        assert legacy.status_code == 200
        assert legacy.json()["ai_rounds"] == expected_legacy_rounds
        assert (
            store.load_document(parsed_team_id, legacy_id, "round-2.json")
            == legacy_round_2
        )
        assert (
            store.load_document(parsed_team_id, legacy_id, "round-3.json")
            == legacy_round_3
        )
        assert (legacy_directory / "round-2.json").read_bytes() == legacy_round_2_bytes
        assert (legacy_directory / "round-3.json").read_bytes() == legacy_round_3_bytes

        legacy_version = legacy.json()["version"]
        second_headers = {
            "x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]
        }
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{legacy_id}/synthesis-runs",
            headers=second_headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 2
        legacy_terminal = legacy.json()
        for _ in range(100):
            legacy_terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{legacy_id}"
            ).json()
            if (
                legacy_terminal["version"] > legacy_version
                and legacy_terminal["stages"][2]["state"] == "completed"
                and legacy_terminal["stages"][3]["state"] == "completed"
            ):
                break
            time.sleep(0.01)
        assert legacy_terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{legacy_id}/report"
        )
        assert rerun_report.status_code == 200
        assert rerun_report.json()["report_version"] == 2
        assert (
            store.load_document(parsed_team_id, legacy_id, "round-1.json") is not None
        )
        assert (
            store.load_document(parsed_team_id, legacy_id, "round-2.json")
            == legacy_round_2
        )
        assert (
            store.load_document(parsed_team_id, legacy_id, "round-3.json")
            == legacy_round_3
        )
        assert (legacy_directory / "round-2.json").read_bytes() == legacy_round_2_bytes
        assert (legacy_directory / "round-3.json").read_bytes() == legacy_round_3_bytes

    assert restored_gateway.submissions == []


def test_local_app_reruns_ai_from_persisted_evidence_after_restart(
    tmp_path: Path,
) -> None:
    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )
        for _ in range(100):
            first_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if first_state["report_available"]:
                break
            time.sleep(0.01)
        assert first_state["state"] == "completed"

    legacy_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    (legacy_directory / "normalized-core.json").unlink(missing_ok=True)
    legacy_store = LocalAnalysisStore(tmp_path)
    legacy_state = legacy_store.load_states()[(UUID(team_id), UUID(analysis_id))]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    legacy_store.save_state(UUID(team_id), UUID(analysis_id), legacy_state)
    assert {
        "projection.json",
        "report.json",
        "smartperfetto-report.json",
    }.issubset(path.name for path in legacy_directory.iterdir())

    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    second_app = create_local_app(
        gateway=restarted_gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 2
        for _ in range(100):
            rerun_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if rerun_state["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert rerun_state["state"] == "completed"
    assert rerun_state["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]
    assert rerun_report.status_code == 200
    assert rerun_report.json()["report_version"] == 2
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0
    migrated_core = LocalAnalysisStore(tmp_path).load_document(
        UUID(team_id),
        UUID(analysis_id),
        "normalized-core.json",
    )
    assert migrated_core is not None
    assert (
        validate_contract("normalized-trace-report", migrated_core)["analysis_id"]
        == analysis_id
    )
    assert (
        LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))][
        "evidence_format_version"
        ]
        == "normalized-core-v1"
    )

    store = LocalAnalysisStore(tmp_path)
    parsed_analysis_id = UUID(analysis_id)
    parsed_team_id = UUID(team_id)
    legacy_state = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    store.save_state(parsed_team_id, parsed_analysis_id, legacy_state)
    (legacy_directory / "normalized-core.json").unlink()
    tampered_source = store.load_document(
        parsed_team_id,
        parsed_analysis_id,
        "smartperfetto-report.json",
    )
    assert tampered_source is not None
    startup_envelope = tampered_source["dataEnvelopes"][0]
    startup_envelope["evidence"][0]["fields"]["duration_ms"] = 123.4
    startup_envelope["columns"][0]["value"] = 123.4
    analysis = second_app.state.local_runtime.analyses[(parsed_team_id, parsed_analysis_id)]
    tampered = _prepare_local_report(
        analysis,
        EngineResult(
            contract="workspace-agent-v1",
            state="completed",
            payload={
                "reportId": tampered_source["reportId"],
                "report": tampered_source,
            },
        ),
    )
    store.save_document(
        parsed_team_id,
        parsed_analysis_id,
        "smartperfetto-report.json",
        tampered.source_report,
    )
    store.save_document(
        parsed_team_id,
        parsed_analysis_id,
        "projection.json",
        tampered.projection.document,
    )
    old_report_bytes = (legacy_directory / "report.json").read_bytes()
    old_report = rerun_report.json()
    tamper_provider = _InvalidReportProvider()
    tamper_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    third_app = create_local_app(
        gateway=tamper_gateway,
        synthesizer=LocalReportSynthesizer(provider=tamper_provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(third_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 3
        for _ in range(100):
            tampered_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if tampered_state["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)
        preserved = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert tampered_state["failure"] == {
        "code": "ai_source_evidence_invalid",
        "message": "PerfPilot AI 最终报告无法从已保存的分析证据生成",
        "retryable": False,
    }
    assert preserved.json() == old_report
    assert (legacy_directory / "report.json").read_bytes() == old_report_bytes
    assert tamper_provider.calls == 0
    assert tamper_gateway.submissions == []
    assert tamper_gateway.status_calls == 0
    assert tamper_gateway.fetch_calls == 0


@pytest.mark.parametrize(
    ("core_mutation", "expected_failure_code"),
    [
        ("missing", "ai_source_evidence_unavailable"),
        ("corrupt", "ai_source_evidence_invalid"),
        ("consistent_tamper", "ai_source_evidence_invalid"),
    ],
)
def test_local_app_rejects_invalid_versioned_core_and_keeps_the_old_report(
    tmp_path: Path,
    core_mutation: str,
    expected_failure_code: str,
) -> None:
    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )
        for _ in range(100):
            first_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if first_state["report_available"]:
                break
            time.sleep(0.01)
        old_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    store = LocalAnalysisStore(tmp_path)
    parsed_team_id = UUID(team_id)
    parsed_analysis_id = UUID(analysis_id)
    state = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    assert state["evidence_format_version"] == "normalized-core-v1"
    original_core = store.load_document(parsed_team_id, parsed_analysis_id, "normalized-core.json")
    assert original_core is not None
    analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    old_report_bytes = (analysis_directory / "report.json").read_bytes()
    if core_mutation == "missing":
        (analysis_directory / "normalized-core.json").unlink()
    elif core_mutation == "corrupt":
        store.save_document(
            parsed_team_id,
            parsed_analysis_id,
            "normalized-core.json",
            {"schema_version": "invalid"},
        )
    else:
        scenario = original_core["scenario_reports"][0]
        metric = scenario["metrics"][0]
        metric["numeric_value"] = float(metric["numeric_value"]) + 1.0
        core_bytes = canonical_json_bytes(original_core)
        tampered_projection = build_ai_projection(
            NormalizedTraceReport(
                canonical_bytes=core_bytes,
                sha256_b64=base64.b64encode(
                    hashlib.sha256(core_bytes).digest()
                ).decode("ascii"),
            ),
            analysis_profile=state["profile"],
            question=state["question"],
        )
        store.save_document(
            parsed_team_id,
            parsed_analysis_id,
            "normalized-core.json",
            original_core,
        )
        store.save_document(
            parsed_team_id,
            parsed_analysis_id,
            "projection.json",
            tampered_projection.document,
        )
    provider = _InvalidReportProvider()
    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    second_app = create_local_app(
        gateway=restarted_gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        for _ in range(100):
            failed_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if failed_state["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        preserved_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert failed_state["state"] == "partially_completed"
    assert failed_state["failure"] == {
        "code": expected_failure_code,
        "message": "PerfPilot AI 最终报告无法从已保存的分析证据生成",
        "retryable": False,
    }
    assert failed_state["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "failed", "attempts": 0}
    ]
    assert [item["state"] for item in failed_state["stages"]] == [
        "completed",
        "completed",
        "failed",
        "completed",
    ]
    assert preserved_report.status_code == 200
    assert preserved_report.json() == old_report
    assert (analysis_directory / "report.json").read_bytes() == old_report_bytes
    assert provider.calls == 0
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0


@pytest.mark.parametrize(
    "failed_document",
    ["smartperfetto-report.json", "projection.json"],
)
def test_local_app_does_not_publish_a_manifest_for_partial_evidence_writes(
    tmp_path: Path,
    failed_document: str,
) -> None:
    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=_recovery_request(),
        )
        assert recovered.status_code == 201
        analysis_id = recovered.json()["analysis_id"]
        for _ in range(100):
            initial_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if initial_state["report_available"]:
                break
            time.sleep(0.01)
        old_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    store = LocalAnalysisStore(tmp_path)
    parsed_team_id = UUID(team_id)
    parsed_analysis_id = UUID(analysis_id)
    legacy_state = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    store.save_state(parsed_team_id, parsed_analysis_id, legacy_state)
    analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    (analysis_directory / "normalized-core.json").unlink()
    old_report_bytes = (analysis_directory / "report.json").read_bytes()
    provider = _InvalidReportProvider()
    gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    second_app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        runtime = second_app.state.local_runtime
        original_save_document = runtime.store.save_document

        def fail_selected_document(
            stored_team_id: UUID,
            stored_analysis_id: UUID,
            name: str,
            value: dict[str, object],
        ) -> None:
            if name == failed_document:
                raise RuntimeError("evidence document write failed")
            original_save_document(stored_team_id, stored_analysis_id, name, value)

        runtime.store.save_document = fail_selected_document
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        for _ in range(100):
            failed_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if failed_state["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)
        preserved_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    persisted = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    assert "evidence_format_version" not in persisted
    assert "evidence_manifest" not in persisted
    assert preserved_report.json() == old_report
    assert (analysis_directory / "report.json").read_bytes() == old_report_bytes
    assert provider.calls == 0
    assert gateway.submissions == []
    assert gateway.status_calls == 0
    assert gateway.fetch_calls == 0


def test_local_app_installs_recovery_task_before_persisting_initial_state(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    provider.release_reruns.set()
    gateway = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        runtime = app.state.local_runtime
        original_persist = runtime._persist
        recovery_persist_entered = threading.Event()
        release_recovery_persist = threading.Event()
        blocked_recovery = False

        async def persist_with_recovery_barrier(analysis) -> None:
            nonlocal blocked_recovery
            if (
                not blocked_recovery
                and analysis.source_run is not None
                and analysis.generation == 1
                and analysis.report is None
            ):
                blocked_recovery = True
                recovery_persist_entered.set()
                while not release_recovery_persist.is_set():
                    await asyncio.sleep(0.001)
            await original_persist(analysis)

        runtime._persist = persist_with_recovery_barrier
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovery_path = f"/v1/teams/{team_id}/local-recoveries"
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                client.post,
                recovery_path,
                headers=headers,
                json=_recovery_request(),
            )
            entered = recovery_persist_entered.wait(timeout=2)
            if not entered:
                release_recovery_persist.set()
                first_future.result(timeout=5)
            assert entered
            assert provider.calls == 0
            try:
                duplicate = client.post(
                    recovery_path,
                    headers=headers,
                    json=_recovery_request(),
                )
                assert duplicate.status_code == 201
                analysis_id = duplicate.json()["analysis_id"]
                rerun = client.post(
                    f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
                    headers=headers,
                )
            finally:
                release_recovery_persist.set()
            first = first_future.result(timeout=5)

        assert first.status_code == 201
        assert first.json()["analysis_id"] == analysis_id
        assert rerun.status_code == 409
        assert rerun.json() == {"detail": "analysis is already running"}
        terminal = first.json()
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["report_available"]:
                break
            time.sleep(0.01)
        report = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}/report")

    assert provider.calls == 1
    assert report.status_code == 200
    assert report.json()["report_version"] == 1
    persisted = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))]
    assert persisted["generation"] == 1
    assert gateway.submissions == []


def test_local_app_removes_recovery_when_initial_persistence_fails(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_live_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        runtime = app.state.local_runtime

        async def fail_recovery_persist(_analysis) -> None:
            raise RuntimeError("recovery persistence failed")

        runtime._persist = fail_recovery_persist
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        with pytest.raises(RuntimeError, match="^recovery persistence failed$"):
            client.post(
                f"/v1/teams/{team_id}/local-recoveries",
                headers=headers,
                json=_recovery_request(),
            )

        assert runtime.analyses == {}
        assert runtime.tasks == set()
        assert provider.calls == 0

    assert LocalAnalysisStore(tmp_path).load_states() == {}


def test_local_app_retains_committed_recovery_after_durability_error(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        poll_interval_seconds=0.001,
    )
    with TestClient(app) as client:
        runtime = app.state.local_runtime
        original_persist = runtime._persist
        injected = False

        async def persist_then_report_uncertain(analysis) -> None:
            nonlocal injected
            await original_persist(analysis)
            if not injected:
                injected = True
                raise LocalAnalysisStoreDurabilityError(
                    "local analysis durability uncertain"
                )

        runtime._persist = persist_then_report_uncertain
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        with pytest.raises(LocalAnalysisStoreDurabilityError):
            client.post(
                f"/v1/teams/{team_id}/local-recoveries",
                headers=headers,
                json=_recovery_request(),
            )
        runtime._persist = original_persist
        assert len(runtime.analyses) == 1
        (_, analysis_id), analysis = next(iter(runtime.analyses.items()))
        persisted = LocalAnalysisStore(tmp_path).load_states()[
            (UUID(team_id), analysis_id)
        ]
        assert analysis.generation == persisted["generation"] == 1
        assert analysis.task is not None


def test_local_app_retains_committed_rerun_after_durability_error(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        poll_interval_seconds=0.001,
    )
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=_recovery_request(),
        )
        analysis_id = recovered.json()["analysis_id"]
        for _ in range(100):
            current = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if current["report_available"]:
                break
            time.sleep(0.01)
        runtime = app.state.local_runtime
        original_persist = runtime._persist
        injected = False

        async def persist_then_report_uncertain(analysis) -> None:
            nonlocal injected
            await original_persist(analysis)
            if not injected and analysis.generation == 2:
                injected = True
                raise LocalAnalysisStoreDurabilityError(
                    "local analysis durability uncertain"
                )

        runtime._persist = persist_then_report_uncertain
        with pytest.raises(LocalAnalysisStoreDurabilityError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
                headers=headers,
            )
        runtime._persist = original_persist
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        persisted = LocalAnalysisStore(tmp_path).load_states()[
            (UUID(team_id), UUID(analysis_id))
        ]
        assert analysis.generation == persisted["generation"] == 2
        assert analysis.task is not None


def test_local_app_serializes_simultaneous_ai_rerun_reservations(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    gateway = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    checksum = base64.b64encode(hashlib.sha256(b"recovered-trace").digest()).decode(
        "ascii"
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json={
                "schema_version": "1.0",
                "session_id": "session-local-1",
                "run_id": "run-local-1",
                "analysis_profile": "auto",
                "question": "给出最终优化方案",
                "trace_size": len(b"recovered-trace"),
                "trace_sha256_b64": checksum,
            },
        )
        assert recovered.status_code == 201
        analysis_id = recovered.json()["analysis_id"]
        initial = recovered.json()
        for _ in range(100):
            initial = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if initial["report_available"] and initial["state"] in {
                "completed",
                "partially_completed",
            }:
                break
            time.sleep(0.01)
        runtime = app.state.local_runtime
        parsed_analysis_id = UUID(analysis_id)
        for _ in range(100):
            initial_task = runtime.analyses[(UUID(team_id), parsed_analysis_id)].task
            if initial_task is not None and initial_task.done():
                break
            time.sleep(0.01)
        assert initial["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        assert provider.calls == 1
        initial_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )
        assert initial_report.status_code == 200
        assert initial_report.json()["report_version"] == 1

        original_persist = runtime._persist
        first_reservation_entered = threading.Event()
        second_reservation_entered = threading.Event()
        release_reservations = threading.Event()
        reservation_persists = 0

        async def persist_with_reservation_barrier(analysis) -> None:
            nonlocal reservation_persists
            is_reservation = (
                analysis.analysis_id == parsed_analysis_id
                and analysis.generation >= 2
                and len(analysis.ai_rounds) == 1
                and analysis.ai_rounds[0].state == "pending"
                and analysis.stages["report"] == "pending"
            )
            if is_reservation:
                reservation_persists += 1
                if reservation_persists == 1:
                    first_reservation_entered.set()
                else:
                    second_reservation_entered.set()
                while not release_reservations.is_set():
                    await asyncio.sleep(0.001)
            await original_persist(analysis)

        runtime._persist = persist_with_reservation_barrier
        rerun_path = f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs"
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                client.post,
                rerun_path,
                headers=headers,
            )
            first_entered = first_reservation_entered.wait(timeout=2)
            if not first_entered:
                release_reservations.set()
                first_future.result(timeout=5)
            assert first_entered
            assert not provider.first_rerun_started.is_set()
            second_future = executor.submit(
                client.post,
                rerun_path,
                headers=headers,
            )
            deadline = time.monotonic() + 2
            while (
                not second_future.done()
                and not second_reservation_entered.is_set()
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            second_observed = (
                second_future.done() or second_reservation_entered.is_set()
            )
            release_reservations.set()
            first_rerun = first_future.result(timeout=5)
            second_rerun = second_future.result(timeout=5)
        assert second_observed

        responses = (first_rerun, second_rerun)
        try:
            assert sorted(response.status_code for response in responses) == [201, 409]
            accepted = next(response for response in responses if response.status_code == 201)
            rejected = next(response for response in responses if response.status_code == 409)
            assert accepted.json()["generation"] == 2
            assert rejected.json() == {"detail": "analysis is already running"}
            assert provider.first_rerun_started.wait(timeout=2)
            assert provider.rerun_calls == 1
            assert not provider.second_rerun_started.is_set()
        finally:
            provider.release_reruns.set()

        terminal = initial
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if (
                terminal["stages"][2]["state"] == "completed"
                and terminal["stages"][3]["state"] == "completed"
                and terminal["ai_rounds"]
                == [
                    {
                        "round": 1,
                        "role": "report",
                        "state": "completed",
                        "attempts": 1,
                    }
                ]
            ):
                break
            time.sleep(0.01)
        final_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert terminal["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]
    assert provider.calls == 2
    assert provider.rerun_calls == 1
    assert final_report.status_code == 200
    assert final_report.json()["report_version"] == 2
    persisted = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))]
    assert persisted["generation"] == 2
    assert persisted["ai_rounds"] == terminal["ai_rounds"]
    assert gateway.submissions == []


def test_local_app_rolls_back_a_failed_rerun_persistence_reservation(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    gateway = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=_recovery_request(),
        )
        assert recovered.status_code == 201
        analysis_id = recovered.json()["analysis_id"]
        parsed_analysis_id = UUID(analysis_id)
        before = recovered.json()
        for _ in range(100):
            before = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if before["report_available"]:
                break
            time.sleep(0.01)
        runtime = app.state.local_runtime
        for _ in range(100):
            initial_task = runtime.analyses[(UUID(team_id), parsed_analysis_id)].task
            if initial_task is not None and initial_task.done():
                break
            time.sleep(0.01)
        persisted_before = copy.deepcopy(
            LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), parsed_analysis_id)]
        )
        original_persist = runtime._persist
        fail_reservation = True

        async def fail_first_rerun_persist(analysis) -> None:
            nonlocal fail_reservation
            if (
                fail_reservation
                and analysis.analysis_id == parsed_analysis_id
                and analysis.generation == 2
                and analysis.ai_rounds[0].state == "pending"
            ):
                fail_reservation = False
                raise RuntimeError("rerun persistence failed")
            await original_persist(analysis)

        runtime._persist = fail_first_rerun_persist
        rerun_path = f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs"
        with pytest.raises(RuntimeError, match="^rerun persistence failed$"):
            client.post(rerun_path, headers=headers)

        current = runtime.analyses[(UUID(team_id), parsed_analysis_id)]
        assert current.task is None
        assert current.generation == persisted_before["generation"]
        assert current.state == persisted_before["state"]
        assert current.stages == persisted_before["stages"]
        assert current.runtime_status == persisted_before["runtime_status"]
        assert current.version == persisted_before["version"]
        assert provider.calls == 1
        assert provider.rerun_calls == 0
        assert not provider.first_rerun_started.is_set()
        restored = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}"
        ).json()
        assert restored["state"] == before["state"]
        assert restored["version"] == before["version"]
        assert restored["stages"] == before["stages"]
        assert restored["ai_rounds"] == before["ai_rounds"]
        assert LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), parsed_analysis_id)] == (
            persisted_before
        )

        successful = client.post(rerun_path, headers=headers)
        assert successful.status_code == 201
        assert successful.json()["generation"] == 2
        try:
            assert provider.first_rerun_started.wait(timeout=2)
            assert provider.rerun_calls == 1
        finally:
            provider.release_reruns.set()
        terminal = restored
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if (
                terminal["stages"][2]["state"] == "completed"
                and terminal["stages"][3]["state"] == "completed"
            ):
                break
            time.sleep(0.01)
        report = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}/report")

    assert terminal["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]
    assert provider.calls == 2
    assert report.status_code == 200
    assert report.json()["report_version"] == 2
    assert gateway.submissions == []


def test_local_recovery_imports_a_completed_smartperfetto_session_once(
    tmp_path: Path,
) -> None:
    result = _live_smartperfetto_result()
    report = result.payload["report"]
    assert isinstance(report, dict)
    summary = report["summary"]
    assert isinstance(summary, dict)
    summary["rounds"] = 53
    report["claimVerificationResult"] = {"status": "passed"}
    gateway = _FakeSmartPerfettoGateway(result)
    app = create_local_app(
        gateway=gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    checksum = base64.b64encode(hashlib.sha256(b"recovered-trace").digest()).decode(
        "ascii"
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        payload = {
            "schema_version": "1.0",
            "session_id": "session-local-1",
            "run_id": "run-local-1",
            "analysis_profile": "auto",
            "question": "给出最终优化方案",
            "trace_size": len(b"recovered-trace"),
            "trace_sha256_b64": checksum,
        }
        first = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=payload,
        )
        second = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=payload,
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["analysis_id"] == first.json()["analysis_id"]
        analysis_id = first.json()["analysis_id"]
        terminal = first.json()
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["report_available"]:
                break
            time.sleep(0.01)

        assert terminal["source_analysis"] == {
            "engine": "smartperfetto",
            "rounds": 53,
            "verification": "passed",
            "session_id": "session-local-1",
            "run_id": "run-local-1",
        }
        assert terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        assert gateway.submissions == []

        completed_version = terminal["version"]
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 2
        for _ in range(100):
            rerun_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if (
                rerun_state["version"] > completed_version
                and rerun_state["stages"][2]["state"] == "completed"
                and rerun_state["stages"][3]["state"] == "completed"
            ):
                break
            time.sleep(0.01)
        assert rerun_state["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )
        assert rerun_report.status_code == 200
        assert rerun_report.json()["report_version"] == 2
        assert gateway.submissions == []
