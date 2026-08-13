from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
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
    def __init__(
        self, *, lease_expires_at: datetime = NOW + timedelta(minutes=5)
    ) -> None:
        self.active = True
        self.lease_expires_at = lease_expires_at

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
            lease_expires_at=self.lease_expires_at,
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


def _state_path(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "teams"
        / str(TEAM_ID)
        / "analyses"
        / str(ANALYSIS_ID)
        / "agent-artifacts"
        / "state.json"
    )


async def _pending_upload(tmp_path: Path) -> tuple[LocalAgentArtifactService, str]:
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
    return service, urlsplit(part.url).path.rsplit("/", 1)[-1]


async def _persisted_part(tmp_path: Path) -> dict[str, object]:
    service, token = await _pending_upload(tmp_path)
    await service.put_part(token, (b"x",))
    service.close()
    return json.loads(_state_path(tmp_path).read_text())


def _write_state(tmp_path: Path, document: dict[str, object]) -> None:
    _state_path(tmp_path).write_text(
        json.dumps(document, separators=(",", ":"), sort_keys=True)
    )


def _reopen(
    tmp_path: Path,
    *,
    now: datetime = NOW,
    authorizer: FixedExecutionAuthorizer | None = None,
) -> LocalAgentArtifactService:
    return LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=authorizer or FixedExecutionAuthorizer(),
        clock=lambda: now,
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
async def test_input_validation_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    original_read = os.read

    def slow_read(descriptor: int, size: int) -> bytes:
        time.sleep(0.2)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", slow_read)
    started = asyncio.get_running_loop().time()
    opened_task = asyncio.create_task(
        service.open_input(urlsplit(slot.url).path.rsplit("/", 1)[-1])
    )
    await asyncio.sleep(0.02)
    elapsed = asyncio.get_running_loop().time() - started
    opened = await opened_task
    assert elapsed < 0.1
    assert b"".join(opened.body) == payload


