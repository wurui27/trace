from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_api.security.agent_signatures import encode_ed25519_public_key
from perfpilot_api.security.task_snapshots import TaskSnapshotSigner, verify_task_jws
from perfpilot_api.services.agent_tasks import (
    AgentCancellationAcknowledgement,
    AgentExecutionAccess,
    AgentTaskCancellation,
    AgentTaskConflict,
    AgentTaskDefinition,
    AgentTaskService,
    InMemoryAgentTaskRepository,
    InMemoryAgentTaskWakeup,
    StaleLeaseVersion,
    TaskInputArtifact,
    TaskScenario,
)

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
OTHER_AGENT_ID = UUID("71000000-0000-4000-8000-000000000002")
DEVICE_ID = UUID("72000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
LEASE_ID = UUID("75000000-0000-4000-8000-000000000001")
OTHER_ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000002")


def _definition() -> AgentTaskDefinition:
    return AgentTaskDefinition(
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        agent_id=AGENT_ID,
        device_id=DEVICE_ID,
        device_digest="a" * 64,
        package_name="com.example.perfpilot",
        launch_activity="com.example.perfpilot/com.example.perfpilot.MainActivity",
        cleanup_policy="uninstall",
        input_artifacts=(
            TaskInputArtifact(
                artifact_id=ARTIFACT_ID,
                kind="apk",
                mime="application/vnd.android.package-archive",
                size=4,
                sha256_b64=base64.b64encode(b"a" * 32).decode("ascii"),
            ),
        ),
        scenarios=(
            TaskScenario(
                scenario_type="startup",
                recipe_version=1,
                recipe_hash="b" * 64,
                duration_seconds=15,
                memory_rounds=0,
                swipe_count=0,
            ),
        ),
    )


def _remote_definition(
    *, analysis_id: UUID = ANALYSIS_ID,
) -> AgentTaskDefinition:
    definition = _definition()
    return AgentTaskDefinition(
        **{
            **{field: getattr(definition, field) for field in definition.__dataclass_fields__},
            "analysis_id": analysis_id,
            "schema_version": "1.1",
            "scenarios": (
                definition.scenarios[0],
                TaskScenario(
                    scenario_type="scroll",
                    recipe_version=1,
                    recipe_hash="c" * 64,
                    duration_seconds=15,
                    memory_rounds=0,
                    swipe_count=3,
                ),
            ),
        }
    )


def _service() -> tuple[AgentTaskService, InMemoryAgentTaskRepository, str]:
    private_key = Ed25519PrivateKey.generate()
    repository = InMemoryAgentTaskRepository(
        (_definition(),),
        lease_id_source=lambda: LEASE_ID,
        execution_id_source=lambda: EXECUTION_ID,
    )
    service = AgentTaskService(
        repository=repository,
        signer=TaskSnapshotSigner(
            private_key=private_key,
            kid="lan-test",
            clock=lambda: NOW,
        ),
        wakeup=InMemoryAgentTaskWakeup(),
        clock=lambda: NOW,
    )
    return service, repository, encode_ed25519_public_key(private_key.public_key())


@pytest.mark.asyncio
async def test_only_selected_agent_can_poll_signed_task() -> None:
    service, repository, public_key = _service()
    scheduled = await service.schedule(analysis_id=ANALYSIS_ID)
    assert scheduled is not None

    assert await service.poll(agent_id=OTHER_AGENT_ID, wait_seconds=0) is None
    task = await service.poll(agent_id=AGENT_ID, wait_seconds=0)
    assert task is not None
    claims = verify_task_jws(task.snapshot_jws, public_key, now=NOW)

    assert claims["device_digest"] == "a" * 64
    assert claims["lease_version"] == 1
    assert claims["execution_id"] == str(EXECUTION_ID)
    assert repository.snapshot_digest(EXECUTION_ID) is not None
    assert task.snapshot_jws not in repr(repository)


@pytest.mark.asyncio
async def test_remote_device_enqueue_is_replay_safe_and_oldest_first() -> None:
    first = _remote_definition()
    second = _remote_definition(analysis_id=OTHER_ANALYSIS_ID)
    repository = InMemoryAgentTaskRepository()

    assert await repository.enqueue(second, queued_at=NOW + timedelta(seconds=1)) is True
    assert await repository.enqueue(first, queued_at=NOW) is True
    assert await repository.enqueue(first, queued_at=NOW) is False
    assert await repository.oldest_queued(agent_id=AGENT_ID, now=NOW) == (
        NOW,
        ANALYSIS_ID,
    )
    assert await repository.schedule(analysis_id=ANALYSIS_ID, now=NOW) is not None
    assert await repository.oldest_queued(agent_id=AGENT_ID, now=NOW) == (
        NOW + timedelta(seconds=1),
        OTHER_ANALYSIS_ID,
    )

    conflicting = AgentTaskDefinition(
        **{
            **{field: getattr(first, field) for field in first.__dataclass_fields__},
            "package_name": "com.example.conflict",
        }
    )
    with pytest.raises(AgentTaskConflict):
        await repository.enqueue(conflicting, queued_at=NOW)


@pytest.mark.asyncio
async def test_startup_scroll_task_binds_claims_and_derives_upload_allowlist() -> None:
    definition = _remote_definition()
    private_key = Ed25519PrivateKey.generate()
    repository = InMemoryAgentTaskRepository(
        (definition,),
        lease_id_source=lambda: LEASE_ID,
        execution_id_source=lambda: EXECUTION_ID,
    )
    service = AgentTaskService(
        repository=repository,
        signer=TaskSnapshotSigner(private_key=private_key, kid="lan-test", clock=lambda: NOW),
        wakeup=InMemoryAgentTaskWakeup(),
        clock=lambda: NOW,
    )

    await service.schedule(analysis_id=ANALYSIS_ID)
    delivery = await service.poll(agent_id=AGENT_ID, wait_seconds=0)
    assert delivery is not None
    claims = verify_task_jws(
        delivery.snapshot_jws,
        encode_ed25519_public_key(private_key.public_key()),
        now=NOW,
        expected_team_id=TEAM_ID,
    )
    access = await repository.authorize_execution(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        now=NOW,
    )

    assert claims["team_id"] == str(TEAM_ID)
    assert claims["analysis_id"] == str(ANALYSIS_ID)
    assert claims["agent_id"] == str(AGENT_ID)
    assert claims["device_digest"] == "a" * 64
    assert claims["input_artifacts"] == [
        {
            "artifact_id": str(ARTIFACT_ID),
            "kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 4,
            "sha256_b64": base64.b64encode(b"a" * 32).decode("ascii"),
        }
    ]
    assert [item["scenario_type"] for item in claims["scenarios"]] == [
        "startup",
        "scroll",
    ]
    assert claims["allowed_uploads"] == ["startup_trace", "scroll_trace", "agent_log"]
    assert access.allowed_uploads == ("startup_trace", "scroll_trace", "agent_log")


@pytest.mark.asyncio
async def test_renew_is_idempotent_and_fenced() -> None:
    service, _repository, _public_key = _service()
    await service.schedule(analysis_id=ANALYSIS_ID)

    first = await service.renew(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
    )
    again = await service.renew(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
    )

    assert again == first
    assert first.lease_expires_at == NOW + timedelta(seconds=60)
    assert first.renew_after_seconds == 20
    with pytest.raises(StaleLeaseVersion):
        await service.renew(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=0,
        )


@pytest.mark.asyncio
async def test_poll_rejects_wait_above_twenty_seconds() -> None:
    service, _repository, _public_key = _service()

    with pytest.raises(ValueError):
        await service.poll(agent_id=AGENT_ID, wait_seconds=21)


class RecordingCancellationArtifacts:
    def __init__(self) -> None:
        self.aborted: list[AgentExecutionAccess] = []
        self.projected: list[tuple[AgentExecutionAccess, str]] = []

    async def abort_execution(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.aborted.append(access)

    async def project_cancellation(
        self,
        *,
        access: AgentExecutionAccess,
        reason_code: str,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.projected.append((access, reason_code))


@pytest.mark.asyncio
async def test_cancel_is_delivered_by_poll_and_renew_then_acknowledged() -> None:
    service, _repository, _public_key = _service()
    await service.schedule(analysis_id=ANALYSIS_ID)

    requested = await service.request_cancel(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)
    polled = await service.poll(agent_id=AGENT_ID, wait_seconds=0)
    renewed = await service.renew(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
    )

    assert requested.cancel_requested_at == NOW
    assert requested.execution_id == EXECUTION_ID
    assert polled == AgentTaskCancellation(
        execution_id=EXECUTION_ID,
        lease_version=1,
        requested_at=NOW,
    )
    assert renewed == polled

    artifacts = RecordingCancellationArtifacts()
    acknowledged = await service.acknowledge_cancellation(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        reason_code="analysis_canceled",
        artifact_coordinator=artifacts,
    )
    repeated = await service.acknowledge_cancellation(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        reason_code="analysis_canceled",
        artifact_coordinator=artifacts,
    )

    assert acknowledged == repeated == AgentCancellationAcknowledgement(
        execution_id=EXECUTION_ID,
        analysis_id=ANALYSIS_ID,
        lease_version=1,
        acknowledged_at=NOW,
    )
    assert await service.poll(agent_id=AGENT_ID, wait_seconds=0) is None
    assert artifacts.projected[-1][1] == "analysis_canceled"


@pytest.mark.asyncio
async def test_cancel_rejects_caller_controlled_reason_code() -> None:
    service, _repository, _public_key = _service()
    await service.schedule(analysis_id=ANALYSIS_ID)
    await service.request_cancel(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    with pytest.raises(ValueError):
        await service.acknowledge_cancellation(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            reason_code="private_process_error_with_secrets",
            artifact_coordinator=RecordingCancellationArtifacts(),
        )
