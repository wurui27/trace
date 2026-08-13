from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest

from perfpilot_api.local_agent_artifacts import LocalAgentArtifactService
from perfpilot_api.services.agent_tasks import AgentExecutionAccess, StaleLeaseVersion
from perfpilot_api.services.agent_uploads import (
    AgentUploadInvalidRequest,
    AgentUploadMismatch,
    AgentUploadNotFound,
    AgentUploadStaleLease,
    AgentUploadUnavailable,
)
from perfpilot_api.storage.base import MultipartPart


NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
INPUT_ID = UUID("50000000-0000-4000-8000-000000000001")
INPUT_UPLOAD_ID = UUID("51000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("76000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("77000000-0000-4000-8000-000000000001")


class FixedExecutionAuthorizer:
    def __init__(self) -> None:
        self.active = True

    async def authorize_execution(self, **kwargs: object) -> AgentExecutionAccess:
        if kwargs["lease_version"] != 3:
            raise StaleLeaseVersion
        if not self.active:
            from perfpilot_api.services.agent_tasks import AgentTaskNotFound

            raise AgentTaskNotFound
        return AgentExecutionAccess(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            lease_expires_at=NOW + timedelta(minutes=5),
            allowed_uploads=("startup_trace", "scroll_trace", "agent_log"),
            scenario_types=("startup", "scroll"),
            input_artifact_ids=(INPUT_ID,),
        )


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _service(tmp_path: Path) -> LocalAgentArtifactService:
    return LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=FixedExecutionAuthorizer(),
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
        token_source=iter(("input-grant", "part-grant")).__next__,
    )


def _input_path(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "teams"
        / str(TEAM_ID)
        / "analyses"
        / str(ANALYSIS_ID)
        / "uploads"
        / f"{INPUT_UPLOAD_ID}.bin"
    )


def _completed_path(tmp_path: Path, artifact_id: UUID, kind: str = "startup_trace") -> Path:
    return (
        tmp_path
        / "teams"
        / str(TEAM_ID)
        / "analyses"
        / str(ANALYSIS_ID)
        / "agent-artifacts"
        / "completed"
        / f"{kind}-{artifact_id}.bin"
    )


@pytest.mark.asyncio
async def test_agent_input_get_is_private_and_bound_to_active_lease(tmp_path: Path) -> None:
    payload = b"private apk bytes"
    source = _input_path(tmp_path)
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(payload)
    source.chmod(0o600)
    service = _service(tmp_path)
    service.register_input(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=INPUT_ID,
        upload_id=INPUT_UPLOAD_ID,
        mime="application/vnd.android.package-archive",
        size=len(payload),
        sha256_b64=_checksum(payload),
    )

    slot = await service.authorize_input(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_id=INPUT_ID,
    )
    opened = await service.open_input(urlsplit(slot.url).path.rsplit("/", 1)[-1])

    assert b"".join(opened.body) == payload
    assert opened.mime == "application/vnd.android.package-archive"
    assert opened.size == len(payload)
    assert "input-grant" not in repr(slot)


@pytest.mark.asyncio
async def test_agent_input_get_revalidates_active_lease_after_grant(tmp_path: Path) -> None:
    payload = b"private apk bytes"
    source = _input_path(tmp_path)
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(payload)
    source.chmod(0o600)
    authorizer = FixedExecutionAuthorizer()
    service = LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=authorizer,
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
        token_source=lambda: "revocable-input-grant",
    )
    service.register_input(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=INPUT_ID,
        upload_id=INPUT_UPLOAD_ID,
        mime="application/vnd.android.package-archive",
        size=len(payload),
        sha256_b64=_checksum(payload),
    )
    slot = await service.authorize_input(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_id=INPUT_ID,
    )
    authorizer.active = False

    with pytest.raises(AgentUploadNotFound, match="execution was not found"):
        await service.open_input(urlsplit(slot.url).path.rsplit("/", 1)[-1])


@pytest.mark.asyncio
async def test_agent_multipart_create_put_complete_publishes_exact_artifact(
    tmp_path: Path,
) -> None:
    payload = b"trace bytes"
    service = _service(tmp_path)

    slot = await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=len(payload),
        sha256_b64=_checksum(payload),
    )
    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        part_number=1,
    )
    grant = urlsplit(part.url).path.rsplit("/", 1)[-1]
    etag = await service.put_part(grant, (payload,))
    completed = await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        parts=(MultipartPart(part_number=1, etag=etag),),
    )

    assert completed.state == "finalized"
    assert _completed_path(tmp_path, completed.artifact_id).read_bytes() == payload
    assert await service.put_part(grant, (payload,)) == etag