@pytest.mark.asyncio
async def test_cancelled_input_validation_closes_worker_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    original_open = service._open_verified_input
    opened = threading.Event()
    release = threading.Event()
    descriptors: list[int] = []

    def gated_open(*args: object):
        result = original_open(*args)  # type: ignore[arg-type]
        descriptors.extend((result.descriptor, result.directory))
        opened.set()
        assert release.wait(timeout=2)
        return result

    monkeypatch.setattr(service, "_open_verified_input", gated_open)
    task = asyncio.create_task(
        service.open_input(urlsplit(slot.url).path.rsplit("/", 1)[-1])
    )
    assert await asyncio.to_thread(opened.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)

    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError) as raised:
            os.fstat(descriptor)
        assert raised.value.errno == 9


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
async def test_part_put_revalidates_lease_before_creating_bytes(tmp_path: Path) -> None:
    authorizer = FixedExecutionAuthorizer()
    service = LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=authorizer,
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
        token_source=lambda: "revocable-part-grant",
    )
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
    authorizer.active = False

    with pytest.raises(AgentUploadNotFound, match="execution was not found"):
        await service.put_part(urlsplit(part.url).path.rsplit("/", 1)[-1], (b"x",))

    part_dir = _state_path(tmp_path).parent / "parts" / str(slot.upload_id)
    assert list(part_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_part_put_revalidates_lease_before_atomic_publish(tmp_path: Path) -> None:
    authorizer = FixedExecutionAuthorizer()
    service = LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=authorizer,
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
        token_source=lambda: "streaming-part-grant",
    )
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

    async def revoke_during_stream():
        yield b"x"
        authorizer.active = False

    with pytest.raises(AgentUploadNotFound, match="execution was not found"):
        await service.put_part(
            urlsplit(part.url).path.rsplit("/", 1)[-1], revoke_during_stream()
        )

    part_dir = _state_path(tmp_path).parent / "parts" / str(slot.upload_id)
    assert list(part_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_part_put_cleans_temp_when_async_body_raises(tmp_path: Path) -> None:
    service, token = await _pending_upload(tmp_path)

    async def broken_body():
        yield b"x"
        raise RuntimeError("private transport detail")

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ) as raised:
        await service.put_part(token, broken_body())

    assert "private transport detail" not in str(raised.value)
    part_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    assert list(part_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_part_put_cleans_temp_and_reraises_cancellation(tmp_path: Path) -> None:
    service, token = await _pending_upload(tmp_path)

    async def canceled_body():
        yield b"x"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.put_part(token, canceled_body())

    part_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    assert list(part_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_part_publish_rolls_back_when_state_save_fails_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, token = await _pending_upload(tmp_path)
    original_save = service._save_state
    failed = False

    def fail_once(team_id: UUID, analysis_id: UUID) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise AgentUploadUnavailable("local artifact storage is unavailable")
        original_save(team_id, analysis_id)

    monkeypatch.setattr(service, "_save_state", fail_once)
    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        await service.put_part(token, (b"x",))

    part_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    assert list(part_dir.iterdir()) == []
    etag = await service.put_part(token, (b"x",))
    service.close()
    reopened = _reopen(tmp_path)
    resumed = await reopened.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    assert resumed.upload_id == UPLOAD_ID
    assert resumed.state == "pending"
    assert etag.startswith('"')


@pytest.mark.asyncio
async def test_final_publish_rolls_back_when_state_save_fails_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, token = await _pending_upload(tmp_path)
    etag = await service.put_part(token, (b"x",))
    receipt = (MultipartPart(part_number=1, etag=etag),)
    original_save = service._save_state
    failed = False

    def fail_once(team_id: UUID, analysis_id: UUID) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise AgentUploadUnavailable("local artifact storage is unavailable")
        original_save(team_id, analysis_id)

    monkeypatch.setattr(service, "_save_state", fail_once)
    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        await service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            upload_id=UPLOAD_ID,
            parts=receipt,
        )

    assert not _completed_path(tmp_path, ARTIFACT_ID).exists()
    completed = await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )
    service.close()
    reopened = _reopen(tmp_path)
    repeated = await reopened.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )
    assert completed.state == repeated.state == "finalized"


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ("part", "final"))
async def test_post_replace_state_failure_keeps_publication_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication: str,
) -> None:
    service, token = await _pending_upload(tmp_path)
    etag: str | None = None
    if publication == "final":
        etag = await service.put_part(token, (b"x",))
    original_replace = os.replace
    original_fsync = os.fsync
    state_replaced = False

    def observe_replace(*args: object, **kwargs: object) -> None:
        nonlocal state_replaced
        original_replace(*args, **kwargs)  # type: ignore[arg-type]
        state_replaced = True

    def fail_state_directory_fsync(descriptor: int) -> None:
        nonlocal state_replaced
        if state_replaced and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            state_replaced = False
            raise OSError("private durability detail")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "replace", observe_replace)
    monkeypatch.setattr(os, "fsync", fail_state_directory_fsync)
    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ) as raised:
        if publication == "part":
            await service.put_part(token, (b"x",))
        else:
            await service.complete_upload(
                agent_id=AGENT_ID,
                execution_id=EXECUTION_ID,
                lease_version=3,
                upload_id=UPLOAD_ID,
                parts=(MultipartPart(part_number=1, etag=etag),),  # type: ignore[arg-type]
            )
    assert "private durability detail" not in str(raised.value)
    assert getattr(raised.value, "committed", False) is True
    monkeypatch.setattr(os, "replace", original_replace)
    monkeypatch.setattr(os, "fsync", original_fsync)

    if publication == "part":
        retried = await service.put_part(token, (b"x",))
        assert retried.startswith('"')
    else:
        retried_slot = await service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            upload_id=UPLOAD_ID,
            parts=(MultipartPart(part_number=1, etag=etag),),  # type: ignore[arg-type]
        )
        assert retried_slot.state == "finalized"
    service.close()
    reopened = _reopen(tmp_path)
    resumed = await reopened.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    assert resumed.upload_id == UPLOAD_ID
    assert resumed.state == ("pending" if publication == "part" else "finalized")


