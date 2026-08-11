from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest

from perfpilot_api.services.source_artifacts import (
    SourceArtifactService,
    SourceArtifactUnavailableError,
    source_artifact_key,
)
from perfpilot_api.services.source_tasks import SourceTaskView


TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")


def _task() -> SourceTaskView:
    now = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
    return SourceTaskView(
        id=EXECUTION_ID,
        execution_id=EXECUTION_ID,
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        agent_id=UUID("71000000-0000-4000-8000-000000000001"),
        workspace_id=WORKSPACE_ID,
        task_type="source_context",
        state="running",
        lease_version=1,
        expires_at=now,
        created_at=now,
    )


def _completion() -> dict[str, object]:
    content = "class Startup\n"
    return {
        "schema_version": "1.0",
        "task_type": "source_context",
        "execution_id": str(EXECUTION_ID),
        "analysis_id": str(ANALYSIS_ID),
        "team_id": str(TEAM_ID),
        "agent_id": "71000000-0000-4000-8000-000000000001",
        "workspace_id": str(WORKSPACE_ID),
        "lease_version": 1,
        "state": "completed",
        "result": {
            "snapshot_id": "95000000-0000-4000-8000-000000000001",
            "snapshot_hash": "a" * 64,
            "git_head": "b" * 40,
            "tracked_dirty_count": 0,
            "fragments": [
                {
                    "source_ref_id": "97000000-0000-4000-8000-000000000001",
                    "relative_path": "app/src/Startup.kt",
                    "language": "kotlin",
                    "symbol": "demo.Startup.init",
                    "start_line": 1,
                    "end_line": 1,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "snapshot_hash": "a" * 64,
                    "finding_ids": [],
                    "evidence_ids": [],
                    "rule_ids": [],
                    "match_signals": ["trace_symbol"],
                }
            ],
            "exclusions": [],
            "truncated": False,
        },
        "signature_b64": "A" * 86 + "==",
    }


@pytest.mark.asyncio
async def test_completion_is_immutable_versioned_and_tenant_private() -> None:
    service = SourceArtifactService.in_memory()
    document = _completion()
    checksum = hashlib.sha256(service.canonical_bytes(document)).hexdigest()

    stored = await service.record_completion(
        task=_task(), document=document, checksum=checksum, now=datetime.now(UTC)
    )
    loaded = await service.read_context(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=stored.artifact_id,
        expected_checksum=checksum,
        direct_identifiers=("demo.Startup.init",),
    )

    assert loaded["match_summary"] == "strong"
    record = service.record(stored.artifact_id)
    assert record.object_key == source_artifact_key(
        ANALYSIS_ID, stored.artifact_id, "source_context"
    )
    assert record.version_id
    assert record.mime_type == "application/json"
    assert record.size_bytes == len(service.canonical_bytes(document))
    assert "/internal/source-context/" in record.object_key

    persisted = await service.persist_validated_context(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        source_artifact_id=stored.artifact_id,
        context=loaded,
        now=datetime.now(UTC),
    )
    validated_record = service.record(persisted.artifact_id)
    assert validated_record.kind == "source_context_validated"
    assert persisted.artifact_id != stored.artifact_id
    assert validated_record.object_key != record.object_key


@pytest.mark.asyncio
async def test_cross_tenant_context_read_is_rejected_without_coordinates() -> None:
    service = SourceArtifactService.in_memory()
    document = _completion()
    checksum = hashlib.sha256(service.canonical_bytes(document)).hexdigest()
    stored = await service.record_completion(
        task=_task(), document=document, checksum=checksum, now=datetime.now(UTC)
    )

    with pytest.raises(SourceArtifactUnavailableError) as raised:
        await service.read_context(
            team_id=UUID("10000000-0000-4000-8000-000000000099"),
            analysis_id=ANALYSIS_ID,
            artifact_id=stored.artifact_id,
            expected_checksum=checksum,
        )

    assert "raw/" not in str(raised.value)
