from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.services.agent_tasks import AgentTaskService
from perfpilot_api.services.source_tasks import SourceTaskDelivery, SourceTaskMutation
from perfpilot_api.services.source_tasks import SourceTaskTooLarge

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
TOKEN = "ppat_" + "A" * 43
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_PRIVATE_KEY = Ed25519PrivateKey.generate()
AGENT_PUBLIC_KEY_B64 = base64.b64encode(
    AGENT_PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
).decode("ascii")


def _sign_completion(document: dict[str, object]) -> dict[str, object]:
    signed = dict(document)
    canonical = json.dumps(
        signed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signed["signature_b64"] = base64.b64encode(
        AGENT_PRIVATE_KEY.sign(canonical)
    ).decode("ascii")
    return signed


class FakeAgentService:
    async def authenticate_access(self, token: str) -> SimpleNamespace:
        if token != TOKEN:
            from perfpilot_api.services.agents import AgentAuthenticationRejected

            raise AgentAuthenticationRejected
        return SimpleNamespace(
            agent_id=AGENT_ID,
            team_id=TEAM_ID,
            public_key_b64=AGENT_PUBLIC_KEY_B64,
        )


class FakeTaskService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.poll_result = None
        self.renew_result = None
        self.acknowledgement = None
        self.queued = None
        self.queued_delivery = None

    async def poll(self, **kwargs: object):
        self.calls.append(("poll", kwargs))
        return self.poll_result

    async def renew(self, **kwargs: object):
        self.calls.append(("renew", kwargs))
        if self.renew_result is not None:
            return self.renew_result
        from perfpilot_api.services.agent_tasks import LeaseRenewal

        return LeaseRenewal(
            execution_id=EXECUTION_ID,
            lease_version=1,
            lease_expires_at=NOW,
            renew_after_seconds=20,
        )

    async def acknowledge_cancellation(self, **kwargs: object):
        self.calls.append(("cancel_ack", kwargs))
        if self.acknowledgement is not None:
            return self.acknowledgement
        from perfpilot_api.services.agent_tasks import AgentCancellationAcknowledgement

        return AgentCancellationAcknowledgement(
            execution_id=EXECUTION_ID,
            analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
            lease_version=1,
            acknowledged_at=NOW,
        )

    async def oldest_queued(self, **kwargs):
        self.calls.append(("oldest_queued", kwargs))
        return self.queued

    async def schedule(self, **kwargs):
        self.calls.append(("schedule", kwargs))
        self.poll_result = self.queued_delivery
        return None


class FakeSourceTaskService:
    def __init__(self) -> None:
        self.delivery = None
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.reject_too_large = False
        self.active = False

    async def lease_next(self, **kwargs):
        self.calls.append(("lease_next", kwargs))
        result, self.delivery = self.delivery, None
        return result

    async def owns(self, **kwargs):
        self.calls.append(("owns", kwargs))
        return True

    async def has_active(self, **kwargs):
        self.calls.append(("has_active", kwargs))
        return self.active

    async def oldest_queued(self, **kwargs):
        self.calls.append(("oldest_queued", kwargs))
        if self.delivery is None:
            return None
        return (self.delivery.created_at, self.delivery.execution_id)

    async def renew(self, **kwargs):
        self.calls.append(("renew", kwargs))
        return SourceTaskMutation(
            execution_id=EXECUTION_ID,
            analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
            lease_version=1,
            state="running",
            occurred_at=NOW,
        )

    async def complete(self, **kwargs):
        self.calls.append(("complete", kwargs))
        if self.reject_too_large:
            raise SourceTaskTooLarge
        return SourceTaskMutation(
            execution_id=EXECUTION_ID,
            analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
            lease_version=1,
            state="failed",
            occurred_at=NOW,
            artifact_id=UUID("40000000-0000-4000-8000-000000000001"),
            checksum="a" * 64,
        )

    async def ack_cancel(self, **kwargs):
        self.calls.append(("ack_cancel", kwargs))
        return SourceTaskMutation(
            execution_id=EXECUTION_ID,
            analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
            lease_version=1,
            state="canceled",
            occurred_at=NOW,
        )


def _settings() -> Settings:
    return Settings(
        app_env="test",
        allowed_origins=("https://console.example.com",),
        _env_prefix="PERFPILOT_TEST_AGENT_TASK_ISOLATED_",
        _env_file=None,
        _secrets_dir=None,
    )


def _client(
    task_service: AgentTaskService | FakeTaskService,
    source_task_service: FakeSourceTaskService | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            testing=True,
            settings_override=_settings(),
            agent_service=FakeAgentService(),  # type: ignore[arg-type]
            agent_task_service=task_service,  # type: ignore[arg-type]
            source_task_service=source_task_service,  # type: ignore[arg-type]
            source_task_completion_recorder=SimpleNamespace(),  # type: ignore[arg-type]
            agent_upload_service=SimpleNamespace(),  # type: ignore[arg-type]
        )
    )