@pytest.mark.asyncio
async def test_slow_part_stream_does_not_block_independent_upload(
    tmp_path: Path,
) -> None:
    service = LocalAgentArtifactService(
        root=tmp_path,
        public_origin="http://testserver",
        execution_authorizer=FixedExecutionAuthorizer(),
        clock=lambda: NOW,
        uuid_source=iter(
            (
                ARTIFACT_ID,
                UPLOAD_ID,
                UUID("76000000-0000-4000-8000-000000000002"),
                UUID("77000000-0000-4000-8000-000000000002"),
            )
        ).__next__,
        token_source=lambda: "slow-part-grant",
    )
    first = await service.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=2,
        sha256_b64=_checksum(b"xy"),
    )
    part = await service.authorize_part(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=first.upload_id,
        part_number=1,
    )
    paused = asyncio.Event()
    resume = asyncio.Event()

    async def slow_body():
        yield b"x"
        paused.set()
        await resume.wait()
        yield b"y"

    put = asyncio.create_task(
        service.put_part(urlsplit(part.url).path.rsplit("/", 1)[-1], slow_body())
    )
    await paused.wait()
    second = await asyncio.wait_for(
        service.create_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            artifact_kind="scroll_trace",
            mime="application/x-perfetto-trace",
            size=1,
            sha256_b64=_checksum(b"z"),
        ),
        timeout=0.2,
    )
    resume.set()
    await put
    assert second.artifact_kind == "scroll_trace"


@pytest.mark.asyncio
async def test_part_disk_work_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, token = await _pending_upload(tmp_path)
    original_write = service._write_chunk

    def slow_write(*args: object) -> int:
        time.sleep(0.2)
        return original_write(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_write_chunk", slow_write)
    started = asyncio.get_running_loop().time()
    put = asyncio.create_task(service.put_part(token, (b"x",)))
    await asyncio.sleep(0.02)
    elapsed = asyncio.get_running_loop().time() - started
    await put
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_concurrent_exact_completion_is_idempotent(tmp_path: Path) -> None:
    service, token = await _pending_upload(tmp_path)
    etag = await service.put_part(token, (b"x",))
    receipt = (MultipartPart(part_number=1, etag=etag),)

    first, second = await asyncio.gather(
        *(
            service.complete_upload(
                agent_id=AGENT_ID,
                execution_id=EXECUTION_ID,
                lease_version=3,
                upload_id=UPLOAD_ID,
                parts=receipt,
            )
            for _ in range(2)
        )
    )

    assert first == second


@pytest.mark.asyncio
async def test_finalized_exact_completion_verify_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, token = await _pending_upload(tmp_path)
    etag = await service.put_part(token, (b"x",))
    receipt = (MultipartPart(part_number=1, etag=etag),)
    await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )
    original_verify = service._verify_completed

    def slow_verify(*args: object) -> None:
        time.sleep(0.2)
        original_verify(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_verify_completed", slow_verify)
    started = asyncio.get_running_loop().time()
    completed_task = asyncio.create_task(
        service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            upload_id=UPLOAD_ID,
            parts=receipt,
        )
    )
    await asyncio.sleep(0.02)
    elapsed = asyncio.get_running_loop().time() - started
    completed = await completed_task
    assert elapsed < 0.1
    assert completed.state == "finalized"


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


@pytest.mark.asyncio
async def test_recovery_rejects_oversized_state_without_unbounded_read(tmp_path: Path) -> None:
    await _persisted_part(tmp_path)
    _state_path(tmp_path).write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_target", ("state", "part", "finalized", "input", "stale_temp")
)
async def test_recovery_rejects_world_readable_private_files(
    tmp_path: Path, unsafe_target: str
) -> None:
    if unsafe_target in {"state", "part", "stale_temp"}:
        await _persisted_part(tmp_path)
        if unsafe_target == "state":
            target = _state_path(tmp_path)
        elif unsafe_target == "part":
            target = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID) / "1.part"
        else:
            target = (
                _state_path(tmp_path).parent
                / "parts"
                / str(UPLOAD_ID)
                / ".1.12345678123442348123456789abcdef.tmp"
            )
            target.write_bytes(b"interrupted")
    elif unsafe_target == "finalized":
        service, token = await _pending_upload(tmp_path)
        etag = await service.put_part(token, (b"x",))
        await service.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            upload_id=UPLOAD_ID,
            parts=(MultipartPart(part_number=1, etag=etag),),
        )
        service.close()
        target = _completed_path(tmp_path, ARTIFACT_ID)
    else:
        payload = b"private apk bytes"
        target = _input_path(tmp_path)
        target.parent.mkdir(mode=0o700, parents=True)
        target.write_bytes(payload)
        target.chmod(0o600)
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
        service.close()
    target.chmod(0o644)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
