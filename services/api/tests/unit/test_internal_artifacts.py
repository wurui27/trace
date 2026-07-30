from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid5

import pytest

from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.services.internal_artifacts import (
    InternalArtifactConflictError,
    InternalArtifactRecord,
    InternalArtifactUnavailableError,
    S3InternalArtifactSink,
    manifest_artifact_id,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
CAPTURE_ID = UUID("40000000-0000-4000-8000-000000000001")
ARTIFACT_ID = manifest_artifact_id(CAPTURE_ID)
EVIDENCE_ID = UUID("50000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
NAMESPACE = UUID("3fce5d93-30fd-5ac5-9f62-c1a89f78cd83")


def _payload(
    *,
    analysis_id: UUID = ANALYSIS_ID,
    capture_id: UUID = CAPTURE_ID,
) -> bytes:
    return MemoryCaptureManifest.model_validate(
        {
            "schema_version": "1.0",
            "analysis_id": analysis_id,
            "capture_id": capture_id,
            "phase": "single",
            "source": "manual_upload",
            "captured_at": None,
            "subject": {"package": "com.example.app", "android_sdk": 37},
            "artifacts": [{"artifact_id": EVIDENCE_ID, "role": "meminfo"}],
        }
    ).canonical_bytes()


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _record(payload: bytes, *, state: str = "pending") -> InternalArtifactRecord:
    return InternalArtifactRecord(
        artifact_id=ARTIFACT_ID,
        analysis_id=ANALYSIS_ID,
        artifact_kind="memory_capture_manifest",
        mime_type="application/json",
        size_bytes=len(payload),
        sha256_b64=_checksum(payload),
        object_key=(f"raw/analyses/{ANALYSIS_ID}/internal/memory_capture_manifest/{ARTIFACT_ID}"),
        state=state,
        expires_at=NOW + timedelta(days=30),
        version=2 if state == "finalized" else 1,
        version_id="immutable-version-1" if state == "finalized" else None,
    )


class FakeRepository:
    def __init__(self, record: InternalArtifactRecord) -> None:
        self.record = record
        self.events: list[tuple[str, dict[str, object]]] = []
        self.finalized = _record(_payload(), state="finalized")

    async def reserve(self, **kwargs: object) -> InternalArtifactRecord:
        self.events.append(("reserve", kwargs))
        return self.record

    async def finalize(self, **kwargs: object) -> InternalArtifactRecord | None:
        self.events.append(("finalize", kwargs))
        return self.finalized


class FakeBucketResolver:
    def __init__(self, *, resource_version: int = 1) -> None:
        self.resource_version = resource_version

    async def active_for_team(self, team_id: UUID) -> object:
        assert team_id == TEAM_ID
        return TenantBucket(
            team_id=TEAM_ID,
            bucket="private-team-bucket",
            resource_version=self.resource_version,
        )


class FakeS3Client:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.events: list[tuple[str, dict[str, object]]] = []
        self.put_response: object = {
            "VersionId": "immutable-version-1",
            "ChecksumSHA256": _checksum(payload),
        }
        self.head_response: object = {
            "VersionId": "immutable-version-1",
            "ChecksumSHA256": _checksum(payload),
            "ContentType": "application/json",
            "ContentLength": len(payload),
            "DeleteMarker": False,
        }
        self.get_response: object = {
            **self.head_response,  # type: ignore[arg-type]
            "Body": BytesIO(payload),
        }
        self.failure: Exception | None = None

    def put_object(self, **kwargs: object) -> object:
        self.events.append(("put", kwargs))
        if self.failure is not None:
            raise self.failure
        return self.put_response

    def head_object(self, **kwargs: object) -> object:
        self.events.append(("head", kwargs))
        return self.head_response

    def get_object(self, **kwargs: object) -> object:
        self.events.append(("get", kwargs))
        return self.get_response


def _sink(
    repository: FakeRepository,
    client: FakeS3Client,
    *,
    bucket_resolver: FakeBucketResolver | None = None,
) -> S3InternalArtifactSink:
    return S3InternalArtifactSink(
        repository=repository,
        bucket_resolver=bucket_resolver or FakeBucketResolver(),
        client=client,
        clock=lambda: NOW,
    )


def test_manifest_artifact_id_uses_the_fixed_namespace() -> None:
    assert ARTIFACT_ID == uuid5(NAMESPACE, str(CAPTURE_ID))


@pytest.mark.asyncio
async def test_context_and_bucket_resource_versions_must_match_before_io() -> None:
    payload = _payload()
    repository = FakeRepository(_record(payload))
    client = FakeS3Client(payload)

    with pytest.raises(InternalArtifactUnavailableError) as caught:
        await _sink(
            repository,
            client,
            bucket_resolver=FakeBucketResolver(resource_version=2),
        ).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            artifact_kind="memory_capture_manifest",
            payload=payload,
        )

    assert repository.events == []
    assert client.events == []
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_write_json_persists_verified_immutable_s3_version_before_finalize() -> None:
    payload = _payload()
    repository = FakeRepository(_record(payload))
    client = FakeS3Client(payload)

    artifact_id = await _sink(repository, client).write_json(
        team_id=TEAM_ID,
        expected_tenant_resource_version=1,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        artifact_kind="memory_capture_manifest",
        payload=payload,
    )

    assert artifact_id == ARTIFACT_ID
    assert [event for event, _ in repository.events] == ["reserve", "finalize"]
    assert [event for event, _ in client.events] == ["put", "head"]
    put = client.events[0][1]
    assert put == {
        "Bucket": "private-team-bucket",
        "Key": f"raw/analyses/{ANALYSIS_ID}/internal/memory_capture_manifest/{ARTIFACT_ID}",
        "Body": payload,
        "ContentType": "application/json",
        "ChecksumSHA256": _checksum(payload),
    }
    assert repository.events[1][1]["storage_version_id"] == "immutable-version-1"
    assert repository.events[0][1]["tenant"] is repository.events[1][1]["tenant"]


@pytest.mark.asyncio
async def test_exact_replay_reads_and_verifies_the_pinned_object_without_rewriting() -> None:
    payload = _payload()
    repository = FakeRepository(_record(payload, state="finalized"))
    client = FakeS3Client(payload)

    artifact_id = await _sink(repository, client).write_json(
        team_id=TEAM_ID,
        expected_tenant_resource_version=1,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        artifact_kind="memory_capture_manifest",
        payload=payload,
    )

    assert artifact_id == ARTIFACT_ID
    assert [event for event, _ in repository.events] == ["reserve"]
    assert [event for event, _ in client.events] == ["get"]
    assert client.events[0][1]["VersionId"] == "immutable-version-1"


@pytest.mark.asyncio
async def test_concurrent_same_bytes_replays_the_winning_immutable_version() -> None:
    payload = _payload()

    class ConcurrentRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__(_record(payload))
            self.reserve_count = 0

        async def reserve(self, **kwargs: object) -> InternalArtifactRecord:
            self.events.append(("reserve", kwargs))
            self.reserve_count += 1
            return (
                replace(
                    _record(payload, state="finalized"),
                    version_id="winning-version",
                )
                if self.reserve_count == 2
                else _record(payload)
            )

        async def finalize(self, **kwargs: object) -> InternalArtifactRecord | None:
            self.events.append(("finalize", kwargs))
            return None

    repository = ConcurrentRepository()
    client = FakeS3Client(payload)
    assert isinstance(client.get_response, dict)
    client.get_response["VersionId"] = "winning-version"

    artifact_id = await _sink(repository, client).write_json(
        team_id=TEAM_ID,
        expected_tenant_resource_version=1,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        artifact_kind="memory_capture_manifest",
        payload=payload,
    )

    assert artifact_id == ARTIFACT_ID
    assert [event for event, _ in repository.events] == ["reserve", "finalize", "reserve"]
    assert [event for event, _ in client.events] == ["put", "head", "get"]
    assert len({id(kwargs["tenant"]) for _, kwargs in repository.events}) == 1


@pytest.mark.asyncio
async def test_replay_with_different_bytes_is_a_redacted_idempotency_conflict() -> None:
    payload = _payload()
    marker = "private-team-bucket"
    repository = FakeRepository(_record(payload, state="finalized"))
    client = FakeS3Client(payload)
    different = payload.replace(b'"phase":"single"', b'"phase":"before"')

    with pytest.raises(InternalArtifactConflictError) as caught:
        await _sink(repository, client).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            artifact_kind="memory_capture_manifest",
            payload=different,
        )

    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert client.events == []
    assert [event for event, _ in repository.events] == ["reserve"]


