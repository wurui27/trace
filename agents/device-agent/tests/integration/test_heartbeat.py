from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, FormatChecker

from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import ControlClient
from perfpilot_agent.credentials import AgentCredentials, TaskSigningKey
from perfpilot_agent.devices import DeviceInventoryItem, DeviceObservation, HeartbeatPublisher
from perfpilot_agent.platform.base import PlatformMetadata
from perfpilot_agent.state import AgentRuntimeState

CLIENT_REF = UUID("74000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("72000000-0000-4000-8000-000000000001")
DEVICE_DIGEST = "a" * 64
SERIAL = "R3CN30SECRET"
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
TASK_KID = "task-key-2026-08"


class FakeInventory:
    async def read_all(self) -> tuple[DeviceInventoryItem, ...]:
        return (
            DeviceInventoryItem(
                serial=SERIAL,
                observation=DeviceObservation(
                    client_ref=CLIENT_REF,
                    serial=SERIAL,
                    manufacturer="Google",
                    model="Pixel 7",
                    android_release="14",
                    api_level=34,
                    connection_type="usb",
                    adb_state="device",
                    battery_percent=82,
                    temperature_c=31.5,
                    storage_available_bytes=42_949_672_960,
                    property_error_code=None,
                ),
                transport_id="1",
                abi="arm64-v8a",
                fingerprint="google/panther/demo",
                perfetto_available=True,
            ),
        )


def credentials() -> AgentCredentials:
    key = Ed25519PrivateKey.generate()
    private_raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return AgentCredentials(
        schema_version="1.0",
        agent_id=AGENT_ID,
        private_key_b64=base64.b64encode(private_raw).decode("ascii"),
        access_token="ppat_" + "A" * 43,
        access_token_expires_at=NOW + timedelta(minutes=15),
        refresh_token="pprt_" + "B" * 43,
        refresh_token_expires_at=NOW + timedelta(days=30),
        task_signing_key=TaskSigningKey(
            kid=TASK_KID,
            public_key_b64=base64.b64encode(public_raw).decode("ascii"),
        ),
        heartbeat_interval_seconds=10,
    )


@pytest.mark.asyncio
async def test_heartbeat_publishes_full_snapshot_and_keeps_digest_mapping_only_in_memory(
    tmp_path: Path,
) -> None:
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
    stored = credentials()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "schema_version": "1.0",
                "accepted_at": NOW.isoformat(),
                "next_heartbeat_seconds": 10,
                "devices": [
                    {
                        "client_ref": str(CLIENT_REF),
                        "device_id": str(DEVICE_ID),
                        "device_digest": DEVICE_DIGEST,
                    }
                ],
            },
        )

    state = AgentRuntimeState()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        publisher = HeartbeatPublisher(
            inventory=FakeInventory(),
            control=ControlClient(config, http_client=http_client),
            credentials=stored,
            metadata=PlatformMetadata(
                platform="linux",
                hostname="rivotek",
                os_version="Ubuntu 24.04",
            ),
            state=state,
            workspace_root=workspace,
            clock=lambda: NOW,
            disk_free=lambda _path: 107_374_182_400,
        )

        receipt = await publisher.publish()

    assert captured["authorization"] == f"Bearer {stored.access_token}"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    contract = json.loads(
        (
            Path(__file__).parents[4]
            / "contracts"
            / "v1"
            / "agents"
            / "heartbeat-request.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(contract, format_checker=FormatChecker()).validate(payload)
    assert payload["devices"][0]["serial"] == SERIAL
    assert payload["execution_slot"] == {"state": "idle", "execution_id": None}
    assert receipt.devices[0].device_digest == DEVICE_DIGEST
    assert state.serial_for_digest(DEVICE_DIGEST) == SERIAL
    assert SERIAL not in repr(state)