def test_agent_poll_returns_closed_source_task_and_routes_fenced_renewal() -> None:
    device = FakeTaskService()
    source = FakeSourceTaskService()
    source.delivery = SourceTaskDelivery(
        execution_id=EXECUTION_ID,
        analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
        agent_id=AGENT_ID,
        task_type="source_context",
        lease_version=1,
        lease_token="opaque-lease-token-123",
        lease_expires_at=NOW,
        snapshot={
            "schema_version": "1.0",
            "aud": "perfpilot-agent",
            "task_type": "source_context",
            "execution_id": str(EXECUTION_ID),
            "analysis_id": "30000000-0000-4000-8000-000000000001",
            "team_id": "10000000-0000-4000-8000-000000000001",
            "agent_id": str(AGENT_ID),
            "workspace_id": "91000000-0000-4000-8000-000000000001",
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": None,
            "lease_version": 1,
            "expires_at": NOW.isoformat(),
            "finding_hints": [],
            "limits": {"max_findings": 3, "max_files": 12, "max_bytes": 98_304},
        },
        signature_b64="A" * 86 + "==",
    )
    with _client(device, source) as client:
        polled = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        renewed = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/renew",
            json={"schema_version": "1.0", "lease_version": 1},
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-PerfPilot-Lease-Token": "opaque-lease-token-123",
            },
        )

    assert polled.status_code == 200
    assert polled.json()["task_kind"] == "source"
    assert polled.json()["lease_token"] == "opaque-lease-token-123"
    assert renewed.status_code == 200
    assert renewed.json()["state"] == "running"
    assert source.calls[-1] == (
        "renew",
        {
            "execution_id": EXECUTION_ID,
            "agent_id": AGENT_ID,
            "lease_version": 1,
            "lease_token": "opaque-lease-token-123",
        },
    )


def test_agent_poll_fairly_selects_oldest_queued_kind_in_both_orderings() -> None:
    from datetime import timedelta
    from perfpilot_api.services.agent_tasks import AgentTaskDelivery

    compact = f"{'A' * 24}.{'B' * 24}.{'C' * 86}"

    def source_delivery(created_at):
        return SourceTaskDelivery(
            execution_id=EXECUTION_ID,
            analysis_id=UUID("30000000-0000-4000-8000-000000000001"),
            agent_id=AGENT_ID,
            task_type="source_context",
            lease_version=1,
            lease_token="opaque-lease-token-123",
            lease_expires_at=NOW,
            snapshot={
                "schema_version": "1.0",
                "aud": "perfpilot-agent",
                "task_type": "source_context",
            },
            signature_b64="A" * 86 + "==",
            created_at=created_at,
        )

    device = FakeTaskService()
    source = FakeSourceTaskService()
    device.queued = (NOW - timedelta(seconds=2), UUID(int=2))
    device.queued_delivery = AgentTaskDelivery(
        snapshot_jws=compact,
        lease_expires_at=NOW,
    )
    source.delivery = source_delivery(NOW - timedelta(seconds=1))
    with _client(device, source) as client:
        older_device = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert older_device.status_code == 200
    assert older_device.json()["schema_version"] == "1.0"
    assert any(call[0] == "schedule" for call in device.calls)
    assert not any(call[0] == "lease_next" for call in source.calls)

    device = FakeTaskService()
    source = FakeSourceTaskService()
    device.queued = (NOW - timedelta(seconds=1), UUID(int=2))
    device.queued_delivery = AgentTaskDelivery(
        snapshot_jws=compact,
        lease_expires_at=NOW,
    )
    source.delivery = source_delivery(NOW - timedelta(seconds=2))
    with _client(device, source) as client:
        older_source = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert older_source.status_code == 200
    assert older_source.json()["task_kind"] == "source"
    assert not any(call[0] == "schedule" for call in device.calls)


