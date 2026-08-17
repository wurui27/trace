from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import perfpilot_agent.uploads as uploads_module

from perfpilot_agent.control_client import (
    InputAuthorizationResponse,
    UploadPartAuthorizationResponse,
    UploadSlotResponse,
)
from perfpilot_agent.security import TaskInputArtifact
from perfpilot_agent.uploads import (
    ArtifactDescriptor,
    InputDownloader,
    MultipartUploader,
    UploadCheckpoint,
    UploadCheckpointPart,
    save_upload_checkpoint,
)

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
INPUT_ID = UUID("50000000-0000-4000-8000-000000000001")
OUTPUT_ID = UUID("76000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("77000000-0000-4000-8000-000000000001")


def _sha(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/local/v1/agent-inputs/private-token",
        "http://10.166.0.125:8000/local/v1/agent-inputs/private-token",
        "https://objects.example/local/v1/agent-inputs/private-token",
    ],
)
def test_agent_accepts_https_or_private_http_signed_artifact_urls(url: str) -> None:
    response = InputAuthorizationResponse(
        schema_version="1.0",
        artifact_id=INPUT_ID,
        mime="application/vnd.android.package-archive",
        size=1,
        sha256_b64=_sha(b"x"),
        download_url=url,
        expires_at=NOW + timedelta(minutes=5),
    )

    assert response.download_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example.test/local/v1/agent-inputs/private-token",
        "http://8.8.8.8/local/v1/agent-inputs/private-token",
        "http://user:password@10.166.0.125/local/v1/agent-inputs/private-token",
        "http://10.166.0.125/local/v1/agent-inputs/private-token#fragment",
    ],
)
def test_agent_rejects_unsafe_signed_artifact_urls(url: str) -> None:
    with pytest.raises(ValueError, match="signed URL is invalid"):
        UploadPartAuthorizationResponse(
            schema_version="1.0",
            upload_id=UPLOAD_ID,
            part_number=1,
            put_url=url,
            required_headers={},
            expires_at=NOW + timedelta(minutes=5),
        )


def test_default_artifact_transfer_clients_trust_configured_ca(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_bundle = tmp_path / "private-ca.crt"
    ca_bundle.write_text("private test CA", encoding="utf-8")
    observed: list[dict[str, object]] = []

    class ObservedAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            observed.append(kwargs)

    monkeypatch.setattr(uploads_module.httpx, "AsyncClient", ObservedAsyncClient)

    InputDownloader(
        control=object(),
        workspace_root=tmp_path,
        ca_bundle=ca_bundle,
    )
    MultipartUploader(
        control=object(),
        checkpoint_path=tmp_path / "upload-state.json",
        ca_bundle=ca_bundle,
    )

    assert [item["verify"] for item in observed] == [str(ca_bundle), str(ca_bundle)]
    assert all(item["trust_env"] is False for item in observed)


class InputControl:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def authorize_input(self, *, execution_id, lease_version, artifact_id):
        return InputAuthorizationResponse(
            schema_version="1.0",
            artifact_id=artifact_id,
            mime="application/vnd.android.package-archive",
            size=len(self.payload),
            sha256_b64=_sha(self.payload),
            download_url="https://objects.example/input.apk?signature=private",
            expires_at=NOW + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_input_download_is_size_and_checksum_verified_before_rename(tmp_path) -> None:
    payload = b"verified-apk"
    artifact = TaskInputArtifact(
        artifact_id=INPUT_ID,
        kind="apk",
        mime="application/vnd.android.package-archive",
        size=len(payload),
        sha256_b64=_sha(payload),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/input.apk"
        return httpx.Response(200, request=request, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        downloader = InputDownloader(
            control=InputControl(payload),
            http_client=http_client,
            workspace_root=tmp_path,
        )
        target = tmp_path / str(EXECUTION_ID) / "input.apk"
        downloaded = await downloader.download(
            execution_id=EXECUTION_ID,
            lease_version=1,
            artifact=artifact,
            target=target,
        )

    assert downloaded == target
    assert target.read_bytes() == payload
    assert not target.with_suffix(".apk.part").exists()


class UploadControl:
    def __init__(self, descriptor: ArtifactDescriptor) -> None:
        self.descriptor = descriptor
        self.authorized_parts: list[int] = []
        self.completed_parts = ()

    async def create_upload(self, **kwargs):
        return UploadSlotResponse(
            schema_version="1.0",
            artifact_id=OUTPUT_ID,
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

    async def authorize_upload_part(self, *, part_number, **kwargs):
        self.authorized_parts.append(part_number)
        return UploadPartAuthorizationResponse(
            schema_version="1.0",
            upload_id=UPLOAD_ID,
            part_number=part_number,
            put_url=f"https://objects.example/parts/{part_number}?signature=private",
            required_headers={},
            expires_at=NOW + timedelta(minutes=15),
        )

    async def complete_upload(self, *, parts, **kwargs):
        self.completed_parts = parts
        return UploadSlotResponse(
            schema_version="1.0",
            artifact_id=OUTPUT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind=self.descriptor.kind,
            mime=self.descriptor.mime,
            size=self.descriptor.size,
            sha256_b64=self.descriptor.sha256_b64,
            part_size_bytes=2,
            part_count=4,
            state="finalized",
            expires_at=NOW + timedelta(days=30),
            finalized_at=NOW,
        )


@pytest.mark.asyncio
async def test_upload_resumes_from_locally_confirmed_parts(tmp_path) -> None:
    content = b"abcdefgh"
    artifact_path = tmp_path / "startup.perfetto-trace"
    artifact_path.write_bytes(content)
    descriptor = ArtifactDescriptor(
        kind="startup_trace",
        mime="application/x-perfetto-trace",
        path=artifact_path,
        size=len(content),
        sha256_b64=_sha(content),
    )
    checkpoint_path = tmp_path / "upload-state.json"
    save_upload_checkpoint(
        checkpoint_path,
        UploadCheckpoint(
            schema_version="1.0",
            upload_id=UPLOAD_ID,
            artifact_id=OUTPUT_ID,
            artifact_kind=descriptor.kind,
            size=descriptor.size,
            sha256_b64=descriptor.sha256_b64,
            part_size_bytes=2,
            part_count=4,
            parts=(
                UploadCheckpointPart(part_number=1, etag='"etag-1"'),
                UploadCheckpointPart(part_number=2, etag='"etag-2"'),
            ),
        ),
    )
    control = UploadControl(descriptor)
    uploaded_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploaded_bodies.append(request.content)
        part = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, request=request, headers={"etag": f'"etag-{part}"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        uploader = MultipartUploader(
            control=control,
            http_client=http_client,
            checkpoint_path=checkpoint_path,
        )
        uploaded = await uploader.upload(
            execution_id=EXECUTION_ID,
            lease_version=1,
            descriptor=descriptor,
        )

    assert control.authorized_parts == [3, 4]
    assert uploaded_bodies == [b"ef", b"gh"]
    assert [part.part_number for part in control.completed_parts] == [1, 2, 3, 4]
    assert uploaded.artifact_id == OUTPUT_ID
    assert not checkpoint_path.exists()
