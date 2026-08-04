from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.services.synthesis_artifacts import (
    S3SynthesisArtifactStore,
    SynthesisArtifactConflictError,
    SynthesisArtifactRecord,
    SynthesisArtifactWrite,
    projection_artifact_id,
    synthesis_artifact_id,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
CANONICAL_ID = UUID("93000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("94000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
TENANT = TenantBucket(team_id=TEAM_ID, bucket="private-team", resource_version=7)


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def test_artifact_identities_are_stable_and_content_bound() -> None:
    payload = canonical_json_bytes({"schema_version": "1.0"})
    checksum = _checksum(payload)
    assert projection_artifact_id(CANONICAL_ID, "smartperfetto-normalizer-1") == projection_artifact_id(CANONICAL_ID, "smartperfetto-normalizer-1")
    assert synthesis_artifact_id(EXECUTION_ID, checksum) == synthesis_artifact_id(EXECUTION_ID, checksum)
    assert synthesis_artifact_id(EXECUTION_ID, checksum) != synthesis_artifact_id(EXECUTION_ID, _checksum(b"different"))


def _write() -> SynthesisArtifactWrite:
    payload = canonical_json_bytes({"schema_version": "1.0", "value": "safe"})
    return SynthesisArtifactWrite(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        tenant_resource_version=7,
        artifact_id=projection_artifact_id(CANONICAL_ID, "smartperfetto-normalizer-1"),
        kind="ai_projection",
        canonical_bytes=payload,
        sha256_b64=_checksum(payload),
    )


class Repository:
    def __init__(self, record: SynthesisArtifactRecord) -> None:
        self.record = record
        self.events: list[str] = []

    async def reserve(self, **_kwargs: object) -> SynthesisArtifactRecord:
        self.events.append("reserve")
        return self.record

    async def require_resource_version(self, _tenant: TenantBucket) -> None:
        self.events.append("fence")

    async def finalize(self, **kwargs: object) -> SynthesisArtifactRecord | None:
        self.events.append("finalize")
        return replace(self.record, state="finalized", version=2, version_id=kwargs["storage_version_id"])

    async def reload(self, **_kwargs: object) -> SynthesisArtifactRecord:
        self.events.append("reload")
        return self.record


class Resolver:
    async def active_for_team(self, _team_id: UUID) -> TenantBucket:
        return TENANT


class Body:
    def __init__(self, value: bytes) -> None:
        self.value = value
    def read(self, _amount: int) -> bytes:
        return self.value
    def close(self) -> None:
        return None


class Client:
    def __init__(self, request: SynthesisArtifactWrite) -> None:
        self.request = request
        self.events: list[str] = []
    def put_object(self, **_kwargs: object) -> dict[str, object]:
        self.events.append("put")
        return {"VersionId": "v1", "ChecksumSHA256": self.request.sha256_b64}
    def head_object(self, **_kwargs: object) -> dict[str, object]:
        self.events.append("head")
        return {"VersionId": "v1", "ChecksumSHA256": self.request.sha256_b64, "ContentType": "application/json", "ContentLength": len(self.request.canonical_bytes), "DeleteMarker": False}
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        self.events.append("get")
        return {"VersionId": "v1", "ChecksumSHA256": self.request.sha256_b64, "ContentType": "application/json", "ContentLength": len(self.request.canonical_bytes), "DeleteMarker": False, "Body": Body(self.request.canonical_bytes)}


def _record(request: SynthesisArtifactWrite) -> SynthesisArtifactRecord:
    return SynthesisArtifactRecord(artifact_id=request.artifact_id, analysis_id=request.analysis_id, artifact_kind=request.kind, mime_type="application/json", size_bytes=len(request.canonical_bytes), sha256_b64=request.sha256_b64, object_key=f"raw/analyses/{ANALYSIS_ID}/internal/ai-projection/{request.artifact_id}.json", idempotency_key=f"internal:ai_projection:{request.artifact_id}", state="pending", expires_at=NOW + timedelta(days=30), version=1, version_id=None)


@pytest.mark.asyncio
async def test_store_reserves_fences_finalizes_and_returns_no_storage_coordinates() -> None:
    request = _write()
    repository = Repository(_record(request))
    result = await S3SynthesisArtifactStore(repository=repository, bucket_resolver=Resolver(), client=Client(request), clock=lambda: NOW).write(request)
    assert result.artifact_id == request.artifact_id
    assert result.sha256_b64 == request.sha256_b64
    assert not hasattr(result, "object_key")
    assert not hasattr(result, "version_id")
    assert repository.events == ["reserve", "fence", "fence", "fence", "finalize", "fence"]


@pytest.mark.asyncio
async def test_store_fails_closed_when_reserved_content_differs() -> None:
    request = _write()
    record = _record(request)
    repository = Repository(replace(record, sha256_b64=base64.b64encode(b"d" * 32).decode("ascii")))
    with pytest.raises(SynthesisArtifactConflictError):
        await S3SynthesisArtifactStore(repository=repository, bucket_resolver=Resolver(), client=Client(request), clock=lambda: NOW).write(request)
