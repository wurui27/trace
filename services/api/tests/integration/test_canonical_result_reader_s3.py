from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber

from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.services.canonical_result_reader import CanonicalResultReader
from perfpilot_api.services.engine_result_artifacts import EngineResultArtifactRecord
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")
ARTIFACT_ID = result_artifact_id(EXECUTION_ID)
TENANT = TenantBucket(TEAM_ID, "tenant-private-a", 7)


@dataclass
class _Execution:
    id: UUID = EXECUTION_ID
    team_id: UUID = TEAM_ID
    analysis_id: UUID = ANALYSIS_ID
    raw_result_artifact_id: UUID = ARTIFACT_ID
    tenant_resource_version: int = 7
    engine_id: str = "smartperfetto"
    state: str = "completed"
    adapter_version: str = "1.0.0"
    engine_commit_sha: str = "1" * 40
    engine_image_digest: str = "sha256:" + "2" * 64
    attempt_number: int = 1


class _Resolver:
    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        assert team_id == TEAM_ID
        return TENANT


class _Repository:
    def __init__(self, record: EngineResultArtifactRecord) -> None:
        self.record = record

    async def require_resource_version(self, tenant: TenantBucket) -> None:
        assert tenant == TENANT

    async def reload(self, **_kwargs: object) -> EngineResultArtifactRecord:
        return self.record


def test_reader_uses_real_s3_client_exact_versioned_get() -> None:
    execution = _Execution()
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=EXECUTION_ID,
            expected_execution_version=1,
            tenant_resource_version=7,
            artifact_id=ARTIFACT_ID,
            engine_id="smartperfetto",
            adapter_version=execution.adapter_version,
            engine_commit_sha=execution.engine_commit_sha,
            engine_image_digest=execution.engine_image_digest,
            attempt_number=execution.attempt_number,
            input_manifest_hash="3" * 64,
            config_hash="4" * 64,
            result=EngineResult(
                contract="workspace-agent-v1",
                state="completed",
                payload={"reportId": "report-1", "report": {"reportId": "report-1"}},
            ),
        )
    )
    key = f"raw/analyses/{ANALYSIS_ID}/internal/engine-results/{ARTIFACT_ID}.json"
    record = EngineResultArtifactRecord(
        artifact_id=ARTIFACT_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=ARTIFACT_ID,
        idempotency_key=f"internal:engine_result:{EXECUTION_ID}",
        request_hash=canonical.request_hash_hex,
        artifact_kind="engine_result",
        mime_type="application/json",
        size_bytes=len(canonical.canonical_bytes),
        sha256_b64=canonical.checksum_sha256_b64,
        object_key=key,
        state="finalized",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        version=2,
        version_id="immutable-engine-result-v1",
    )
    client = boto3.client(
        "s3", region_name="us-east-1", endpoint_url="https://s3.example.test", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {
                "Body": StreamingBody(io.BytesIO(canonical.canonical_bytes), len(canonical.canonical_bytes)),
                "ContentLength": len(canonical.canonical_bytes),
                "ContentType": "application/json",
                "ChecksumSHA256": canonical.checksum_sha256_b64,
                "VersionId": "immutable-engine-result-v1",
                "DeleteMarker": False,
            },
            {"Bucket": TENANT.bucket, "Key": key, "VersionId": "immutable-engine-result-v1", "ChecksumMode": "ENABLED"},
        )
        loaded = asyncio.run(
            CanonicalResultReader(
                artifact_repository=_Repository(record),  # type: ignore[arg-type]
                bucket_resolver=_Resolver(),  # type: ignore[arg-type]
                client=client,
            ).read(execution)
        )
    assert loaded.canonical_bytes == canonical.canonical_bytes
