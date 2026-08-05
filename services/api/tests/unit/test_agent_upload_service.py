from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.services.agent_uploads import (
    AgentUploadInvalidRequest,
    AgentUploadMismatch,
    AgentUploadNotFound,
    AgentUploadService,
    InMemoryAgentUploadRepository,
)
from perfpilot_api.services.agent_tasks import AgentExecutionAccess
from perfpilot_api.services.uploads import TenantBucket
from perfpilot_api.storage.base import (
    CompletedMultipart,
    MultipartCreation,
    MultipartPart,
    MultipartPartAuthorization,
    ObjectLocation,
    StoredObjectMetadata,
)

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
OTHER_AGENT_ID = UUID("71000000-0000-4000-8000-000000000002")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("76000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("77000000-0000-4000-8000-000000000001")
CHECKSUM = base64.b64encode(b"a" * 32).decode("ascii")


class FixedExecutionAuthorizer:
    async def authorize_execution(self, **kwargs: object) -> AgentExecutionAccess:
        if (
            kwargs["agent_id"] != AGENT_ID
            or kwargs["execution_id"] != EXECUTION_ID
            or kwargs["lease_version"] != 1
            or kwargs["now"] >= NOW + timedelta(seconds=60)
        ):
            raise AgentUploadNotFound
        return AgentExecutionAccess(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            lease_expires_at=NOW + timedelta(seconds=60),
            allowed_uploads=(
                "startup_trace",
                "scroll_trace",
                "memory_evidence",
                "agent_log",
            ),
        )


class FixedBucketResolver:
    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        assert team_id == TEAM_ID
        return TenantBucket(team_id=TEAM_ID, bucket="private-team-bucket", resource_version=1)


class RecordingMultipartStore:
    def __init__(self) -> None:
        self.complete_calls = 0
        self.abort_calls = 0
        self.part_calls: list[int] = []
        self.metadata = StoredObjectMetadata(
            location=ObjectLocation(
                bucket="private-team-bucket",
                key=f"raw/analyses/{ANALYSIS_ID}/agent/{EXECUTION_ID}/startup_trace/{UPLOAD_ID}",
                version_id="immutable-version",
            ),
            checksum_sha256_b64=CHECKSUM,
            content_type="application/x-perfetto-trace",
            size_bytes=512 * 1024 * 1024,
        )

    async def create_multipart(self, **kwargs: object) -> MultipartCreation:
        return MultipartCreation(
            location=kwargs["location"],
            storage_upload_id="private-storage-upload-id",
        )

    async def authorize_part(self, **kwargs: object) -> MultipartPartAuthorization:
        part_number = int(kwargs["part_number"])
        self.part_calls.append(part_number)
        return MultipartPartAuthorization(
            part_number=part_number,
            url="https://objects.example/private-signed-part",
            required_headers={},
            expires_in_seconds=int(kwargs["expires_in_seconds"]),
        )

    async def complete_multipart(self, **kwargs: object) -> CompletedMultipart:
        self.complete_calls += 1
        return CompletedMultipart(location=kwargs["location"])

    async def abort_multipart(self, **kwargs: object) -> None:
        self.abort_calls += 1

    async def head(self, **kwargs: object) -> StoredObjectMetadata:
        return self.metadata


def _service() -> tuple[AgentUploadService, RecordingMultipartStore]:
    repository = InMemoryAgentUploadRepository()
    store = RecordingMultipartStore()
    return (
        AgentUploadService(
            repository=repository,
            artifact_store=store,
            bucket_resolver=FixedBucketResolver(),
            execution_authorizer=FixedExecutionAuthorizer(),
            clock=lambda: NOW,
            uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
        ),
        store,
    )


async def _create(service: AgentUploadService):
    return await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=512 * 1024 * 1024,
        sha256_b64=CHECKSUM,
    )


@pytest.mark.asyncio
async def test_512_mib_output_uses_eight_64_mib_parts() -> None:
    service, _store = _service()

    upload = await _create(service)

    assert upload.artifact_id == ARTIFACT_ID
    assert upload.upload_id == UPLOAD_ID
    assert upload.part_size_bytes == 64 * 1024 * 1024
    assert upload.part_count == 8
    assert "private-storage-upload-id" not in repr(upload)


@pytest.mark.asyncio
async def test_part_authorization_is_lease_and_part_bound_for_fifteen_minutes() -> None:
    service, store = _service()
    await _create(service)

    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        upload_id=UPLOAD_ID,
        part_number=8,
    )

    assert part.part_number == 8
    assert part.expires_at == NOW + timedelta(minutes=15)
    assert store.part_calls == [8]
    assert "private-signed-part" not in repr(part)
    with pytest.raises(AgentUploadInvalidRequest):
        await service.authorize_part(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            upload_id=UPLOAD_ID,
            part_number=9,
        )
    with pytest.raises(AgentUploadNotFound):
        await service.authorize_part(
            agent_id=OTHER_AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            upload_id=UPLOAD_ID,
            part_number=1,
        )


@pytest.mark.asyncio
async def test_complete_requires_exact_contiguous_parts_and_is_exactly_once() -> None:
    service, store = _service()
    await _create(service)
    parts = tuple(MultipartPart(part_number=index, etag=f'"etag-{index}"') for index in range(1, 9))

    completed = await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        upload_id=UPLOAD_ID,
        parts=parts,
    )
    repeated = await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=1,
        upload_id=UPLOAD_ID,
        parts=parts,
    )

    assert completed == repeated
    assert completed.state == "finalized"
    assert completed.artifact_id == ARTIFACT_ID
    assert store.complete_calls == 1


@pytest.mark.asyncio
async def test_complete_rejects_non_contiguous_parts_before_storage_call() -> None:
    service, store = _service()
    await _create(service)

    with pytest.raises(AgentUploadInvalidRequest):
        await service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            upload_id=UPLOAD_ID,
            parts=(
                MultipartPart(part_number=1, etag='"etag-1"'),
                MultipartPart(part_number=3, etag='"etag-3"'),
            ),
        )

    assert store.complete_calls == 0


@pytest.mark.asyncio
async def test_complete_rejects_final_object_checksum_mismatch() -> None:
    service, store = _service()
    await _create(service)
    store.metadata = StoredObjectMetadata(
        location=store.metadata.location,
        checksum_sha256_b64=base64.b64encode(b"b" * 32).decode("ascii"),
        content_type=store.metadata.content_type,
        size_bytes=store.metadata.size_bytes,
    )

    with pytest.raises(AgentUploadMismatch):
        await service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=1,
            upload_id=UPLOAD_ID,
            parts=tuple(
                MultipartPart(part_number=index, etag=f'"etag-{index}"') for index in range(1, 9)
            ),
        )