@pytest.mark.parametrize(
    "head_change",
    [
        {"ChecksumSHA256": base64.b64encode(b"x" * 32).decode("ascii")},
        {"ContentLength": 1},
        {"ContentType": "text/plain"},
        {"VersionId": "different-version"},
        {"DeleteMarker": True},
    ],
)
@pytest.mark.asyncio
async def test_unverified_s3_receipt_never_finalizes(
    head_change: dict[str, object],
) -> None:
    payload = _payload()
    repository = FakeRepository(_record(payload))
    client = FakeS3Client(payload)
    assert isinstance(client.head_response, dict)
    client.head_response.update(head_change)

    with pytest.raises(InternalArtifactUnavailableError):
        await _sink(repository, client).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            artifact_kind="memory_capture_manifest",
            payload=payload,
        )

    assert [event for event, _ in repository.events] == ["reserve"]


@pytest.mark.asyncio
async def test_s3_failure_is_redacted_and_never_finalizes() -> None:
    payload = _payload()
    marker = "private-object-key-secret"
    repository = FakeRepository(_record(payload))
    client = FakeS3Client(payload)
    client.failure = RuntimeError(marker)

    with pytest.raises(InternalArtifactUnavailableError) as caught:
        await _sink(repository, client).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            artifact_kind="memory_capture_manifest",
            payload=payload,
        )

    assert [event for event, _ in repository.events] == ["reserve"]
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_manifest_identity_must_match_route_and_deterministic_artifact_id() -> None:
    payload = _payload(analysis_id=UUID(int=99))
    repository = FakeRepository(_record(payload))
    client = FakeS3Client(payload)

    with pytest.raises(InternalArtifactConflictError):
        await _sink(repository, client).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            artifact_kind="memory_capture_manifest",
            payload=payload,
        )

    assert repository.events == []
    assert client.events == []
