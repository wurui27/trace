from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from perfpilot_agent.control_client import (
    UploadPartAuthorizationResponse,
    UploadSlotResponse,
)
from perfpilot_agent.uploads import (
    ArtifactDescriptor,
    ArtifactTransferError,
    MultipartUploader,
)

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("76000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("77000000-0000-4000-8000-000000000001")


def _sha(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


class ResumeControl:
    def __init__(self, descriptor: ArtifactDescriptor) -> None:
        self.descriptor = descriptor
        self.authorized_parts: list[int] = []
        self.completed_parts: list[int] = []

    async def create_upload(self, **kwargs: object) -> UploadSlotResponse:
        return UploadSlotResponse(
            schema_version="1.0",
            artifact_id=ARTIFACT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind=self.descriptor.kind,
            mime=self.descriptor.mime,
            size=self.descriptor.size,
            sha256_b64=self.descriptor.sha256_b64,
            part_size_bytes=2,
            part_count=4,
            state="pending",
            expires_at=NOW + timedelta(minutes=15),
            finalized_at=None,
        )

    async def authorize_upload_part(
        self,
        *,
        part_number: int,
        **kwargs: object,
    ) -> UploadPartAuthorizationResponse:
        self.authorized_parts.append(part_number)
        return UploadPartAuthorizationResponse(
            schema_version="1.0",
            upload_id=UPLOAD_ID,
            part_number=part_number,
            put_url=f"https://objects.example/parts/{part_number}",
            required_headers={},
            expires_at=NOW + timedelta(minutes=15),
        )

    async def complete_upload(self, *, parts, **kwargs: object) -> UploadSlotResponse:
        self.completed_parts = [part.part_number for part in parts]
        return (await self.create_upload()).model_copy(
            update={"state": "finalized", "finalized_at": NOW}
        )


@pytest.mark.asyncio
async def test_process_restart_resumes_only_after_durable_confirmed_parts(tmp_path) -> None:
    content = b"abcdefgh"
    source = tmp_path / "startup.perfetto-trace"
    source.write_bytes(content)
    descriptor = ArtifactDescriptor(
        kind="startup_trace",
        mime="application/x-perfetto-trace",
        path=source,
        size=len(content),
        sha256_b64=_sha(content),
    )
    checkpoint = tmp_path / "upload-state.json"
    control = ResumeControl(descriptor)

    def interrupted(request: httpx.Request) -> httpx.Response:
        part = int(request.url.path.rsplit("/", 1)[-1])
        if part == 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, request=request, headers={"etag": f'"etag-{part}"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted)) as client:
        uploader = MultipartUploader(
            control=control,
            checkpoint_path=checkpoint,
            http_client=client,
        )
        with pytest.raises(ArtifactTransferError):
            await uploader.upload(
                execution_id=EXECUTION_ID,
                lease_version=1,
                descriptor=descriptor,
            )

    resumed_bodies: list[bytes] = []

    def resumed(request: httpx.Request) -> httpx.Response:
        resumed_bodies.append(request.content)
        part = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, request=request, headers={"etag": f'"etag-{part}"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(resumed)) as client:
        restarted = MultipartUploader(
            control=control,
            checkpoint_path=checkpoint,
            http_client=client,
        )
        await restarted.upload(
            execution_id=EXECUTION_ID,
            lease_version=1,
            descriptor=descriptor,
        )

    assert control.authorized_parts == [1, 2, 3, 3, 4]
    assert resumed_bodies == [b"ef", b"gh"]
    assert control.completed_parts == [1, 2, 3, 4]
    assert not checkpoint.exists()
