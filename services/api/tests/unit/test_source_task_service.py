from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest

from perfpilot_api.services.source_tasks import (
    InMemorySourceTaskRepository,
    SourceCompletionArtifact,
    SourcePatchArtifactBinding,
    SourcePatchArtifactPayload,
    SourceTaskConflict,
    SourceTaskService,
    SourceTaskTooLarge,
    SourceTaskUnavailable,
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
        self.analysis_by_artifact: dict[UUID, UUID] = {}
        self.owner_by_artifact: dict[UUID, str] = {}
        self.read_calls: list[tuple[UUID, UUID, UUID, str]] = []
        self.abort_calls = 0
        self.write_calls = 0

    async def write_patch(
        self, *, team_id, analysis_id, patch, checksum, idempotency_key
    ):
        self.write_calls += 1
        artifact_id = uuid5(UUID("98000000-0000-4000-8000-000000000001"), idempotency_key)
        existing = self.values.get(artifact_id)
        if existing is not None:
            assert self.analysis_by_artifact[artifact_id] == analysis_id
            assert existing.checksum == checksum
            return SourcePatchArtifactBinding(
                artifact_id=artifact_id,
                checksum=checksum,
            )
        ownership_token = f"owner:{artifact_id}"
        self.values[artifact_id] = SourcePatchArtifactPayload(
            patch=patch,
            checksum=checksum,
        )
        self.analysis_by_artifact[artifact_id] = analysis_id
        self.owner_by_artifact[artifact_id] = ownership_token
        return SourcePatchArtifactBinding(
            artifact_id=artifact_id,
            checksum=checksum,
            ownership_token=ownership_token,
        )

    async def abort_patch(self, *, team_id, analysis_id, binding):
        self.abort_calls += 1
        if self.owner_by_artifact.get(binding.artifact_id) != binding.ownership_token:
            return
        assert self.analysis_by_artifact[binding.artifact_id] == analysis_id
        self.values.pop(binding.artifact_id)
        self.analysis_by_artifact.pop(binding.artifact_id)
        self.owner_by_artifact.pop(binding.artifact_id)

    async def read_patch(
        self, *, team_id, analysis_id, artifact_id, expected_checksum
    ):
        self.read_calls.append(
            (team_id, analysis_id, artifact_id, expected_checksum)
        )
        assert self.analysis_by_artifact[artifact_id] == analysis_id
        payload = self.values[artifact_id]
        assert payload.checksum == expected_checksum
        return payload


class BarrierPatchStore(PrivatePatchStore):
    def __init__(self) -> None:
        super().__init__()
        self._arrived = 0
        self._release = asyncio.Event()

    async def write_patch(
        self, *, team_id, analysis_id, patch, checksum, idempotency_key
    ):
        binding = await super().write_patch(
            team_id=team_id,
            analysis_id=analysis_id,
            patch=patch,
            checksum=checksum,
            idempotency_key=idempotency_key,
        )
        self._arrived += 1
        if self._arrived == 2:
            self._release.set()
        await self._release.wait()
        return binding


class CorruptPatchStore(PrivatePatchStore):
    async def read_patch(
        self, *, team_id, analysis_id, artifact_id, expected_checksum
    ):
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
async def test_expired_lease_rejects_renew_and_complete_before_recorder() -> None:
    service, repository = _service()
    lease = await _lease_context(service)
    expired = SourceTaskService(
        repository=repository,
        clock=lambda: NOW + timedelta(seconds=61),
    )
    recorder = FixedRecorder()
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

    with pytest.raises(StaleSourceTaskLease):
        await expired.renew(
            execution_id=lease.execution_id,
            agent_id=AGENT_ID,
            lease_version=lease.lease_version,
            lease_token=lease.lease_token,
        )
    with pytest.raises(StaleSourceTaskLease):
        await expired.complete(
            execution_id=lease.execution_id,
            agent_id=AGENT_ID,
            lease_version=lease.lease_version,
            lease_token=lease.lease_token,
            completion_document=completion,
            recorder=recorder,
        )

    stored = repository.tasks[lease.execution_id]
    assert stored.state == "expired"
    assert stored.failure_code == "source_task_lease_expired"
    assert recorder.calls == 0


@pytest.mark.asyncio
async def test_expired_cancel_requested_lease_rejects_agent_ack() -> None:
    service, repository = _service()
    lease = await _lease_context(service)
    await service.request_cancel(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)
    expired = SourceTaskService(
        repository=repository,
        clock=lambda: lease.lease_expires_at,
    )

    with pytest.raises(StaleSourceTaskLease):
        await expired.ack_cancel(
            execution_id=lease.execution_id,
            agent_id=AGENT_ID,
            lease_version=lease.lease_version,
            lease_token=lease.lease_token,
        )

    stored = repository.tasks[lease.execution_id]
    assert stored.state == "expired"
    assert stored.failure_code == "source_task_lease_expired"


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
async def test_patch_retry_is_exact_and_never_rewrites_private_artifact() -> None:
    service, _ = _service()
    patch_store = PrivatePatchStore()
    service._patch_writer = patch_store
    service._patch_reader = patch_store
    arguments = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "agent_id": AGENT_ID,
        "workspace_id": WORKSPACE_ID,
        "validation_profile_id": UUID("94000000-0000-4000-8000-000000000001"),
        "snapshot_id": UUID("95000000-0000-4000-8000-000000000001"),
        "snapshot_hash": "a" * 64,
        "fix_id": UUID("96000000-0000-4000-8000-000000000001"),
        "patch": "diff --git a/a.kt b/a.kt\n",
    }

    first = await service.create_patch_task(**arguments)
    same = await service.create_patch_task(**arguments)
    with pytest.raises(SourceTaskConflict):
        await service.create_patch_task(
            **{**arguments, "patch": "diff --git a/b.kt b/b.kt\n"}
        )

    assert same.id == first.id
    assert patch_store.write_calls == 1