def test_agent_complete_routes_source_body_to_injected_recorder_service() -> None:
    source = FakeSourceTaskService()
    completion = _sign_completion({
        "schema_version": "1.0",
        "task_type": "source_context",
        "execution_id": str(EXECUTION_ID),
        "analysis_id": "30000000-0000-4000-8000-000000000001",
        "team_id": str(TEAM_ID),
        "agent_id": str(AGENT_ID),
        "workspace_id": "91000000-0000-4000-8000-000000000001",
        "lease_version": 1,
        "state": "failed",
        "result": {"failure_code": "source_unavailable", "retryable": False},
    })
    with _client(FakeTaskService(), source) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=completion,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-PerfPilot-Lease-Token": "opaque-lease-token-123",
            },
        )

    assert response.status_code == 200
    assert response.json()["artifact_id"] == "40000000-0000-4000-8000-000000000001"
    call = source.calls[-1]
    assert call[0] == "complete"
    assert call[1]["recorder"] is not None
    assert call[1]["completion_document"] == completion


def test_agent_complete_authenticates_before_parsing_the_completion_body() -> None:
    source = FakeSourceTaskService()
    malformed = {"schema_version": "1.0", "task_type": "source_context"}
    with _client(FakeTaskService(), source) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=malformed,
            headers={"Authorization": "Bearer invalid"},
        )

    assert response.status_code == 401
    assert source.calls == []


def test_agent_complete_resolves_server_owned_task_before_closed_body_validation() -> None:
    source = FakeSourceTaskService()
    malformed = {"schema_version": "1.0", "task_type": "source_context"}
    with _client(FakeTaskService(), source) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=malformed,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 422
    assert source.calls == [
        ("owns", {"execution_id": EXECUTION_ID, "agent_id": AGENT_ID})
    ]


def test_agent_complete_rejects_tampered_or_misbound_source_documents() -> None:
    base = {
        "schema_version": "1.0",
        "task_type": "source_context",
        "execution_id": str(EXECUTION_ID),
        "analysis_id": "30000000-0000-4000-8000-000000000001",
        "team_id": str(TEAM_ID),
        "agent_id": str(AGENT_ID),
        "workspace_id": "91000000-0000-4000-8000-000000000001",
        "lease_version": 1,
        "state": "failed",
        "result": {"failure_code": "source_unavailable", "retryable": False},
    }
    tampered = _sign_completion(base)
    tampered["state"] = "canceled"
    wrong_agent = _sign_completion(
        {**base, "agent_id": "71000000-0000-4000-8000-000000000002"}
    )
    client_kind = _sign_completion({**base, "task_kind": "source"})
    source = FakeSourceTaskService()

    with _client(FakeTaskService(), source) as client:
        tampered_response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=tampered,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        wrong_agent_response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=wrong_agent,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        client_kind_response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=client_kind,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert tampered_response.status_code == 401
    assert wrong_agent_response.status_code == 404
    assert client_kind_response.status_code == 422
    assert all(call[0] == "owns" for call in source.calls)