async def test_recovery_rejects_uuid_named_directory_symlink_without_reading_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "teams"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside" / "team"
    state = outside / "analyses" / str(ANALYSIS_ID) / "agent-artifacts" / "state.json"
    state.parent.mkdir(mode=0o700, parents=True)
    state.write_text('{"schema_version":"1.0","inputs":[],"uploads":[]}')
    (root / str(TEAM_ID)).symlink_to(outside, target_is_directory=True)
    outside_read = False
    original = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        nonlocal outside_read
        if path == state:
            outside_read = True
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)
    assert outside_read is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry_level", "entry_kind"), (("analysis", "symlink"), ("analysis", "fifo"), ("upload", "fifo")))
async def test_recovery_rejects_uuid_named_non_directory_entries(
    tmp_path: Path, entry_level: str, entry_kind: str
) -> None:
    if entry_level == "upload":
        await _persisted_part(tmp_path)
        target = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
        (target / "1.part").unlink()
        target.rmdir()
    else:
        analyses = tmp_path / "teams" / str(TEAM_ID) / "analyses"
        analyses.mkdir(mode=0o700, parents=True)
        target = analyses / str(ANALYSIS_ID)
    if entry_kind == "symlink":
        outside = tmp_path / "outside-analysis"
        outside.mkdir(mode=0o700)
        target.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(target, 0o600)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
async def test_recovery_rejects_upload_symlink_and_missing_part(tmp_path: Path) -> None:
    document = await _persisted_part(tmp_path)
    upload_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    outside = tmp_path / "outside-parts"
    outside.mkdir(mode=0o700)
    (outside / "1.part").write_bytes(b"x")
    (upload_dir / "1.part").unlink()
    upload_dir.rmdir()
    upload_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)

    upload_dir.unlink()
    upload_dir.mkdir(mode=0o700)
    _write_state(tmp_path, document)
    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("fifo_target", ("state", "part"))
