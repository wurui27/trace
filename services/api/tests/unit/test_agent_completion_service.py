from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_api.security.task_snapshots import TaskSnapshotSigner
from perfpilot_api.services.agent_tasks import (
    AgentTaskConflict,
    AgentTaskDefinition,
    AgentTaskService,
    InMemoryAgentTaskRepository,
    InMemoryAgentTaskWakeup,
    TaskInputArtifact,
    TaskScenario,
)

NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
INPUT_ID = UUID("40000000-0000-4000-8000-000000000001")
OUTPUT_ID = UUID("76000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("72000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
LEASE_ID = UUID("75000000-0000-4000-8000-000000000001")
CHECKSUM = base64.b64encode(b"a" * 32).decode("ascii")


class RecordingCompletionArtifacts:
    def __init__(self) -> None:
        self.validated = 0
        self.projected = 0

    async def validate_completion(self, **kwargs: object) -> None:
        self.validated += 1
        assert kwargs["access"].execution_id == EXECUTION_ID
        assert kwargs["manifest"].artifacts[0].artifact_id == OUTPUT_ID

    async def project_completion(self, **kwargs: object) -> None:
        self.projected += 1
        assert kwargs["now"] == NOW


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
                artifact_id=INPUT_ID,
                kind="apk",
                mime="application/vnd.android.package-archive",
                size=4,
                sha256_b64=CHECKSUM,
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


def _manifest() -> dict[str, object]:
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
                "artifact_id": str(OUTPUT_ID),
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
                "artifact_ids": [str(OUTPUT_ID)],
                "diagnostic_code": None,
            }
        ],
        "diagnostic_code": None,
    }


def _service() -> tuple[AgentTaskService, RecordingCompletionArtifacts]:
    private_key = Ed25519PrivateKey.generate()
    repository = InMemoryAgentTaskRepository(
        (_definition(),),
        lease_id_source=lambda: LEASE_ID,
        execution_id_source=lambda: EXECUTION_ID,
    )
    artifacts = RecordingCompletionArtifacts()
    return (
        AgentTaskService(
            repository=repository,
            signer=TaskSnapshotSigner(private_key=private_key, kid="completion-test"),
            wakeup=InMemoryAgentTaskWakeup(),
            clock=lambda: NOW,
        ),
        artifacts,
    )


@pytest.mark.asyncio
async def test_completion_validates_artifacts_releases_lease_and_is_idempotent() -> None:
    service, artifacts = _service()
    await service.schedule(analysis_id=ANALYSIS_ID)

    completed = await service.complete(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        manifest_document=_manifest(),
        artifact_validator=artifacts,
    )
    repeated = await service.complete(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        manifest_document=_manifest(),
        artifact_validator=artifacts,
    )

    assert repeated == completed
    assert completed.analysis_id == ANALYSIS_ID
    assert completed.analysis_state == "analyzing"
    assert await service.poll(agent_id=AGENT_ID, wait_seconds=0) is None
    assert artifacts.validated == 2
    assert artifacts.projected == 2


@pytest.mark.asyncio
async def test_completion_rejects_a_different_manifest_after_release() -> None:
    service, artifacts = _service()
    await service.schedule(analysis_id=ANALYSIS_ID)
    await service.complete(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        manifest_document=_manifest(),
        artifact_validator=artifacts,
    )
    conflicting = _manifest()
    conflicting["adb_version"] = "Android Debug Bridge version 2.0"

    with pytest.raises(AgentTaskConflict):
        await service.complete(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            manifest_document=conflicting,
            artifact_validator=artifacts,
        )


@pytest.mark.asyncio
async def test_completion_rejects_closed_schema_and_scenario_artifact_drift() -> None:
    service, artifacts = _service()
    await service.schedule(analysis_id=ANALYSIS_ID)
    invalid = _manifest()
    invalid["object_key"] = "caller-controlled"

    with pytest.raises(AgentTaskConflict):
        await service.complete(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            manifest_document=invalid,
            artifact_validator=artifacts,
        )
