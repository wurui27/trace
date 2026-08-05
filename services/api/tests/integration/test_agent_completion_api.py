from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.services.agent_tasks import AgentExecutionCompletion

NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("76000000-0000-4000-8000-000000000001")
TOKEN = "ppat_" + "A" * 43
CHECKSUM = base64.b64encode(b"a" * 32).decode("ascii")


class FakeAgentService:
    async def authenticate_access(self, token: str) -> SimpleNamespace:
        assert token == TOKEN
        return SimpleNamespace(agent_id=AGENT_ID)


class FakeTaskService:
    def __init__(self) -> None:
        self.received: dict[str, object] | None = None

    async def complete(self, **kwargs: object) -> AgentExecutionCompletion:
        self.received = kwargs
        return AgentExecutionCompletion(
            execution_id=EXECUTION_ID,
            analysis_id=ANALYSIS_ID,
            lease_version=1,
            analysis_state="analyzing",
            accepted_at=NOW,
        )


class FakeArtifactValidator:
    pass


def _client(task_service: FakeTaskService) -> TestClient:
    settings = Settings(
        app_env="test",
        allowed_origins=("https://console.example.com",),
        _env_prefix="PERFPILOT_TEST_AGENT_COMPLETION_ISOLATED_",
        _env_file=None,
        _secrets_dir=None,
    )
    return TestClient(
        create_app(
            testing=True,
            settings_override=settings,
            agent_service=FakeAgentService(),  # type: ignore[arg-type]
            agent_task_service=task_service,  # type: ignore[arg-type]
            agent_upload_service=FakeArtifactValidator(),  # type: ignore[arg-type]
        )
    )


def _payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "execution_id": str(EXECUTION_ID),
        "lease_version": 1,
        "state": "completed",
        "started_at": (NOW - timedelta(seconds=30)).isoformat(),
        "completed_at": NOW.isoformat(),
        "agent_version": "0.1.0",
        "adb_version": "Android Debug Bridge version 1.0.41",
        "artifacts": [
            {
                "artifact_id": str(ARTIFACT_ID),
                "kind": "startup_trace",
                "mime": "application/x-perfetto-trace",
                "size": 4,
                "sha256_b64": CHECKSUM,
            }
        ],
        "scenarios": [
            {
                "scenario_type": "startup",
                "state": "completed",
                "started_at": (NOW - timedelta(seconds=30)).isoformat(),
                "completed_at": NOW.isoformat(),
                "temperature_start_c": 31.5,
                "temperature_end_c": 32.0,
                "artifact_ids": [str(ARTIFACT_ID)],
                "diagnostic_code": None,
            }
        ],
        "diagnostic_code": None,
    }


def test_agent_completion_accepts_only_the_closed_execution_manifest() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "execution_id": str(EXECUTION_ID),
        "analysis_id": str(ANALYSIS_ID),
        "lease_version": 1,
        "analysis_state": "analyzing",
        "accepted_at": NOW.isoformat(),
    }
    assert service.received is not None
    assert service.received["agent_id"] == AGENT_ID
    assert service.received["execution_id"] == EXECUTION_ID
    assert service.received["manifest_document"]["execution_id"] == str(EXECUTION_ID)


def test_agent_completion_rejects_unknown_manifest_fields() -> None:
    service = FakeTaskService()
    payload = _payload()
    payload["object_key"] = "caller-controlled"
    with _client(service) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=payload,
        )

    assert response.status_code == 422
    assert service.received is None
