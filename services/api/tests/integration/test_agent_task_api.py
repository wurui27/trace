from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.services.agent_tasks import AgentTaskService

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
TOKEN = "ppat_" + "A" * 43


class FakeAgentService:
    async def authenticate_access(self, token: str) -> SimpleNamespace:
        if token != TOKEN:
            from perfpilot_api.services.agents import AgentAuthenticationRejected

            raise AgentAuthenticationRejected
        return SimpleNamespace(agent_id=AGENT_ID)


class FakeTaskService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.poll_result = None

    async def poll(self, **kwargs: object):
        self.calls.append(("poll", kwargs))
        return self.poll_result

    async def renew(self, **kwargs: object):
        self.calls.append(("renew", kwargs))
        from perfpilot_api.services.agent_tasks import LeaseRenewal

        return LeaseRenewal(
            execution_id=EXECUTION_ID,
            lease_version=1,
            lease_expires_at=NOW,
            renew_after_seconds=20,
        )


def _settings() -> Settings:
    return Settings(
        app_env="test",
        allowed_origins=("https://console.example.com",),
        _env_prefix="PERFPILOT_TEST_AGENT_TASK_ISOLATED_",
        _env_file=None,
        _secrets_dir=None,
    )


def _client(task_service: AgentTaskService | FakeTaskService) -> TestClient:
    return TestClient(
        create_app(
            testing=True,
            settings_override=_settings(),
            agent_service=FakeAgentService(),  # type: ignore[arg-type]
            agent_task_service=task_service,  # type: ignore[arg-type]
        )
    )


def test_agent_poll_returns_closed_wait_response() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        response = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "action": "wait",
        "retry_after_seconds": 1,
    }
    assert service.calls == [("poll", {"agent_id": AGENT_ID, "wait_seconds": 0})]


def test_agent_renews_only_its_fenced_execution() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/renew",
            json={"schema_version": "1.0", "lease_version": 1},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "execution_id": str(EXECUTION_ID),
        "lease_version": 1,
        "lease_expires_at": NOW.isoformat(),
        "renew_after_seconds": 20,
    }


def test_agent_poll_returns_only_the_signed_execution_contract() -> None:
    from perfpilot_api.services.agent_tasks import AgentTaskDelivery

    service = FakeTaskService()
    compact = f"{'A' * 24}.{'B' * 24}.{'C' * 86}"
    service.poll_result = AgentTaskDelivery(
        snapshot_jws=compact,
        lease_expires_at=NOW,
    )
    with _client(service) as client:
        response = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "action": "execute",
        "snapshot_jws": compact,
        "lease_expires_at": NOW.isoformat(),
        "renew_after_seconds": 20,
    }
