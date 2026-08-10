from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest

from perfpilot_api.services.source_tasks import (
    InMemorySourceTaskRepository,
    SourceCompletionArtifact,
    SourceTaskConflict,
    SourceTaskService,
    SourceTaskTooLarge,
    SourcePatchArtifactBinding,
    SourcePatchArtifactPayload,
    StaleSourceTaskLease,
)

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("91000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000001")


class FixedRecorder:
    def __init__(self) -> None:
        self.calls = 0

    async def record_completion(self, *, task, document, checksum, now):
        self.calls += 1
        return SourceCompletionArtifact(artifact_id=ARTIFACT_ID, checksum=checksum)


class PrivatePatchStore:
    def __init__(self) -> None:
        self.values: dict[UUID, SourcePatchArtifactPayload] = {}

    async def write_patch(
        self, *, team_id, analysis_id, patch, checksum, idempotency_key
    ):
        artifact_id = UUID("98000000-0000-4000-8000-000000000001")
        self.values[artifact_id] = SourcePatchArtifactPayload(
            patch=patch,
            checksum=checksum,
        )
        return SourcePatchArtifactBinding(artifact_id=artifact_id, checksum=checksum)

    async def read_patch(self, *, team_id, artifact_id):
        return self.values[artifact_id]


class CorruptPatchStore(PrivatePatchStore):
    async def read_patch(self, *, team_id, artifact_id):
        return SourcePatchArtifactPayload(patch="tampered", checksum="b" * 64)


def _service() -> tuple[SourceTaskService, InMemorySourceTaskRepository]:
    repository = InMemorySourceTaskRepository()
    execution_ids = iter(
        (
            EXECUTION_ID,
            UUID("73000000-0000-4000-8000-000000000002"),
            UUID("73000000-0000-4000-8000-000000000003"),
        )
    )
    service = SourceTaskService(
        repository=repository,
        clock=lambda: NOW,
        execution_id_source=lambda: next(execution_ids),
        lease_token_source=lambda: b"source-lease-token",
    )
    return service, repository


async def _lease_context(service: SourceTaskService):
    await service.create_context_task(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        validation_profile_id=None,
        finding_hints=(),
    )
    lease = await service.lease_next(agent_id=AGENT_ID)
    assert lease is not None
    return lease


@pytest.mark.asyncio
async def test_context_task_is_unique_and_has_no_device_binding() -> None:
    service, repository = _service()
    first = await service.create_context_task(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        validation_profile_id=None,
        finding_hints=(),
    )
    second = await service.create_context_task(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        validation_profile_id=None,
        finding_hints=(),
    )
    assert second.id == first.id
    assert not hasattr(repository.tasks[first.id], "device_id")


@pytest.mark.asyncio
async def test_lease_returns_token_but_stores_only_digest() -> None:
    service, repository = _service()
    lease = await _lease_context(service)
    stored = next(iter(repository.tasks.values()))
    assert lease.lease_token == "c291cmNlLWxlYXNlLXRva2Vu"
    assert stored.lease_token_digest == hashlib.sha256(
        lease.lease_token.encode("ascii")
    ).hexdigest()
    assert lease.lease_token not in repr(stored)
    assert "device_id" not in lease.snapshot


@pytest.mark.asyncio
async def test_active_lease_never_redelivers_plaintext_token_even_after_restart() -> None:
    service, repository = _service()
    first = await _lease_context(service)

    repeated = await service.lease_next(agent_id=AGENT_ID)
    restarted = SourceTaskService(
        repository=repository,
        clock=lambda: NOW,
        lease_token_source=lambda: b"different-source-token",
    )
    recovered = await restarted.lease_next(agent_id=AGENT_ID)

    assert first.lease_token == "c291cmNlLWxlYXNlLXRva2Vu"
    assert repeated is None
    assert recovered is None


@pytest.mark.asyncio
async def test_mutations_require_execution_agent_version_and_token_fence() -> None:
    service, _ = _service()
    lease = await _lease_context(service)
    with pytest.raises(StaleSourceTaskLease):
        await service.renew(
            execution_id=lease.execution_id,
            agent_id=AGENT_ID,
            lease_version=lease.lease_version,
            lease_token="wrong-token-value",
        )
    renewal = await service.renew(
        execution_id=lease.execution_id,
        agent_id=AGENT_ID,
        lease_version=lease.lease_version,
        lease_token=lease.lease_token,
    )
    assert renewal.state == "running"


@pytest.mark.asyncio
async def test_completion_is_idempotent_only_for_equal_checksum() -> None:
    service, repository = _service()
    lease = await _lease_context(service)
    completion = {
        "schema_version": "1.0",
        "task_type": "source_context",
        "execution_id": str(lease.execution_id),
        "analysis_id": str(ANALYSIS_ID),
        "team_id": str(TEAM_ID),
        "agent_id": str(AGENT_ID),
        "workspace_id": str(WORKSPACE_ID),
        "lease_version": lease.lease_version,
        "state": "failed",
        "result": {"failure_code": "source_unavailable", "retryable": False},
        "signature_b64": "A" * 86 + "==",
    }
    recorder = FixedRecorder()
    first = await service.complete(
        execution_id=lease.execution_id,
        agent_id=AGENT_ID,
        lease_version=lease.lease_version,
        lease_token=lease.lease_token,
        completion_document=completion,
        recorder=recorder,
    )
    second = await service.complete(
        execution_id=lease.execution_id,
        agent_id=AGENT_ID,
        lease_version=lease.lease_version,
        lease_token=lease.lease_token,
        completion_document=completion,
        recorder=recorder,
    )
    assert second == first
    assert recorder.calls == 1
    assert next(iter(repository.tasks.values())).completion_artifact_id == ARTIFACT_ID
    with pytest.raises(SourceTaskConflict):
        await service.complete(
            execution_id=lease.execution_id,
            agent_id=AGENT_ID,
            lease_version=lease.lease_version,
            lease_token=lease.lease_token,
            completion_document={
                **completion,
                "result": {"failure_code": "source_changed", "retryable": False},
            },
            recorder=recorder,
        )


