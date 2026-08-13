from __future__ import annotations

import base64
import asyncio
import json
from datetime import datetime, timedelta
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import (
    ControlClient,
    ControlClientError,
    DeviceTaskExecuteResponse,
    SourceTaskExecuteResponse,
    TaskExecuteResponse,
    TaskWaitResponse,
)
from perfpilot_agent.credentials import (
    AgentCredentials,
    CredentialStore,
    InMemoryCredentialBackend,
    TaskSigningKey,
)
from perfpilot_agent.service import AgentService, SourceTaskExecutor, TaskLoop
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding


class FakeExecutor:
    def __init__(self) -> None:
        self.tasks = []

    async def run(self, task, **kwargs) -> None:
        self.tasks.append(task)


class FakePollControl:
    def __init__(self, response, credentials: AgentCredentials) -> None:
        self.response = response
        self.credentials = credentials
        self.source_completions = []
        self.state = None

    async def poll_task(self, *, wait_seconds: int = 20):
        return self.response

    async def acknowledge_cancellation(self, *, execution_id, lease_version):
        raise AssertionError("unexpected cancellation")

    async def complete_source_task(self, **kwargs):
        if self.state is not None:
            assert self.state.execution_id == kwargs["execution_id"]
        self.source_completions.append(kwargs)
        return object()


def _credentials(
    signing_key, task_kid: str, agent_id, now, *, team_id: UUID | None = None
) -> AgentCredentials:
    public = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private = signing_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return AgentCredentials(
        schema_version="1.1" if team_id is not None else "1.0",
        agent_id=agent_id,
        team_id=team_id,
        private_key_b64=base64.b64encode(private).decode("ascii"),
        access_token="ppat_" + "A" * 43,
        access_token_expires_at=now + timedelta(minutes=15),
        refresh_token="pprt_" + "B" * 43,
        refresh_token_expires_at=now + timedelta(days=30),
        task_signing_key=TaskSigningKey(
            kid=task_kid,
            public_key_b64=base64.b64encode(public).decode("ascii"),
        ),
        heartbeat_interval_seconds=10,
    )


@pytest.mark.asyncio
async def test_task_loop_verifies_snapshot_before_dispatch(
    signing_key,
    sign_task,
    task_claims,
) -> None:
    parsed_now = datetime.fromisoformat(task_claims["issued_at"])
    credentials = _credentials(
        signing_key,
        "task-key-2026-08",
        UUID(task_claims["agent_id"]),
        parsed_now,
    )
    response = TaskExecuteResponse(
        schema_version="1.0",
        action="execute",
        snapshot_jws=sign_task(task_claims),
        lease_expires_at=task_claims["expires_at"],
        renew_after_seconds=20,
    )
    state = AgentRuntimeState()
    state.replace_device_bindings(
        (
            DeviceBinding(
                client_ref="74000000-0000-4000-8000-000000000001",
                device_id="72000000-0000-4000-8000-000000000001",
                device_digest=task_claims["device_digest"],
                serial="device-under-test",
            ),
        )
    )
    executor = FakeExecutor()
    loop = TaskLoop(
        control=FakePollControl(response, credentials),
        executor=executor,
        state=state,
        clock=lambda: parsed_now,
        sleep=lambda _: _completed_sleep(),
    )

    handled = await loop.poll_once()

    assert handled is True
    assert [task.execution_id for task in executor.tasks] == [UUID(task_claims["execution_id"])]


@pytest.mark.asyncio
async def test_task_loop_dispatches_v11_device_response_from_control_client(
    tmp_path,
    signing_key,
    task_claims,
) -> None:
    now = datetime.fromisoformat(task_claims["issued_at"])
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    claims = {
        **task_claims,
        "schema_version": "1.1",
        "team_id": str(team_id),
        "scenarios": [
            task_claims["scenarios"][0],
            {
                "scenario_type": "scroll",
                "recipe_version": 1,
                "recipe_hash": "c" * 64,
                "duration_seconds": 30,
                "memory_rounds": 0,
                "swipe_count": 3,
            },
        ],
        "allowed_uploads": ["startup_trace", "scroll_trace", "agent_log"],
    }
    canonical = json.dumps(
        claims,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = base64.b64encode(signing_key.sign(canonical)).decode("ascii")
    response_document = {
        "schema_version": "1.1",
        "task_kind": "device",
        "lease_token": "opaque-device-lease-token",
        "snapshot": claims,
        "signature_b64": signature,
    }
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=tmp_path / "work",
    )
    credentials = _credentials(
        signing_key,
        "task-key-2026-08",
        UUID(task_claims["agent_id"]),
        now,
        team_id=team_id,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=response_document)

    state = AgentRuntimeState()
    state.replace_device_bindings(
        (
            DeviceBinding(
                client_ref="74000000-0000-4000-8000-000000000001",
                device_id="72000000-0000-4000-8000-000000000001",
                device_digest=claims["device_digest"],
                serial="device-under-test",
            ),
        )
    )
    capture = FakeExecutor()
    source = FakeExecutor()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        control = ControlClient(config, http_client=http_client, credentials=credentials)
        assert isinstance(await control.poll_task(wait_seconds=0), DeviceTaskExecuteResponse)
        loop = TaskLoop(
            control=control,
            executor=capture,
            source_executor=source,
            state=state,
            clock=lambda: now,
            sleep=lambda _: _completed_sleep(),
        )

        handled = await loop.poll_once()

    assert handled is True
    assert [task.execution_id for task in capture.tasks] == [UUID(claims["execution_id"])]
    assert source.tasks == []


