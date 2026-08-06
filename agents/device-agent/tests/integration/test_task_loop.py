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
    TaskExecuteResponse,
    TaskWaitResponse,
)
from perfpilot_agent.credentials import (
    AgentCredentials,
    CredentialStore,
    InMemoryCredentialBackend,
    TaskSigningKey,
)
from perfpilot_agent.service import AgentService, TaskLoop
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding


class FakeExecutor:
    def __init__(self) -> None:
        self.tasks = []

    async def run(self, task) -> None:
        self.tasks.append(task)


class FakePollControl:
    def __init__(self, response, credentials: AgentCredentials) -> None:
        self.response = response
        self.credentials = credentials

    async def poll_task(self, *, wait_seconds: int = 20):
        return self.response

    async def acknowledge_cancellation(self, *, execution_id, lease_version):
        raise AssertionError("unexpected cancellation")


def _credentials(signing_key, task_kid: str, agent_id, now) -> AgentCredentials:
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
        schema_version="1.0",
        agent_id=agent_id,
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