def test_source_completion_budget_counts_canonical_utf8_not_json_escapes() -> None:
    base = {
        "schema_version": "1.0",
        "task_type": "source_context",
        "execution_id": str(EXECUTION_ID),
        "analysis_id": "30000000-0000-4000-8000-000000000001",
        "team_id": str(TEAM_ID),
        "agent_id": str(AGENT_ID),
        "workspace_id": "91000000-0000-4000-8000-000000000001",
        "lease_version": 1,
        "state": "failed",
    }
    accepted = _sign_completion(
        {
            **base,
            "result": {
                "failure_code": "source_unavailable",
                "retryable": False,
                "note": "界" * 22_000,
            },
        }
    )
    rejected = _sign_completion(
        {
            **base,
            "result": {
                "failure_code": "source_unavailable",
                "retryable": False,
                "note": "界" * 44_000,
            },
        }
    )
    source = FakeSourceTaskService()
    with _client(FakeTaskService(), source) as client:
        accepted_response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=accepted,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-PerfPilot-Lease-Token": "opaque-lease-token-123",
            },
        )
        rejected_response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=rejected,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-PerfPilot-Lease-Token": "opaque-lease-token-123",
            },
        )

    assert accepted_response.status_code == 200
    assert rejected_response.status_code == 413
    assert [call[0] for call in source.calls].count("complete") == 1


def test_source_cancel_ack_and_oversized_completion_use_source_fence() -> None:
    source = FakeSourceTaskService()
    with _client(FakeTaskService(), source) as client:
        canceled = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/cancel-ack",
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "reason_code": "analysis_canceled",
            },
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-PerfPilot-Lease-Token": "opaque-lease-token-123",
            },
        )
        source.reject_too_large = True
        oversized = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/complete",
            json=_sign_completion({
                "schema_version": "1.0",
                "task_type": "source_context",
                "execution_id": str(EXECUTION_ID),
                "analysis_id": "30000000-0000-4000-8000-000000000001",
                "team_id": str(TEAM_ID),
                "agent_id": str(AGENT_ID),
                "workspace_id": "91000000-0000-4000-8000-000000000001",
                "lease_version": 1,
                "state": "failed",
                "result": {"failure_code": "source_unavailable", "retryable": False},
            }),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-PerfPilot-Lease-Token": "opaque-lease-token-123",
            },
        )

    assert canceled.status_code == 200
    assert canceled.json()["state"] == "canceled"
    assert oversized.status_code == 413


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
    assert response.content == (
        b'{"schema_version":"1.0","action":"execute","snapshot_jws":"'
        + compact.encode("ascii")
        + b'","lease_expires_at":"2026-08-05T08:00:00+00:00",'
        b'"renew_after_seconds":20}'
    )


def test_agent_poll_and_renew_return_the_closed_cancel_action() -> None:
    from perfpilot_api.services.agent_tasks import AgentTaskCancellation

    service = FakeTaskService()
    cancellation = AgentTaskCancellation(
        execution_id=EXECUTION_ID,
        lease_version=1,
        requested_at=NOW,
    )
    service.poll_result = cancellation
    service.renew_result = cancellation
    with _client(service) as client:
        polled = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        renewed = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/renew",
            json={"schema_version": "1.0", "lease_version": 1},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    expected = {
        "schema_version": "1.0",
        "action": "cancel",
        "execution_id": str(EXECUTION_ID),
        "lease_version": 1,
        "reason_code": "analysis_canceled",
    }
    assert polled.status_code == 200
    assert renewed.status_code == 200
    assert polled.json() == expected
    assert renewed.json() == expected


def test_agent_cancel_ack_accepts_only_the_stable_reason_code() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        response = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/cancel-ack",
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "reason_code": "analysis_canceled",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        rejected = client.post(
            f"/v1/agent/tasks/{EXECUTION_ID}/cancel-ack",
            json={
                "schema_version": "1.0",
                "lease_version": 1,
                "reason_code": "raw_adb_error",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "execution_id": str(EXECUTION_ID),
        "analysis_id": "30000000-0000-4000-8000-000000000001",
        "lease_version": 1,
        "state": "canceled",
        "acknowledged_at": NOW.isoformat(),
    }
    assert rejected.status_code == 422
