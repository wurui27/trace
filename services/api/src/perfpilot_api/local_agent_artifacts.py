"""Private local storage for Agent APK inputs and multipart capture artifacts."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
from collections.abc import AsyncIterable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.services.agent_tasks import (
    AgentExecutionAccess,
    AgentTaskNotFound,
    StaleLeaseVersion,
    ValidatedAgentExecutionManifest,
)
from perfpilot_api.services.agent_uploads import (
    AgentInputSlot,
    AgentUploadExpired,
    AgentUploadInvalidRequest,
    AgentUploadMismatch,
    AgentUploadNotFound,
    AgentUploadPartSlot,
    AgentUploadSlot,
    AgentUploadStaleLease,
    AgentUploadUnavailable,
)
from perfpilot_api.storage.base import MultipartPart


_MAX_BYTES = 512 * 1024 * 1024
_PART_BYTES = 64 * 1024 * 1024
_MAX_PARTS = 32
_MIME = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_ALLOWED_KINDS = frozenset({"startup_trace", "scroll_trace", "agent_log"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True, slots=True)
class LocalArtifactBody:
    body: Iterable[bytes] = field(repr=False)
    mime: str
    size: int
    etag: str = field(repr=False)


@dataclass(slots=True)
class _Input:
    team_id: UUID
    analysis_id: UUID
    artifact_id: UUID
    upload_id: UUID
    mime: str
    size: int
    sha256_b64: str = field(repr=False)


@dataclass(slots=True)
class _Upload:
    team_id: UUID
    analysis_id: UUID
    agent_id: UUID
    execution_id: UUID
    lease_version: int
    artifact_id: UUID
    upload_id: UUID
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str = field(repr=False)
    part_count: int
    expires_at: datetime
    parts: dict[int, str] = field(default_factory=dict, repr=False)
    finalized_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Grant:
    purpose: Literal["input", "part"]
    team_id: UUID
    analysis_id: UUID
    agent_id: UUID
    execution_id: UUID
    lease_version: int
    expires_at: datetime
    artifact_id: UUID | None = None
    upload_id: UUID | None = None
    part_number: int | None = None


class LocalAgentArtifactService:
    """Implements the Agent upload coordinator contract using private local files."""

    def __init__(
        self,
        *,
        root: Path,
        public_origin: str,
        execution_authorizer: object,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_source: Callable[[], UUID] = uuid4,
        token_source: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        self._anchor = root.resolve()
        self._root = self._anchor / "teams"
        self._public_origin = public_origin.rstrip("/")
        self._authorizer = execution_authorizer
        self._clock = clock
        self._uuid_source = uuid_source
        self._token_source = token_source
        self._inputs: dict[UUID, _Input] = {}
        self._uploads: dict[UUID, _Upload] = {}
        self._artifacts: dict[UUID, _Upload] = {}
        self._grants: dict[str, _Grant] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_status = os.lstat(self._root)
            if not stat.S_ISDIR(root_status.st_mode):
                raise OSError
            self._root_fd = os.open(
                self._root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC
            )
            os.fchmod(self._root_fd, 0o700)
            held = os.fstat(self._root_fd)
            self._root_identity = (held.st_dev, held.st_ino)
            self._verify_root()
            self._recover()
        except Exception:
            self.close()
            raise AgentUploadUnavailable("local artifact storage is unavailable") from None

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            self._root_fd = -1
            os.close(descriptor)
        self._closed = True

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise AgentUploadUnavailable("local artifact storage is unavailable")
        return value.astimezone(UTC)

    def _verify_root(self) -> None:
        try:
            current = os.lstat(self._root)
            held = os.fstat(self._root_fd)
            if (
                self._closed
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self._root_identity
                or (held.st_dev, held.st_ino) != self._root_identity
            ):
                raise OSError
        except OSError:
            raise AgentUploadUnavailable("local artifact storage is unavailable") from None

    @staticmethod
    def _uuid(value: UUID, _name: str) -> UUID:
        if not isinstance(value, UUID) or value.version not in range(1, 6):
            raise AgentUploadInvalidRequest("artifact request is invalid")
        return value

    @staticmethod
    def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
            dir_fd=parent_fd,
        )
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISDIR(mode):
            os.close(descriptor)
            raise OSError
        os.fchmod(descriptor, 0o700)
        return descriptor

    def _analysis_fd(self, team_id: UUID, analysis_id: UUID, *, create: bool) -> int:
        self._verify_root()
        self._uuid(team_id, "team_id")
        self._uuid(analysis_id, "analysis_id")
        opened: list[int] = []
        try:
            team = self._open_directory(self._root_fd, str(team_id), create=create)
            opened.append(team)
            analyses = self._open_directory(team, "analyses", create=create)
            opened.append(analyses)
            analysis = self._open_directory(analyses, str(analysis_id), create=create)
            return analysis
        finally:
            for descriptor in opened:
                os.close(descriptor)

    def _subdir_fd(
        self, team_id: UUID, analysis_id: UUID, name: str, *, create: bool
    ) -> int:
        if name not in {"uploads", "agent-artifacts", "parts", "completed"}:
            raise AgentUploadUnavailable("local artifact storage is unavailable")
        analysis = self._analysis_fd(team_id, analysis_id, create=create)
        try:
            return self._open_directory(analysis, name, create=create)
        finally:
            os.close(analysis)

    @staticmethod
    def _canonical_checksum(value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise AgentUploadInvalidRequest("artifact request is invalid") from None
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
            raise AgentUploadInvalidRequest("artifact request is invalid")
        return value

    @staticmethod
    def _file_metadata(directory_fd: int, name: str) -> os.stat_result:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError
        return metadata

    @staticmethod
    def _stream_descriptor(descriptor: int, directory_fd: int) -> Iterable[bytes]:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = -1
                while chunk := source.read(1024 * 1024):
                    yield chunk
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    def register_input(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        upload_id: UUID,
        mime: str,
        size: int,
        sha256_b64: str,
    ) -> None:
        for value, name in (
            (team_id, "team_id"),
            (analysis_id, "analysis_id"),
            (artifact_id, "artifact_id"),
            (upload_id, "upload_id"),
        ):
            self._uuid(value, name)
        if (
            _MIME.fullmatch(mime) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= _MAX_BYTES
        ):
            raise AgentUploadInvalidRequest("artifact request is invalid")
        checksum = self._canonical_checksum(sha256_b64)
        directory = self._subdir_fd(team_id, analysis_id, "uploads", create=False)
        try:
            metadata = self._file_metadata(directory, f"{upload_id}.bin")
            if metadata.st_size != size:
                raise AgentUploadMismatch("input artifact metadata does not match")
            observed = hashlib.sha256()
            descriptor = os.open(
                f"{upload_id}.bin", os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=directory
            )
            try:
                with os.fdopen(descriptor, "rb", closefd=True) as source:
                    descriptor = -1
                    while chunk := source.read(1024 * 1024):
                        observed.update(chunk)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if not hmac.compare_digest(
                base64.b64encode(observed.digest()).decode("ascii"), checksum
            ):
                raise AgentUploadMismatch("input artifact metadata does not match")
        except AgentUploadMismatch:
            raise
        except OSError:
            raise AgentUploadNotFound("input artifact was not found") from None
        finally:
            os.close(directory)
        existing = self._inputs.get(artifact_id)
        item = _Input(team_id, analysis_id, artifact_id, upload_id, mime, size, checksum)
        if existing is not None and existing != item:
            raise AgentUploadMismatch("input artifact metadata does not match")
        self._inputs[artifact_id] = item
        self._save_state(team_id, analysis_id)

    async def _authorize(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess:
        try:
            access = await self._authorizer.authorize_execution(
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=lease_version,
                now=now,
            )
        except StaleLeaseVersion:
            raise AgentUploadStaleLease("lease version is stale") from None
        except AgentTaskNotFound:
            raise AgentUploadNotFound("execution was not found") from None
        except (AgentUploadStaleLease, AgentUploadNotFound):
            raise
        except Exception:
            raise AgentUploadUnavailable("upload service is unavailable") from None
        if (
            access.agent_id != agent_id
            or access.execution_id != execution_id
            or access.lease_version != lease_version
            or access.lease_expires_at <= now
        ):
            raise AgentUploadNotFound("execution was not found")
        return access

    def _grant(self, grant: _Grant) -> str:
        token = self._token_source()
        if not isinstance(token, str) or not token or token in self._grants:
            raise AgentUploadUnavailable("upload service is unavailable")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if digest in self._grants:
            raise AgentUploadUnavailable("upload service is unavailable")
        self._grants[digest] = grant
        return token

    def _load_grant(self, token: str, purpose: Literal["input", "part"]) -> _Grant:
        if not isinstance(token, str):
            raise AgentUploadNotFound("artifact grant was not found")
        grant = self._grants.get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if grant is None or grant.purpose != purpose or grant.expires_at <= self._now():
            raise AgentUploadNotFound("artifact grant was not found")
        return grant

    async def authorize_input(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        artifact_id: UUID,
    ) -> AgentInputSlot:
        now = self._now()
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        item = self._inputs.get(artifact_id)
        if (
            item is None
            or artifact_id not in access.input_artifact_ids
            or item.team_id != access.team_id
            or item.analysis_id != access.analysis_id
        ):
            raise AgentUploadNotFound("input artifact was not found")
        expires_at = min(now + timedelta(minutes=5), access.lease_expires_at)
        token = self._grant(
            _Grant(
                "input",
                access.team_id,
                access.analysis_id,
                agent_id,
                execution_id,
                lease_version,
                expires_at,
                artifact_id=artifact_id,
            )
        )
        return AgentInputSlot(
            artifact_id=artifact_id,
            mime=item.mime,
            size=item.size,
            sha256_b64=item.sha256_b64,
            url=f"{self._public_origin}/local/v1/agent-inputs/{token}",
            expires_at=expires_at,
        )

    async def open_input(self, token: str) -> LocalArtifactBody:
        grant = self._load_grant(token, "input")
        access = await self._authorize(
            agent_id=grant.agent_id,
            execution_id=grant.execution_id,
            lease_version=grant.lease_version,
            now=self._now(),
        )
        item = self._inputs.get(grant.artifact_id)
        if (
            item is None
            or item.team_id != grant.team_id
            or item.analysis_id != grant.analysis_id
            or access.team_id != grant.team_id
            or access.analysis_id != grant.analysis_id
            or grant.artifact_id not in access.input_artifact_ids
        ):
            raise AgentUploadNotFound("input artifact was not found")
        directory = self._subdir_fd(item.team_id, item.analysis_id, "uploads", create=False)
        descriptor = -1
        try:
            name = f"{item.upload_id}.bin"
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=directory)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            if metadata.st_size != item.size:
                raise OSError
            # Hash before yielding so changed content is never served.
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            if not hmac.compare_digest(
                base64.b64encode(digest.digest()).decode("ascii"), item.sha256_b64
            ):
                raise OSError
            os.lseek(descriptor, 0, os.SEEK_SET)
            return LocalArtifactBody(
                body=self._stream_descriptor(descriptor, directory),
                mime=item.mime,
                size=item.size,
                etag=f'"{base64.b16encode(digest.digest()).decode("ascii").lower()}"',
            )
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory)
            raise AgentUploadNotFound("input artifact was not found") from None

    async def create_upload(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        artifact_kind: str,
        mime: str,
        size: int,
        sha256_b64: str,
    ) -> AgentUploadSlot:
        if (
            artifact_kind not in _ALLOWED_KINDS
            or _MIME.fullmatch(mime) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= _MAX_BYTES
        ):
            raise AgentUploadInvalidRequest("upload request is invalid")
        checksum = self._canonical_checksum(sha256_b64)
        now = self._now()
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        if artifact_kind not in access.allowed_uploads:
            raise AgentUploadInvalidRequest("artifact kind is not allowed by this task")
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self._uploads.values()
                    if item.execution_id == execution_id
                    and item.artifact_kind == artifact_kind
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.team_id != access.team_id
                    or existing.analysis_id != access.analysis_id
                    or existing.agent_id != agent_id
                    or existing.lease_version != lease_version
                    or existing.mime != mime
                    or existing.size != size
                    or not hmac.compare_digest(existing.sha256_b64, checksum)
                ):
                    raise AgentUploadInvalidRequest(
                        "upload kind was reused with different metadata"
                    )
                return self._slot(existing)
            artifact_id = self._uuid_source()
            upload_id = self._uuid_source()
            self._uuid(artifact_id, "artifact_id")
            self._uuid(upload_id, "upload_id")
            part_count = math.ceil(size / _PART_BYTES)
            if part_count > _MAX_PARTS:
                raise AgentUploadInvalidRequest("upload request is invalid")
            expires_at = min(now + timedelta(minutes=15), access.lease_expires_at)
            upload = _Upload(
                access.team_id,
                access.analysis_id,
                agent_id,
                execution_id,
                lease_version,
                artifact_id,
                upload_id,
                artifact_kind,
                mime,
                size,
                checksum,
                part_count,
                expires_at,
            )
            directory = self._subdir_fd(
                access.team_id, access.analysis_id, "agent-artifacts", create=True
            )
            os.close(directory)
            analysis = self._analysis_fd(access.team_id, access.analysis_id, create=False)
            try:
                artifacts = self._open_directory(analysis, "agent-artifacts", create=False)
                try:
                    parts = self._open_directory(artifacts, "parts", create=True)
                    try:
                        upload_dir = self._open_directory(parts, str(upload_id), create=True)
                        os.close(upload_dir)
                    finally:
                        os.close(parts)
                    completed = self._open_directory(artifacts, "completed", create=True)
                    os.close(completed)
                finally:
                    os.close(artifacts)
            finally:
                os.close(analysis)
            self._uploads[upload_id] = upload
            self._artifacts[artifact_id] = upload
            self._save_state(access.team_id, access.analysis_id)
            return self._slot(upload)

    async def authorize_part(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        upload_id: UUID,
        part_number: int,
    ) -> AgentUploadPartSlot:
        now = self._now()
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        upload = self._owned_upload(access, upload_id)
        if upload.finalized_at is not None or upload.expires_at <= now:
            raise AgentUploadExpired("upload has expired")
        if (
            isinstance(part_number, bool)
            or not isinstance(part_number, int)
            or not 1 <= part_number <= upload.part_count
        ):
            raise AgentUploadInvalidRequest("part number is invalid")
        expires_at = min(now + timedelta(minutes=15), access.lease_expires_at)
        token = self._grant(
            _Grant(
                "part",
                access.team_id,
                access.analysis_id,
                agent_id,
                execution_id,
                lease_version,
                expires_at,
                upload_id=upload_id,
                part_number=part_number,
            )
        )
        return AgentUploadPartSlot(
            upload_id=upload_id,
            part_number=part_number,
            url=f"{self._public_origin}/local/v1/agent-upload-parts/{token}",
            required_headers={},
            expires_at=expires_at,
        )

    async def put_part(self, token: str, chunks: AsyncIterable[bytes] | Iterable[bytes]) -> str:
        grant = self._load_grant(token, "part")
        upload = self._uploads.get(grant.upload_id)
        if (
            upload is None
            or upload.team_id != grant.team_id
            or upload.analysis_id != grant.analysis_id
            or upload.agent_id != grant.agent_id
            or upload.execution_id != grant.execution_id
            or upload.lease_version != grant.lease_version
            or grant.part_number is None
        ):
            raise AgentUploadNotFound("upload part was not found")
        expected = min(
            _PART_BYTES,
            upload.size - (grant.part_number - 1) * _PART_BYTES,
        )
        async with self._lock:
            directory = self._upload_parts_fd(upload, create=False)
            temporary = f".{grant.part_number}.{uuid4().hex}.tmp"
            size = 0
            digest = hashlib.sha256()
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        descriptor = -1
                        if hasattr(chunks, "__aiter__"):
                            async for chunk in chunks:  # type: ignore[union-attr]
                                size = self._write_chunk(output, digest, chunk, size, expected)
                        else:
                            for chunk in chunks:  # type: ignore[union-attr]
                                size = self._write_chunk(output, digest, chunk, size, expected)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                if size != expected:
                    raise AgentUploadMismatch("upload part size does not match")
                etag = f'"{digest.hexdigest()}"'
                name = f"{grant.part_number}.part"
                try:
                    existing = self._file_metadata(directory, name)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    previous = upload.parts.get(grant.part_number)
                    if existing.st_size != size or previous != etag:
                        raise AgentUploadMismatch("upload part does not match")
                    os.unlink(temporary, dir_fd=directory)
                    return etag
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
                upload.parts[grant.part_number] = etag
                self._save_state(upload.team_id, upload.analysis_id)
                return etag
            except (AgentUploadMismatch, AgentUploadNotFound):
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                raise
            except OSError:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
                raise AgentUploadUnavailable("local artifact storage is unavailable") from None
            finally:
                os.close(directory)

    @staticmethod
    def _write_chunk(
        output: object,
        digest: object,
        chunk: bytes,
        size: int,
        expected: int,
    ) -> int:
        if not isinstance(chunk, bytes):
            raise AgentUploadMismatch("upload part does not match")
        size += len(chunk)
        if size > expected:
            raise AgentUploadMismatch("upload part size does not match")
        output.write(chunk)  # type: ignore[attr-defined]
        digest.update(chunk)  # type: ignore[attr-defined]
        return size

    async def complete_upload(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        upload_id: UUID,
        parts: Sequence[MultipartPart],
    ) -> AgentUploadSlot:
        now = self._now()
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        upload = self._owned_upload(access, upload_id)
        canonical = tuple(parts)
        if len(canonical) != upload.part_count or any(
            not isinstance(part, MultipartPart)
            or part.part_number != number
            or upload.parts.get(number) != part.etag
            for number, part in enumerate(canonical, start=1)
        ):
            raise AgentUploadInvalidRequest("multipart completion is invalid")
        if upload.finalized_at is not None:
            self._verify_completed(upload)
            return self._slot(upload)
        if upload.expires_at <= now:
            raise AgentUploadExpired("upload has expired")
        async with self._lock:
            parts_fd = self._upload_parts_fd(upload, create=False)
            completed_fd = self._completed_fd(upload, create=False)
            temporary = f".{upload.artifact_id}.{uuid4().hex}.tmp"
            final_name = f"{upload.artifact_kind}-{upload.artifact_id}.bin"
            digest = hashlib.sha256()
            size = 0
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC,
                    0o600,
                    dir_fd=completed_fd,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    for number in range(1, upload.part_count + 1):
                        part_fd = os.open(
                            f"{number}.part",
                            os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                            dir_fd=parts_fd,
                        )
                        with os.fdopen(part_fd, "rb", closefd=True) as source:
                            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                                raise OSError
                            while chunk := source.read(1024 * 1024):
                                size += len(chunk)
                                if size > upload.size:
                                    raise AgentUploadMismatch("uploaded artifact does not match")
                                digest.update(chunk)
                                output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                checksum = base64.b64encode(digest.digest()).decode("ascii")
                if size != upload.size or not hmac.compare_digest(checksum, upload.sha256_b64):
                    raise AgentUploadMismatch("uploaded artifact does not match")
                try:
                    os.link(
                        temporary,
                        final_name,
                        src_dir_fd=completed_fd,
                        dst_dir_fd=completed_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise AgentUploadMismatch("completed artifact is immutable") from None
                os.unlink(temporary, dir_fd=completed_fd)
                os.fsync(completed_fd)
                upload.finalized_at = now
                self._save_state(upload.team_id, upload.analysis_id)
                return self._slot(upload)
            except (AgentUploadInvalidRequest, AgentUploadMismatch):
                try:
                    os.unlink(temporary, dir_fd=completed_fd)
                except FileNotFoundError:
                    pass
                raise
            except OSError:
                try:
                    os.unlink(temporary, dir_fd=completed_fd)
                except OSError:
                    pass
                raise AgentUploadUnavailable("local artifact storage is unavailable") from None
            finally:
                os.close(parts_fd)
                os.close(completed_fd)

    def _verify_completed(self, upload: _Upload) -> None:
        directory = self._completed_fd(upload, create=False)
        try:
            name = f"{upload.artifact_kind}-{upload.artifact_id}.bin"
            metadata = self._file_metadata(directory, name)
            if metadata.st_size != upload.size:
                raise AgentUploadMismatch("completed artifact does not match")
            digest = hashlib.sha256()
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "rb", closefd=True) as source:
                    descriptor = -1
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if not hmac.compare_digest(
                base64.b64encode(digest.digest()).decode("ascii"), upload.sha256_b64
            ):
                raise AgentUploadMismatch("completed artifact does not match")
        except AgentUploadMismatch:
            raise
        except OSError:
            raise AgentUploadMismatch("completed artifact does not match") from None
        finally:
            os.close(directory)

    def _owned_upload(self, access: AgentExecutionAccess, upload_id: UUID) -> _Upload:
        upload = self._uploads.get(upload_id)
        if (
            upload is None
            or upload.team_id != access.team_id
            or upload.analysis_id != access.analysis_id
            or upload.agent_id != access.agent_id
            or upload.execution_id != access.execution_id
            or upload.lease_version != access.lease_version
        ):
            raise AgentUploadNotFound("upload was not found")
        return upload

    def _upload_parts_fd(self, upload: _Upload, *, create: bool) -> int:
        analysis = self._analysis_fd(upload.team_id, upload.analysis_id, create=create)
        try:
            artifacts = self._open_directory(analysis, "agent-artifacts", create=create)
            try:
                parts = self._open_directory(artifacts, "parts", create=create)
                try:
                    return self._open_directory(parts, str(upload.upload_id), create=create)
                finally:
                    os.close(parts)
            finally:
                os.close(artifacts)
        finally:
            os.close(analysis)

    def _completed_fd(self, upload: _Upload, *, create: bool) -> int:
        analysis = self._analysis_fd(upload.team_id, upload.analysis_id, create=create)
        try:
            artifacts = self._open_directory(analysis, "agent-artifacts", create=create)
            try:
                return self._open_directory(artifacts, "completed", create=create)
            finally:
                os.close(artifacts)
        finally:
            os.close(analysis)

    @staticmethod
    def _slot(upload: _Upload) -> AgentUploadSlot:
        return AgentUploadSlot(
            artifact_id=upload.artifact_id,
            upload_id=upload.upload_id,
            artifact_kind=upload.artifact_kind,
            mime=upload.mime,
            size=upload.size,
            sha256_b64=upload.sha256_b64,
            part_size_bytes=_PART_BYTES,
            part_count=upload.part_count,
            state="finalized" if upload.finalized_at is not None else "pending",
            expires_at=upload.expires_at,
            finalized_at=upload.finalized_at,
        )

    def _save_state(self, team_id: UUID, analysis_id: UUID) -> None:
        uploads = [
            self._upload_document(item)
            for item in self._uploads.values()
            if item.team_id == team_id and item.analysis_id == analysis_id
        ]
        inputs = [
            {
                "team_id": str(item.team_id),
                "analysis_id": str(item.analysis_id),
                "artifact_id": str(item.artifact_id),
                "upload_id": str(item.upload_id),
                "mime": item.mime,
                "size": item.size,
                "sha256_b64": item.sha256_b64,
            }
            for item in self._inputs.values()
            if item.team_id == team_id and item.analysis_id == analysis_id
        ]
        payload = canonical_json_bytes(
            {"schema_version": "1.0", "inputs": inputs, "uploads": uploads}
        )
        directory = self._subdir_fd(team_id, analysis_id, "agent-artifacts", create=True)
        temporary = f".state.{uuid4().hex}.tmp"
        try:
            try:
                state_status = os.stat("state.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                state_status = None
            if state_status is not None and not stat.S_ISREG(state_status.st_mode):
                raise OSError
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, "state.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except OSError:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            raise AgentUploadUnavailable("local artifact storage is unavailable") from None
        finally:
            os.close(directory)

    @staticmethod
    def _upload_document(item: _Upload) -> dict[str, object]:
        return {
            "team_id": str(item.team_id),
            "analysis_id": str(item.analysis_id),
            "agent_id": str(item.agent_id),
            "execution_id": str(item.execution_id),
            "lease_version": item.lease_version,
            "artifact_id": str(item.artifact_id),
            "upload_id": str(item.upload_id),
            "artifact_kind": item.artifact_kind,
            "mime": item.mime,
            "size": item.size,
            "sha256_b64": item.sha256_b64,
            "part_count": item.part_count,
            "expires_at": item.expires_at.isoformat(),
            "parts": [
                {"part_number": number, "etag": etag}
                for number, etag in sorted(item.parts.items())
            ],
            "finalized_at": (
                None if item.finalized_at is None else item.finalized_at.isoformat()
            ),
        }

    def _recover(self) -> None:
        # State is analysis-private. A malformed or path-shaped entry rejects startup.
        root = Path(self._root)
        for state in root.glob("*/analyses/*/agent-artifacts/state.json"):
            if state.is_symlink() or not state.is_file():
                raise OSError
            team_id = UUID(state.parents[3].name)
            analysis_id = UUID(state.parents[1].name)
            if str(team_id) != state.parents[3].name or str(analysis_id) != state.parents[1].name:
                raise ValueError
            document = json.loads(state.read_bytes())
            if not isinstance(document, Mapping) or document.get("schema_version") != "1.0":
                raise ValueError
            for raw in document.get("inputs", []):
                item = _Input(
                    UUID(raw["team_id"]),
                    UUID(raw["analysis_id"]),
                    UUID(raw["artifact_id"]),
                    UUID(raw["upload_id"]),
                    raw["mime"],
                    raw["size"],
                    self._canonical_checksum(raw["sha256_b64"]),
                )
                if item.team_id != team_id or item.analysis_id != analysis_id:
                    raise ValueError
                self._inputs[item.artifact_id] = item
            for raw in document.get("uploads", []):
                item = _Upload(
                    UUID(raw["team_id"]),
                    UUID(raw["analysis_id"]),
                    UUID(raw["agent_id"]),
                    UUID(raw["execution_id"]),
                    raw["lease_version"],
                    UUID(raw["artifact_id"]),
                    UUID(raw["upload_id"]),
                    raw["artifact_kind"],
                    raw["mime"],
                    raw["size"],
                    self._canonical_checksum(raw["sha256_b64"]),
                    raw["part_count"],
                    datetime.fromisoformat(raw["expires_at"]),
                    {part["part_number"]: part["etag"] for part in raw["parts"]},
                    (
                        None
                        if raw["finalized_at"] is None
                        else datetime.fromisoformat(raw["finalized_at"])
                    ),
                )
                if (
                    item.team_id != team_id
                    or item.analysis_id != analysis_id
                    or item.artifact_kind not in _ALLOWED_KINDS
                    or item.part_count != math.ceil(item.size / _PART_BYTES)
                ):
                    raise ValueError
                self._uploads[item.upload_id] = item
                self._artifacts[item.artifact_id] = item

    async def validate_completion(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
    ) -> None:
        uploads = tuple(
            item
            for item in self._uploads.values()
            if item.execution_id == access.execution_id and item.finalized_at is not None
        )
        by_id = {item.artifact_id: item for item in uploads}
        if set(by_id) != {item.artifact_id for item in manifest.artifacts}:
            raise AgentUploadMismatch("execution artifacts do not match finalized uploads")
        scenario_artifacts = {
            artifact_id for scenario in manifest.scenarios for artifact_id in scenario.artifact_ids
        }
        if not scenario_artifacts.issubset(by_id):
            raise AgentUploadMismatch("scenario artifacts do not match finalized uploads")
        for artifact in manifest.artifacts:
            item = by_id[artifact.artifact_id]
            if (
                item.team_id != access.team_id
                or item.analysis_id != access.analysis_id
                or item.agent_id != access.agent_id
                or item.artifact_kind != artifact.kind
                or item.mime != artifact.mime
                or item.size != artifact.size
                or not hmac.compare_digest(item.sha256_b64, artifact.sha256_b64)
            ):
                raise AgentUploadMismatch("execution artifacts do not match finalized uploads")

    async def project_completion(self, **kwargs: object) -> None:
        del kwargs

    async def abort_execution(self, *, access: AgentExecutionAccess, now: datetime) -> None:
        del now
        async with self._lock:
            for upload in tuple(self._uploads.values()):
                if (
                    upload.execution_id == access.execution_id
                    and upload.team_id == access.team_id
                    and upload.analysis_id == access.analysis_id
                    and upload.agent_id == access.agent_id
                    and upload.finalized_at is None
                ):
                    directory = self._upload_parts_fd(upload, create=False)
                    try:
                        for name in os.listdir(directory):
                            os.unlink(name, dir_fd=directory)
                    finally:
                        os.close(directory)
                    parts = self._subdir_fd(
                        upload.team_id, upload.analysis_id, "agent-artifacts", create=False
                    )
                    try:
                        parent = self._open_directory(parts, "parts", create=False)
                        try:
                            os.rmdir(str(upload.upload_id), dir_fd=parent)
                        finally:
                            os.close(parent)
                    finally:
                        os.close(parts)
                    self._uploads.pop(upload.upload_id, None)
                    self._artifacts.pop(upload.artifact_id, None)
            self._save_state(access.team_id, access.analysis_id)

    async def project_cancellation(self, **kwargs: object) -> None:
        if kwargs.get("reason_code") != "analysis_canceled":
            raise AgentUploadInvalidRequest("cancellation reason is invalid")


__all__ = ["LocalAgentArtifactService", "LocalArtifactBody"]
