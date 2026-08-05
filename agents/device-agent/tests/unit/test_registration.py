from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import AGENT_ID, TASK_KID
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import (
    ControlClient,
    ControlClientError,
    RegistrationResponse,
    TaskSigningKeyResponse,
)
from perfpilot_agent.credentials import CredentialStore, InMemoryCredentialBackend
from perfpilot_agent.platform.base import PlatformMetadata
from perfpilot_agent.registration import RegistrationAlreadyExists, RegistrationService


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


class FakeClient:
    def __init__(self, response: RegistrationResponse) -> None:
        self.response = response
        self.requests = []

    async def register(self, request):
        self.requests.append(request)
        return self.response


def registration_response(signing_key: Ed25519PrivateKey) -> RegistrationResponse:
    now = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    return RegistrationResponse(
        schema_version="1.0",
        agent_id=AGENT_ID,
        access_token="ppat_" + "A" * 43,
        access_token_expires_at=now + timedelta(minutes=15),
        refresh_token="pprt_" + "B" * 43,
        refresh_token_expires_at=now + timedelta(days=30),
        task_signing_key=TaskSigningKeyResponse(
            kid=TASK_KID,
            public_key_b64=public_key_b64(signing_key),
        ),
        heartbeat_interval_seconds=10,
    )


@pytest.mark.asyncio
async def test_registration_generates_key_saves_credentials_and_zeroes_code(
    signing_key: Ed25519PrivateKey,
) -> None:
    store = CredentialStore(InMemoryCredentialBackend())
    client = FakeClient(registration_response(signing_key))
    registration_key = Ed25519PrivateKey.generate()
    code = bytearray(("ppreg_" + "C" * 43).encode("ascii"))
    service = RegistrationService(
        store=store,
        client=client,
        metadata=PlatformMetadata(
            platform="linux",
            hostname="rivotek",
            os_version="Ubuntu 24.04",
        ),
        private_key_factory=lambda: registration_key,
    )

    saved = await service.register(code)

    assert code == bytearray(len(code))
    assert len(client.requests) == 1
    assert client.requests[0].registration_code.startswith("ppreg_")
    assert client.requests[0].public_key_b64 == public_key_b64(registration_key)
    assert store.load() == saved
    assert saved.agent_id == AGENT_ID


@pytest.mark.asyncio
async def test_registration_refuses_existing_credentials_without_replace(
    signing_key: Ed25519PrivateKey,
) -> None:
    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend)
    first_client = FakeClient(registration_response(signing_key))
    first = RegistrationService(
        store=store,
        client=first_client,
        metadata=PlatformMetadata(
            platform="macos",
            hostname="ray-mac",
            os_version="macOS 15",
        ),
    )
    await first.register(bytearray(("ppreg_" + "C" * 43).encode("ascii")))
    second_client = FakeClient(registration_response(signing_key))
    second = RegistrationService(
        store=store,
        client=second_client,
        metadata=first.metadata,
    )
    code = bytearray(("ppreg_" + "D" * 43).encode("ascii"))

    with pytest.raises(RegistrationAlreadyExists):
        await second.register(code)

    assert code == bytearray(len(code))
    assert second_client.requests == []


@pytest.mark.asyncio
async def test_control_client_rejects_open_registration_response(tmp_path) -> None:
    ca_bundle = tmp_path / "ca.crt"
    ca_bundle.write_text("unused-by-mock", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        schema_version="1.0",
        server_url="https://10.166.0.125",
        ca_bundle=ca_bundle,
        workspace_root=workspace,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            request=request,
            json={
                "schema_version": "1.0",
                "agent_id": str(AGENT_ID),
                "access_token": "ppat_" + "A" * 43,
                "access_token_expires_at": "2026-08-05T08:15:00Z",
                "refresh_token": "pprt_" + "B" * 43,
                "refresh_token_expires_at": "2026-09-05T08:00:00Z",
                "task_signing_key": {
                    "kid": TASK_KID,
                    "public_key_b64": "A" * 43 + "=",
                },
                "heartbeat_interval_seconds": 10,
                "unexpected": "rejected",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ControlClient(config, http_client=http_client)
        with pytest.raises(ControlClientError):
            await client.register(
                {
                    "schema_version": "1.0",
                    "registration_code": "ppreg_" + "C" * 43,
                    "public_key_b64": "A" * 43 + "=",
                    "platform": "linux",
                    "agent_version": "0.1.0",
                    "hostname": "rivotek",
                    "os_version": "Ubuntu 24.04",
                }
            )
