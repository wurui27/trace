from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import httpx
import pytest

from perfpilot_agent.capture import CaptureTaskRunner, ThermalReading
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import (
    InputAuthorizationResponse,
    UploadPartAuthorizationResponse,
    UploadSlotResponse,
)
from perfpilot_agent.security import TaskSnapshot
from perfpilot_agent.state import AgentRuntimeState
from perfpilot_agent.uploads import InputDownloader, MultipartUploader

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
_UPLOAD_NAMESPACE = UUID("78000000-0000-4000-8000-000000000001")


def _sha(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


class FakeDevice:
    def __init__(self) -> None:
        self.installed: bytes | None = None
        self.cleaned = False
        self.uninstalled = False

    async def adb_version(self) -> str:
        return "Android Debug Bridge version 1.0.41"

    async def thermal_reading(self) -> ThermalReading:
        return ThermalReading(temperature_c=30.0, thermal_status=0)

    async def install(self, apk: Path) -> None:
        self.installed = apk.read_bytes()

    async def capture_trace(self, *, output: Path, **kwargs: object) -> None:
        output.write_bytes(b"captured-perfetto-trace")

    async def collect_memory_samples(self, **kwargs: object) -> tuple[str, ...]:
        raise AssertionError("unexpected memory capture")

    async def cleanup(self) -> None:
        self.cleaned = True

    async def uninstall(self, package_name: str) -> None:
        self.uninstalled = True


class ObjectControl:
    def __init__(self, apk: bytes) -> None:
        self.apk = apk
        self.created_kinds: list[str] = []
        self.completed_kinds: list[str] = []
        self.slots: dict[UUID, UploadSlotResponse] = {}

    async def authorize_input(self, *, artifact_id: UUID, **kwargs: object):
        return InputAuthorizationResponse(
            schema_version="1.0",
            artifact_id=artifact_id,
            mime="application/vnd.android.package-archive",
            size=len(self.apk),
            sha256_b64=_sha(self.apk),
            download_url="https://objects.example/input.apk?signature=private",
            expires_at=NOW + timedelta(minutes=5),
        )

    async def create_upload(
        self,
        *,
        artifact_kind: str,
        mime: str,
        size: int,
        sha256_b64: str,
        **kwargs: object,
    ) -> UploadSlotResponse:
        self.created_kinds.append(artifact_kind)
        upload_id = uuid5(_UPLOAD_NAMESPACE, f"upload:{artifact_kind}")
        slot = UploadSlotResponse(
            schema_version="1.0",
            artifact_id=uuid5(_UPLOAD_NAMESPACE, f"artifact:{artifact_kind}"),
            upload_id=upload_id,
            artifact_kind=artifact_kind,
            mime=mime,
            size=size,
            sha256_b64=sha256_b64,
            part_size_bytes=size,
            part_count=1,
            state="pending",
            expires_at=NOW + timedelta(minutes=15),
            finalized_at=None,
        )
        self.slots[upload_id] = slot
        return slot

    async def authorize_upload_part(
        self,
        *,
        upload_id: UUID,
        part_number: int,
        **kwargs: object,
    ) -> UploadPartAuthorizationResponse:
        return UploadPartAuthorizationResponse(
            schema_version="1.0",
            upload_id=upload_id,
            part_number=part_number,
            put_url=f"https://objects.example/parts/{upload_id}/{part_number}",
            required_headers={},
            expires_at=NOW + timedelta(minutes=15),
        )

    async def complete_upload(
        self,
        *,
        upload_id: UUID,
        **kwargs: object,
    ) -> UploadSlotResponse:
        slot = self.slots[upload_id]
        self.completed_kinds.append(slot.artifact_kind)
        return slot.model_copy(update={"state": "finalized", "finalized_at": NOW})


@pytest.mark.asyncio
async def test_verified_apk_capture_upload_and_manifest_round_trip(
    tmp_path,
    task_claims,
) -> None:
    apk = b"verified-agent-input-apk"
    claims = dict(task_claims)
    claims["input_artifacts"] = [
        {
            **task_claims["input_artifacts"][0],
            "size": len(apk),
            "sha256_b64": _sha(apk),
        }
    ]
    task = TaskSnapshot.model_validate(claims)
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace_root = tmp_path / "work"
    workspace_root.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace_root,
    )
    control = ObjectControl(apk)
    uploaded: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/input.apk":
            return httpx.Response(200, request=request, content=apk)
        if request.method == "PUT" and request.url.path.startswith("/parts/"):
            uploaded.append(request.content)
            return httpx.Response(200, request=request, headers={"etag": '"confirmed"'})
        raise AssertionError("unexpected object request")

    device = FakeDevice()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = CaptureTaskRunner(
            config=config,
            adb_binary=tmp_path / "adb",
            control=control,
            state=AgentRuntimeState(),
            redactor=None,
            device_factory=lambda **kwargs: device,
            downloader_factory=lambda **kwargs: InputDownloader(
                control=control,
                http_client=client,
                **kwargs,
            ),
            uploader_factory=lambda **kwargs: MultipartUploader(
                control=control,
                http_client=client,
                **kwargs,
            ),
            sleep=lambda _: _done(),
            clock=lambda: NOW,
        )
        execution = await runner.start(task, serial="device-under-test")
        outcome = await execution.wait()
        await execution.finalize()

    assert device.installed == apk
    assert device.cleaned is True
    assert device.uninstalled is True
    assert control.created_kinds == ["startup_trace", "agent_log"]
    assert control.completed_kinds == ["startup_trace", "agent_log"]
    assert len(uploaded) == 2
    assert uploaded[0] == b"captured-perfetto-trace"
    assert b"schema_version=1.0" in uploaded[1]
    assert outcome.manifest["state"] == "completed"
    assert [item["kind"] for item in outcome.manifest["artifacts"]] == [
        "startup_trace",
        "agent_log",
    ]
    assert not (workspace_root / str(task.execution_id)).exists()


async def _done() -> None:
    return None