@pytest.mark.asyncio
async def test_equal_patch_fix_ids_do_not_collide_across_analyses() -> None:
    service, _ = _service()
    patch_store = PrivatePatchStore()
    service._patch_writer = patch_store
    service._patch_reader = patch_store
    arguments = {
        "team_id": TEAM_ID,
        "agent_id": AGENT_ID,
        "workspace_id": WORKSPACE_ID,
        "validation_profile_id": UUID("94000000-0000-4000-8000-000000000001"),
        "snapshot_id": UUID("95000000-0000-4000-8000-000000000001"),
        "snapshot_hash": "a" * 64,
        "fix_id": UUID("96000000-0000-4000-8000-000000000001"),
        "patch": "diff --git a/a.kt b/a.kt\n",
    }
    first = await service.create_patch_task(analysis_id=ANALYSIS_ID, **arguments)
    second = await service.create_patch_task(
        analysis_id=UUID("30000000-0000-4000-8000-000000000002"),
        **arguments,
    )
    assert first.id != second.id


@pytest.mark.asyncio
async def test_patch_body_is_private_artifact_and_only_resolved_for_delivery() -> None:
    service, repository = _service()
    patch_store = PrivatePatchStore()
    service._patch_writer = patch_store
    service._patch_reader = patch_store
    sentinel = "diff --git a/SECRET.kt b/SECRET.kt\n+private-token-sentinel\n"

    created = await service.create_patch_task(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        validation_profile_id=UUID("94000000-0000-4000-8000-000000000001"),
        snapshot_id=UUID("95000000-0000-4000-8000-000000000001"),
        snapshot_hash="a" * 64,
        fix_id=UUID("96000000-0000-4000-8000-000000000001"),
        patch=sentinel,
    )

    stored = repository.tasks[created.id]
    assert sentinel not in repr(stored)
    assert sentinel not in str(stored.request_document)
    assert "patch" not in stored.request_document
    assert set(stored.request_document) == {
        "snapshot_policy",
        "validation_profile_id",
        "snapshot_id",
        "snapshot_hash",
        "fix_id",
        "patch_artifact_id",
        "patch_sha256",
    }
    delivery = await service.lease_next(agent_id=AGENT_ID)
    assert delivery is not None
    assert delivery.snapshot["patch"] == sentinel


@pytest.mark.asyncio
async def test_invalid_closed_request_leaves_no_control_row() -> None:
    service, repository = _service()

    with pytest.raises(Exception):
        await service.create_context_task(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            agent_id=AGENT_ID,
            workspace_id=WORKSPACE_ID,
            validation_profile_id=None,
            finding_hints=(
                {
                    "finding_id": str(UUID(int=1)),
                    "evidence_ids": [],
                    "rule_id": "startup.main_thread",
                    "symbol_hints": [],
                },
            ),
        )

    assert repository.tasks == {}


@pytest.mark.asyncio
async def test_corrupt_private_patch_never_leaves_an_undelivered_lease() -> None:
    service, repository = _service()
    patch_store = CorruptPatchStore()
    service._patch_writer = patch_store
    service._patch_reader = patch_store
    created = await service.create_patch_task(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        validation_profile_id=UUID("94000000-0000-4000-8000-000000000001"),
        snapshot_id=UUID("95000000-0000-4000-8000-000000000001"),
        snapshot_hash="a" * 64,
        fix_id=UUID("96000000-0000-4000-8000-000000000001"),
        patch="diff --git a/a.kt b/a.kt\n",
    )

    with pytest.raises(Exception):
        await service.lease_next(agent_id=AGENT_ID)

    assert repository.tasks[created.id].state == "failed"
    assert repository.tasks[created.id].failure_code == "source_patch_artifact_invalid"


@pytest.mark.asyncio
async def test_completion_rejects_canonical_json_over_128_kib_before_recording() -> None:
    service, _ = _service()
    lease = await _lease_context(service)
    recorder = FixedRecorder()
    with pytest.raises(SourceTaskTooLarge):
        await service.complete(
            execution_id=lease.execution_id,
            agent_id=AGENT_ID,
            lease_version=lease.lease_version,
            lease_token=lease.lease_token,
            completion_document={
                "schema_version": "1.0",
                "task_type": "source_context",
                "execution_id": str(lease.execution_id),
                "analysis_id": str(ANALYSIS_ID),
                "team_id": str(TEAM_ID),
                "agent_id": str(AGENT_ID),
                "workspace_id": str(WORKSPACE_ID),
                "lease_version": 1,
                "state": "failed",
                "result": {"failure_code": "source_unavailable", "retryable": False, "padding": "x" * (128 * 1024)},
                "signature_b64": "A" * 86 + "==",
            },
            recorder=recorder,
        )
    assert recorder.calls == 0