async def test_recovery_rejects_fifo_files_promptly(
    tmp_path: Path, fifo_target: str
) -> None:
    await _persisted_part(tmp_path)
    target = (
        _state_path(tmp_path)
        if fifo_target == "state"
        else _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID) / "1.part"
    )
    target.unlink()
    os.mkfifo(target, 0o600)
    script = """
from pathlib import Path
import sys
from perfpilot_api.local_agent_artifacts import LocalAgentArtifactService
from perfpilot_api.services.agent_uploads import AgentUploadUnavailable

try:
    LocalAgentArtifactService(
        root=Path(sys.argv[1]),
        public_origin="http://testserver",
        execution_authorizer=object(),
    )
except AgentUploadUnavailable as error:
    assert str(error) == "local artifact storage is unavailable"
    raise SystemExit(0)
raise SystemExit(2)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("recovery blocked while opening a FIFO")

    assert result.returncode == 0, result.stderr
    assert str(tmp_path) not in result.stdout + result.stderr


@pytest.mark.asyncio
async def test_recovery_removes_only_owned_stale_part_temp(tmp_path: Path) -> None:
    document = await _persisted_part(tmp_path)
    upload_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    owned_temp = upload_dir / ".1.12345678123442348123456789abcdef.tmp"
    owned_temp.write_bytes(b"interrupted")
    owned_temp.chmod(0o600)

    reopened = _reopen(tmp_path)

    assert not owned_temp.exists()
    resumed = await reopened.create_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        artifact_kind="startup_trace",
        mime="application/x-perfetto-trace",
        size=1,
        sha256_b64=_checksum(b"x"),
    )
    assert resumed.upload_id == UUID(document["uploads"][0]["upload_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_name", (".1.bad.tmp", "secret.txt"))
async def test_recovery_rejects_unknown_part_entries(
    tmp_path: Path, unknown_name: str
) -> None:
    await _persisted_part(tmp_path)
    upload_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    (upload_dir / unknown_name).write_bytes(b"unexpected")

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
async def test_recovery_rejects_symlink_with_owned_temp_name(tmp_path: Path) -> None:
    await _persisted_part(tmp_path)
    upload_dir = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID)
    marker = tmp_path / "outside-marker"
    marker.write_bytes(b"outside")
    owned_name = ".1.12345678123442348123456789abcdef.tmp"
    (upload_dir / owned_name).symlink_to(marker)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)
    assert marker.read_bytes() == b"outside"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("size", 512 * 1024 * 1024 + 1),
        ("part_count", 33),
        ("agent_id", "71000000-0000-4000-8000-00000000000A"),
        ("mime", "Invalid Mime"),
        ("lease_version", 0),
        ("expires_at", "2026-08-13T09:05:00"),
        ("parts", [{"part_number": 1, "etag": "bad\nvalue"}]),
    ),
)
async def test_recovery_rejects_invalid_persisted_upload_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    document = await _persisted_part(tmp_path)
    upload = document["uploads"][0]
    upload[field] = value
    if field == "size":
        upload["part_count"] = 9
    _write_state(tmp_path, document)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", (b"y", b"xx"))
async def test_recovery_rejects_part_digest_or_size_mismatch(
    tmp_path: Path, replacement: bytes
) -> None:
    await _persisted_part(tmp_path)
    part = _state_path(tmp_path).parent / "parts" / str(UPLOAD_ID) / "1.part"
    part.write_bytes(replacement)

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("missing", "tampered"))
async def test_recovery_rejects_missing_or_tampered_finalized_file(
    tmp_path: Path, mode: str
) -> None:
    service, token = await _pending_upload(tmp_path)
    etag = await service.put_part(token, (b"x",))
    await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=(MultipartPart(part_number=1, etag=etag),),
    )
    service.close()
    completed = _completed_path(tmp_path, ARTIFACT_ID)
    if mode == "missing":
        completed.unlink()
    else:
        completed.write_bytes(b"y")

    with pytest.raises(
        AgentUploadUnavailable, match="local artifact storage is unavailable"
    ):
        _reopen(tmp_path)


@pytest.mark.asyncio
async def test_valid_finalized_artifact_reopens_idempotently(tmp_path: Path) -> None:
    service, token = await _pending_upload(tmp_path)
    etag = await service.put_part(token, (b"x",))
    receipt = (MultipartPart(part_number=1, etag=etag),)
    await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )
    service.close()

    reopened = _reopen(tmp_path)
    completed = await reopened.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )

    assert completed.state == "finalized"


@pytest.mark.asyncio
async def test_expired_finalized_artifact_reopens_for_completion_projection(
    tmp_path: Path,
) -> None:
    service, token = await _pending_upload(tmp_path)
    etag = await service.put_part(token, (b"x",))
    receipt = (MultipartPart(part_number=1, etag=etag),)
    await service.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )
    service.close()
    later = NOW + timedelta(hours=1)

    reopened = _reopen(
        tmp_path,
        now=later,
        authorizer=FixedExecutionAuthorizer(
            lease_expires_at=later + timedelta(minutes=5)
        ),
    )
    completed = await reopened.complete_upload(
        agent_id=AGENT_ID,
        execution_id=EXECUTION_ID,
        lease_version=3,
        upload_id=UPLOAD_ID,
        parts=receipt,
    )

    assert completed.state == "finalized"


@pytest.mark.asyncio
async def test_expired_pending_upload_is_cleaned_during_recovery(tmp_path: Path) -> None:
    await _persisted_part(tmp_path)
    later = NOW + timedelta(hours=1)

    reopened = _reopen(
        tmp_path,
        now=later,
        authorizer=FixedExecutionAuthorizer(
            lease_expires_at=later + timedelta(minutes=5)
        ),
    )
    part_root = _state_path(tmp_path).parent / "parts"

    assert not (part_root / str(UPLOAD_ID)).exists()
    reopened.close()
    reopened = _reopen(
        tmp_path,
        now=later,
        authorizer=FixedExecutionAuthorizer(
            lease_expires_at=later + timedelta(minutes=5)
        ),
    )
    with pytest.raises(AgentUploadNotFound, match="upload was not found"):
        await reopened.complete_upload(
            agent_id=AGENT_ID,
            execution_id=EXECUTION_ID,
            lease_version=3,
            upload_id=UPLOAD_ID,
            parts=(MultipartPart(part_number=1, etag='"' + "0" * 64 + '"'),),
        )