@pytest.mark.parametrize(
    "override",
    (
        {"patch": "diff --git a/b.kt b/b.kt\n"},
        {
            "validation_profile_id": UUID(
                "94000000-0000-4000-8000-000000000002"
            )
        },
        {"snapshot_hash": "b" * 64},
        {"agent_id": UUID("71000000-0000-4000-8000-000000000002")},
    ),
)
@pytest.mark.asyncio
async def test_patch_retry_binding_changes_conflict_before_writer(override) -> None:
    service, _ = _service()
    patch_store = PrivatePatchStore()
    service._patch_writer = patch_store
    arguments = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "agent_id": AGENT_ID,
        "workspace_id": WORKSPACE_ID,
        "validation_profile_id": UUID("94000000-0000-4000-8000-000000000001"),
        "snapshot_id": UUID("95000000-0000-4000-8000-000000000001"),
        "snapshot_hash": "a" * 64,
        "fix_id": UUID("96000000-0000-4000-8000-000000000001"),
        "patch": "diff --git a/a.kt b/a.kt\n",
    }

    await service.create_patch_task(**arguments)
    with pytest.raises(SourceTaskConflict):
        await service.create_patch_task(**{**arguments, **override})

    assert patch_store.write_calls == 1


@pytest.mark.asyncio
async def test_concurrent_identical_patch_creation_keeps_shared_artifact() -> None:
    service, repository = _service()
    patch_store = BarrierPatchStore()
    service._patch_writer = patch_store
    arguments = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "agent_id": AGENT_ID,
        "workspace_id": WORKSPACE_ID,
        "validation_profile_id": UUID("94000000-0000-4000-8000-000000000001"),
        "snapshot_id": UUID("95000000-0000-4000-8000-000000000001"),
        "snapshot_hash": "a" * 64,
        "fix_id": UUID("96000000-0000-4000-8000-000000000001"),
        "patch": "diff --git a/a.kt b/a.kt\n",
    }

    first, second = await asyncio.gather(
        service.create_patch_task(**arguments),
        service.create_patch_task(**arguments),
    )

    assert first.id == second.id
    assert len(repository.tasks) == 1
    assert len(patch_store.values) == 1
    assert patch_store.abort_calls == 0


@pytest.mark.asyncio
async def test_concurrent_conflicting_patch_creation_aborts_only_loser_artifact() -> None:
    service, repository = _service()
    patch_store = BarrierPatchStore()
    service._patch_writer = patch_store
    arguments = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "agent_id": AGENT_ID,
        "workspace_id": WORKSPACE_ID,
        "validation_profile_id": UUID("94000000-0000-4000-8000-000000000001"),
        "snapshot_id": UUID("95000000-0000-4000-8000-000000000001"),
        "snapshot_hash": "a" * 64,
        "fix_id": UUID("96000000-0000-4000-8000-000000000001"),
    }

    outcomes = await asyncio.gather(
        service.create_patch_task(
            **arguments,
            patch="diff --git a/a.kt b/a.kt\n",
        ),
        service.create_patch_task(
            **arguments,
            patch="diff --git a/b.kt b/b.kt\n",
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, SourceTaskConflict) for outcome in outcomes) == 1
    assert len(repository.tasks) == 1
    assert len(patch_store.values) == 1
    assert patch_store.abort_calls == 1


@pytest.mark.asyncio
async def test_patch_reader_rejects_same_team_cross_analysis_substitution() -> None:
    service, repository = _service()
    patch_store = PrivatePatchStore()
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
    foreign_analysis = UUID("30000000-0000-4000-8000-000000000002")
    foreign = await service.create_patch_task(
        team_id=TEAM_ID,
        analysis_id=foreign_analysis,
        agent_id=UUID("71000000-0000-4000-8000-000000000002"),
        workspace_id=WORKSPACE_ID,
        validation_profile_id=UUID("94000000-0000-4000-8000-000000000001"),
        snapshot_id=UUID("95000000-0000-4000-8000-000000000001"),
        snapshot_hash="a" * 64,
        fix_id=UUID("96000000-0000-4000-8000-000000000001"),
        patch="diff --git a/foreign.kt b/foreign.kt\n",
    )
    repository.tasks[created.id].request_document["patch_artifact_id"] = (
        repository.tasks[foreign.id].request_document["patch_artifact_id"]
    )

    with pytest.raises(SourceTaskUnavailable):
        await service.lease_next(agent_id=AGENT_ID)

    assert repository.tasks[created.id].state == "failed"
    assert patch_store.read_calls == [
        (
            TEAM_ID,
            ANALYSIS_ID,
            UUID(str(repository.tasks[foreign.id].request_document["patch_artifact_id"])),
            str(repository.tasks[created.id].request_document["patch_sha256"]),
        )
    ]
    assert "foreign.kt" not in repr(repository.tasks[created.id])


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