@pytest.mark.asyncio
async def test_task_loop_dispatches_source_context_without_capture_executor(
    signing_key,
) -> None:
    now = datetime.fromisoformat("2026-08-05T08:00:00+00:00")
    agent_id = UUID("71000000-0000-4000-8000-000000000001")
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    credentials = _credentials(
        signing_key, "task-key-2026-08", agent_id, now, team_id=team_id
    )
    snapshot = {
        "schema_version": "1.0",
        "aud": "perfpilot-agent",
        "task_type": "source_context",
        "execution_id": "73000000-0000-4000-8000-000000000001",
        "analysis_id": "30000000-0000-4000-8000-000000000001",
        "team_id": str(team_id),
        "agent_id": str(agent_id),
        "workspace_id": "91000000-0000-4000-8000-000000000001",
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
        "lease_version": 1,
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "finding_hints": [],
        "limits": {"max_findings": 3, "max_files": 12, "max_bytes": 98_304},
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    response = SourceTaskExecuteResponse(
        schema_version="1.1",
        task_kind="source",
        lease_token="opaque-lease-token-123",
        snapshot=snapshot,
        signature_b64=base64.b64encode(signing_key.sign(canonical)).decode("ascii"),
    )
    capture = FakeExecutor()
    state = AgentRuntimeState()
    control = FakePollControl(response, credentials)
    control.state = state
    loop = TaskLoop(
        control=control,
        executor=capture,
        source_executor=SourceTaskExecutor(control=control),
        state=state,
        clock=lambda: now,
        sleep=lambda _: _completed_sleep(),
    )

    handled = await loop.poll_once()

    assert handled is True
    assert capture.tasks == []
    assert control.source_completions[0]["completion"]["result"] == {
        "failure_code": "source_runner_unavailable",
        "retryable": False,
    }
    assert state.execution_id is None


async def _completed_sleep() -> None:
    return None


@pytest.mark.asyncio
async def test_control_client_retries_only_retryable_statuses(tmp_path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request, json={"error": "bounded"})
        return httpx.Response(
            200,
            request=request,
            json={
                "schema_version": "1.0",
                "action": "wait",
                "retry_after_seconds": 1,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(
            config,
            http_client=http_client,
            sleep=lambda _: _completed_sleep(),
            jitter=lambda: 0,
        )
        response = await client.poll_task(
            wait_seconds=0,
            access_token="ppat_" + "A" * 43,
        )

    assert response == TaskWaitResponse(
        schema_version="1.0",
        action="wait",
        retry_after_seconds=1,
    )
    assert attempts == 2


@pytest.mark.asyncio
async def test_control_client_sends_source_lease_token_only_as_fence_header(
    tmp_path,
    signing_key,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )
    now = datetime.fromisoformat("2026-08-05T08:00:00+00:00")
    credentials = _credentials(
        signing_key,
        "task-key-2026-08",
        UUID("71000000-0000-4000-8000-000000000001"),
        now,
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
    )
    execution_id = UUID("73000000-0000-4000-8000-000000000001")
    lease_token = "opaque-source-lease-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-perfpilot-lease-token"] == lease_token
        assert lease_token.encode("ascii") not in request.content
        document = json.loads(request.content)
        signature = base64.b64decode(document.pop("signature_b64"), validate=True)
        signing_key.public_key().verify(
            signature,
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        assert document["agent_id"] == str(credentials.agent_id)
        assert document["team_id"] == str(credentials.team_id)
        return httpx.Response(
            200,
            request=request,
            json={
                "schema_version": "1.1",
                "execution_id": str(execution_id),
                "analysis_id": "30000000-0000-4000-8000-000000000001",
                "lease_version": 1,
                "state": "failed",
                "artifact_id": "40000000-0000-4000-8000-000000000001",
                "checksum": "a" * 64,
                "accepted_at": now.isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(config, http_client=http_client, credentials=credentials)
        response = await client.complete_source_task(
            execution_id=execution_id,
            lease_version=1,
            lease_token=lease_token,
            completion={
                "schema_version": "1.0",
                "task_type": "source_context",
                "execution_id": str(execution_id),
                "analysis_id": "30000000-0000-4000-8000-000000000001",
                "workspace_id": "91000000-0000-4000-8000-000000000001",
                "lease_version": 1,
                "state": "failed",
                "result": {"failure_code": "source_unavailable", "retryable": False},
            },
        )

    assert response.state == "failed"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(config, http_client=http_client, credentials=credentials)
        with pytest.raises(ControlClientError):
            await client.complete_source_task(
                execution_id=execution_id,
                lease_version=1,
                lease_token=lease_token,
                completion={
                    "schema_version": "1.0",
                    "task_type": "source_context",
                    "execution_id": str(execution_id),
                    "analysis_id": "30000000-0000-4000-8000-000000000001",
                    "workspace_id": "91000000-0000-4000-8000-000000000001",
                    "lease_version": 1,
                    "state": "failed",
                    "result": {
                        "failure_code": "source_unavailable",
                        "retryable": False,
                    },
                    "signature_b64": "A" * 86 + "==",
                },
            )


@pytest.mark.asyncio
async def test_control_client_safely_rejects_unknown_source_task_type(
    tmp_path,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "schema_version": "1.1",
                "task_kind": "source",
                "lease_token": "opaque-source-lease-token",
                "snapshot": {"schema_version": "1.0", "task_type": "device_capture"},
                "signature_b64": "A" * 86 + "==",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(config, http_client=http_client)
        with pytest.raises(ControlClientError):
            await client.poll_task(wait_seconds=0, access_token="ppat_" + "A" * 43)


@pytest.mark.asyncio
async def test_control_client_refreshes_access_token_and_persists_rotation(
    tmp_path,
    signing_key,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )
    now = datetime.fromisoformat("2026-08-05T08:00:00+00:00")
    credentials = _credentials(
        signing_key,
        "task-key-2026-08",
        UUID("71000000-0000-4000-8000-000000000001"),
        now,
    )
    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend)
    store.save(credentials)
    new_access = "ppat_" + "C" * 43
    new_refresh = "pprt_" + "D" * 43
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agent/token/refresh":
            payload = json.loads(request.content)
            message = f"{payload['agent_id']}\n{payload['nonce']}\n{payload['timestamp']}".encode(
                "ascii"
            )
            signing_key.public_key().verify(
                base64.b64decode(payload["signature_b64"], validate=True),
                message,
            )
            return httpx.Response(
                200,
                request=request,
                json={
                    "schema_version": "1.0",
                    "agent_id": str(credentials.agent_id),
                    "access_token": new_access,
                    "access_token_expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "refresh_token": new_refresh,
                    "refresh_token_expires_at": (now + timedelta(days=30)).isoformat(),
                    "task_signing_key": credentials.task_signing_key.model_dump(),
                    "heartbeat_interval_seconds": 10,
                },
            )
        authorization = request.headers.get("authorization", "")
        authorizations.append(authorization)
        if authorization != f"Bearer {new_access}":
            return httpx.Response(
                401,
                request=request,
                json={
                    "schema_version": "1.0",
                    "error": {
                        "code": "agent_authentication_failed",
                        "message": "Agent authentication failed",
                        "retryable": False,
                        "request_id": "req-refresh-test",
                    },
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "schema_version": "1.0",
                "action": "wait",
                "retry_after_seconds": 1,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(
            config,
            http_client=http_client,
            credential_store=store,
            clock=lambda: now,
            nonce_factory=lambda: "cmVmcmVzaC1hZ2VudC10ZXN0LW5vbmNl",
        )
        response = await client.poll_task(wait_seconds=0)

    assert isinstance(response, TaskWaitResponse)
    assert authorizations == [
        f"Bearer {credentials.access_token}",
        f"Bearer {new_access}",
    ]
    assert store.load().access_token == new_access
    assert store.load().refresh_token == new_refresh


@pytest.mark.asyncio
async def test_control_client_does_not_retry_non_retryable_status(tmp_path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            409,
            request=request,
            json={
                "schema_version": "1.0",
                "error": {
                    "code": "conflict",
                    "message": "Conflict",
                    "retryable": True,
                    "request_id": "req-conflict-test",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(
            config,
            http_client=http_client,
            sleep=lambda _: _completed_sleep(),
            jitter=lambda: 0,
        )
        with pytest.raises(ControlClientError):
            await client.poll_task(
                wait_seconds=0,
                access_token="ppat_" + "A" * 43,
            )

    assert attempts == 1


@pytest.mark.asyncio
async def test_agent_service_runs_heartbeat_task_and_refresh_loops_together() -> None:
    stop = asyncio.Event()
    called: set[str] = set()

    def mark(name: str) -> None:
        called.add(name)
        if called == {"heartbeat", "task", "refresh"}:
            stop.set()

    class Heartbeat:
        async def publish(self):
            mark("heartbeat")
            return type("HeartbeatReceipt", (), {"next_heartbeat_seconds": 10})()

    class Tasks:
        async def poll_once(self):
            mark("task")
            await asyncio.sleep(0)

    class Credentials:
        async def refresh_credentials(self, *, force: bool = False):
            mark("refresh")
            await asyncio.sleep(0)

    service = AgentService(
        heartbeat=Heartbeat(),
        tasks=Tasks(),
        credentials=Credentials(),
        stop_event=stop,
    )

    await asyncio.wait_for(service.run(), timeout=1)

    assert called == {"heartbeat", "task", "refresh"}
