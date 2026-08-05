from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.services.agent_uploads import AgentUploadPartSlot, AgentUploadSlot

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("76000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("77000000-0000-4000-8000-000000000001")
TOKEN = "ppat_" + "A" * 43
CHECKSUM = base64.b64encode(b"a" * 32).decode("ascii")


class FakeAgentService:
    async def authenticate_access(self, token: str) -> SimpleNamespace:
        assert token == TOKEN
        return SimpleNamespace(agent_id=AGENT_ID)


class FakeUploadService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def create_upload(self, **kwargs: object) -> AgentUploadSlot:
        self.calls.append(("create", kwargs))
        return AgentUploadSlot(
            artifact_id=ARTIFACT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind="startup_trace",
            mime="application/x-perfetto-trace",
            size=4,
            sha256_b64=CHECKSUM,
            part_size_bytes=64 * 1024 * 1024,
            part_count=1,
            state="pending",
            expires_at=NOW,
            finalized_at=None,
        )

    async def authorize_part(self, **kwargs: object) -> AgentUploadPartSlot:
        self.calls.append(("part", kwargs))
        return AgentUploadPartSlot(
            upload_id=UPLOAD_ID,
            part_number=1,
            url="https://objects.example/signed-part",
            required_headers={},
            expires_at=NOW,
        )

    async def complete_upload(self, **kwargs: object) -> AgentUploadSlot:
        self.calls.append(("complete", kwargs))
        return AgentUploadSlot(
            artifact_id=ARTIFACT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind="startup_trace",
            mime="application/x-perfetto-trace",
            size=4,
            sha256_b64=CHECKSUM,
            part_size_bytes=64 * 1024 * 1024,
            part_count=1,
            state="finalized",
            expires_at=NOW,
            finalized_at=NOW,
        )


def _client(service: FakeUploadService) -> TestClient:
    settings = Settings(
        app_env="test",
        allowed_origins=("https://console.example.com",),
        _env_prefix="PERFPILOT_TEST_AGENT_UPLOAD_ISOLATED_",
        _env_file=None,
        _secrets_dir=None,
    )
    return TestClient(
        create_app(
            testing=True,
            settings_override=settings,
            agent_service=FakeAgentService(),  # type: ignore[arg-type]
            agent_upload_service=service,  # type: ignore[arg-type]
        )
    )


def test_agent_creates_part_and_completes_a_closed_multipart_upload() -> None:
    service = FakeUploadService()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with _client(service) as client:
        created = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/uploads",
            headers=headers,
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "artifact_kind": "startup_trace",
                "mime": "application/x-perfetto-trace",
                "size": 4,
                "sha256_b64": CHECKSUM,
            },
        )
        part = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/uploads/{UPLOAD_ID}/parts",
            headers=headers,
            json={"schema_version": "1.0", "lease_version": 1, "part_number": 1},
        )
        completed = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/uploads/{UPLOAD_ID}/complete",
            headers=headers,
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "parts": [{"part_number": 1, "etag": '"etag-1"'}],
            },
        )

    assert created.status_code == 201
    assert created.json() == {
        "schema_version": "1.0",
        "artifact_id": str(ARTIFACT_ID),
        "upload_id": str(UPLOAD_ID),
        "artifact_kind": "startup_trace",
        "mime": "application/x-perfetto-trace",
        "size": 4,
        "sha256_b64": CHECKSUM,
        "part_size_bytes": 64 * 1024 * 1024,
        "part_count": 1,
        "state": "pending",
        "expires_at": NOW.isoformat(),
        "finalized_at": None,
    }
    assert part.status_code == 200
    assert part.json() == {
        "schema_version": "1.0",
        "upload_id": str(UPLOAD_ID),
        "part_number": 1,
        "put_url": "https://objects.example/signed-part",
        "required_headers": {},
        "expires_at": NOW.isoformat(),
    }
    assert completed.status_code == 200
    assert completed.json()["state"] == "finalized"
    assert service.calls[2][0] == "complete"
    assert service.calls[2][1]["agent_id"] == AGENT_ID
    assert service.calls[2][1]["parts"][0].etag == '"etag-1"'


def test_agent_upload_contract_rejects_extra_fields() -> None:
    service = FakeUploadService()
    with _client(service) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/uploads",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "artifact_kind": "startup_trace",
                "mime": "application/x-perfetto-trace",
                "size": 4,
                "sha256_b64": CHECKSUM,
                "object_key": "caller-must-not-control-this",
            },
        )

    assert response.status_code == 422
    assert service.calls == []