@pytest.mark.asyncio
async def test_wrong_lease_is_rejected_before_upload_filesystem_write(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(AgentUploadStaleLease, match="lease version is stale"):
        await service.create_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=2,
            artifact_kind="startup_trace",
            mime="application/x-perfetto-trace",
            size=1,
            sha256_b64=_checksum(b"x"),
        )

    assert not any((tmp_path / "teams").iterdir())


@pytest.mark.asyncio
async def test_memory_evidence_is_rejected_before_analysis_directory_write(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    with pytest.raises(AgentUploadInvalidRequest, match="upload request is invalid"):
        await service.create_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            artifact_kind="memory_evidence",
            mime="application/json",
            size=1,
            sha256_b64=_checksum(b"x"),
        )

    assert not any((tmp_path / "teams").iterdir())


def test_input_symlink_and_fifo_are_rejected_without_opening_target(tmp_path: Path) -> None:
    payload = b"private apk bytes"
    source = (
        tmp_path
        / "teams"
        / str(TEAM_ID)
        / "analyses"
        / str(ANALYSIS_ID)
        / "uploads"
        / f"{INPUT_UPLOAD_ID}.bin"
    )
    source.parent.mkdir(mode=0o700, parents=True)
    target = tmp_path / "outside.apk"
    target.write_bytes(payload)
    source.symlink_to(target)
    service = _service(tmp_path)
    kwargs = dict(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=INPUT_ID,
        upload_id=INPUT_UPLOAD_ID,
        mime="application/vnd.android.package-archive",
        size=len(payload),
        sha256_b64=_checksum(payload),
    )

    with pytest.raises(AgentUploadNotFound, match="input artifact was not found"):
        service.register_input(**kwargs)
    source.unlink()
    os.mkfifo(source, 0o600)
    with pytest.raises(AgentUploadNotFound, match="input artifact was not found"):
        service.register_input(**kwargs)


@pytest.mark.asyncio
async def test_root_substitution_is_rejected_before_part_write(tmp_path: Path) -> None:
    service = _service(tmp_path)
    slot = await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        part_number=1,
    )
    token = urlsplit(part.url).path.rsplit("/", 1)[-1]
    original = tmp_path / "teams"
    moved = tmp_path / "teams-original"
    original.rename(moved)
    original.mkdir(mode=0o700)

    with pytest.raises(AgentUploadUnavailable, match="local artifact storage is unavailable"):
        await service.put_part(token, (b"x",))


@pytest.mark.asyncio
async def test_duplicate_part_conflicts_on_changed_bytes_and_completion_is_immutable(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    slot = await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        part_number=1,
    )
    token = urlsplit(part.url).path.rsplit("/", 1)[-1]
    etag = await service.put_part(token, (b"x",))
    with pytest.raises(AgentUploadMismatch, match="upload part does not match"):
        await service.put_part(token, (b"y",))
    receipt = (MultipartPart(part_number=1, etag=etag),)
    completed = await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        parts=receipt,
    )
    repeated = await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        parts=receipt,
    )

    assert repeated == completed
    assert _completed_path(tmp_path, slot.artifact_id).read_bytes() == b"x"


@pytest.mark.asyncio
async def test_repeated_complete_rejects_tampered_completed_artifact(tmp_path: Path) -> None:
    service = _service(tmp_path)
    slot = await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        part_number=1,
    )
    etag = await service.put_part(
        urlsplit(part.url).path.rsplit("/", 1)[-1], (b"x",)
    )
    receipt = (MultipartPart(part_number=1, etag=etag),)
    await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=slot.upload_id,
        parts=receipt,
    )
    completed_path = _completed_path(tmp_path, slot.artifact_id)
    completed_path.write_bytes(b"y")

    with pytest.raises(AgentUploadMismatch, match="completed artifact does not match"):
        await service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            upload_id=slot.upload_id,
            parts=receipt,
        )


@pytest.mark.asyncio
async def test_restart_resumes_parts_and_abort_only_removes_incomplete(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=first.upload_id,
        part_number=1,
    )
    etag = await service.put_part(
        urlsplit(part.url).path.rsplit("/", 1)[-1], (b"x",)
    )
    service.close()
    reopened = LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=FixedExecutionAuthorizer(),
        clock=lambda: NOW,
        uuid_source=lambda: UUID("78000000-0000-4000-8000-000000000001"),
    )
    resumed = await reopened.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    completed = await reopened.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=resumed.upload_id,
        parts=(MultipartPart(part_number=1, etag=etag),),
    )
    await reopened.abort_execution(
        access=await FixedExecutionAuthorizer().authorize_execution(lease_version=3),
        now=NOW,
    )

    assert resumed.upload_id == first.upload_id
    assert _completed_path(tmp_path, completed.artifact_id).read_bytes() == b"x"
