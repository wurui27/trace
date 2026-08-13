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
import sys
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
    AgentUploadError,
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
_MAX_STATE_BYTES = 1024 * 1024
_MIME = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_ALLOWED_KINDS = frozenset({"startup_trace", "scroll_trace", "agent_log"})
_ETAG = re.compile(r'"[0-9a-f]{64}"\Z')
_PART_TEMP = re.compile(r"\.([1-9][0-9]?)\.([0-9a-f]{32})\.tmp\Z")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True, slots=True)
class LocalArtifactBody:
    body: Iterable[bytes] = field(repr=False)
    mime: str
    size: int
    etag: str = field(repr=False)


class AgentUploadCommittedError(AgentUploadUnavailable):
    """The state replacement committed, but directory durability is uncertain."""

    committed = True


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
        self._upload_locks: dict[UUID, asyncio.Lock] = {}
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
    def _document_uuid(value: object) -> UUID:
        if not isinstance(value, str):
            raise ValueError
        parsed = UUID(value)
        if str(parsed) != value or parsed.version not in range(1, 6):
            raise ValueError
        return parsed

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

    @classmethod
    def _artifact_metadata(
        cls, *, mime: object, size: object, sha256_b64: object
    ) -> tuple[str, int, str]:
        if (
            not isinstance(mime, str)
            or _MIME.fullmatch(mime) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= _MAX_BYTES
            or not isinstance(sha256_b64, str)
        ):
            raise AgentUploadInvalidRequest("artifact request is invalid")
        return mime, size, cls._canonical_checksum(sha256_b64)

    @classmethod
    def _upload_metadata(
        cls,
        *,
        artifact_kind: object,
        mime: object,
        size: object,
        sha256_b64: object,
    ) -> tuple[str, str, int, str, int]:
        if not isinstance(artifact_kind, str) or artifact_kind not in _ALLOWED_KINDS:
            raise AgentUploadInvalidRequest("upload request is invalid")
        valid_mime, valid_size, checksum = cls._artifact_metadata(
            mime=mime, size=size, sha256_b64=sha256_b64
        )
        part_count = math.ceil(valid_size / _PART_BYTES)
        if not 1 <= part_count <= _MAX_PARTS:
            raise AgentUploadInvalidRequest("upload request is invalid")
        return artifact_kind, valid_mime, valid_size, checksum, part_count

    @staticmethod
    def _aware_document_time(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)

    @staticmethod
    def _file_metadata(directory_fd: int, name: str) -> os.stat_result:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not LocalAgentArtifactService._is_private_regular(metadata):
            raise OSError
        return metadata

    @staticmethod
    def _is_private_regular(metadata: os.stat_result) -> bool:
        return stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600

    @staticmethod
    def _stream_descriptor(descriptor: int, directory_fd: int) -> Iterable[bytes]:
        try:
            metadata = os.fstat(descriptor)
            if not LocalAgentArtifactService._is_private_regular(metadata):
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
        valid_mime, valid_size, checksum = self._artifact_metadata(
            mime=mime, size=size, sha256_b64=sha256_b64
        )
        if valid_mime != "application/vnd.android.package-archive":
            raise AgentUploadInvalidRequest("artifact request is invalid")
        directory = self._subdir_fd(team_id, analysis_id, "uploads", create=False)
        try:
            metadata = self._file_metadata(directory, f"{upload_id}.bin")
            if metadata.st_size != valid_size:
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
        item = _Input(
            team_id,
            analysis_id,
            artifact_id,
            upload_id,
            valid_mime,
            valid_size,
            checksum,
        )
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
            if not self._is_private_regular(metadata):
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
        valid_kind, valid_mime, valid_size, checksum, part_count = self._upload_metadata(
            artifact_kind=artifact_kind,
            mime=mime,
            size=size,
            sha256_b64=sha256_b64,
        )
        now = self._now()
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        if valid_kind not in access.allowed_uploads:
            raise AgentUploadInvalidRequest("artifact kind is not allowed by this task")
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self._uploads.values()
                    if item.execution_id == execution_id
                    and item.artifact_kind == valid_kind
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.team_id != access.team_id
                    or existing.analysis_id != access.analysis_id
                    or existing.agent_id != agent_id
                    or existing.lease_version != lease_version
                    or existing.mime != valid_mime
                    or existing.size != valid_size
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
            expires_at = min(now + timedelta(minutes=15), access.lease_expires_at)
            upload = _Upload(
                access.team_id,
                access.analysis_id,
                agent_id,
                execution_id,
                lease_version,
                artifact_id,
                upload_id,
                valid_kind,
                valid_mime,
                valid_size,
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
            self._upload_locks[upload_id] = asyncio.Lock()
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
        access = await self._authorize(
            agent_id=grant.agent_id,
            execution_id=grant.execution_id,
            lease_version=grant.lease_version,
            now=self._now(),
        )
        upload = self._uploads.get(grant.upload_id)
        if (
            upload is None
            or upload.team_id != grant.team_id
            or upload.analysis_id != grant.analysis_id
            or upload.agent_id != grant.agent_id
            or upload.execution_id != grant.execution_id
            or upload.lease_version != grant.lease_version
            or grant.part_number is None
            or access.team_id != grant.team_id
            or access.analysis_id != grant.analysis_id
        ):
            raise AgentUploadNotFound("upload part was not found")
        expected = min(
            _PART_BYTES,
            upload.size - (grant.part_number - 1) * _PART_BYTES,
        )
        upload_lock = self._upload_locks.setdefault(upload.upload_id, asyncio.Lock())
        async with upload_lock:
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
                                size = await asyncio.to_thread(
                                    self._write_chunk,
                                    output,
                                    digest,
                                    chunk,
                                    size,
                                    expected,
                                )
                        else:
                            for chunk in chunks:  # type: ignore[union-attr]
                                size = await asyncio.to_thread(
                                    self._write_chunk,
                                    output,
                                    digest,
                                    chunk,
                                    size,
                                    expected,
                                )
                        await asyncio.to_thread(self._flush_file, output)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                if size != expected:
                    raise AgentUploadMismatch("upload part size does not match")
                etag = f'"{digest.hexdigest()}"'
                publish_access = await self._authorize(
                    agent_id=grant.agent_id,
                    execution_id=grant.execution_id,
                    lease_version=grant.lease_version,
                    now=self._now(),
                )
                self._owned_upload(publish_access, upload.upload_id)
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
                try:
                    self._save_state(upload.team_id, upload.analysis_id)
                except AgentUploadCommittedError:
                    raise
                except AgentUploadError:
                    upload.parts.pop(grant.part_number, None)
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                    raise
                return etag
            except AgentUploadError:
                raise
            except OSError:
                raise AgentUploadUnavailable("local artifact storage is unavailable") from None
            except Exception:
                raise AgentUploadUnavailable("local artifact storage is unavailable") from None
            finally:
                active_error = sys.exception()
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    if active_error is None:
                        raise AgentUploadUnavailable(
                            "local artifact storage is unavailable"
                        ) from None
                finally:
                    os.close(directory)

    @staticmethod
    def _flush_file(output: object) -> None:
        output.flush()  # type: ignore[attr-defined]
        os.fsync(output.fileno())  # type: ignore[attr-defined]

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
        upload_lock = self._upload_locks.setdefault(upload.upload_id, asyncio.Lock())
        async with upload_lock:
            if upload.finalized_at is not None:
                await asyncio.to_thread(self._verify_completed, upload)
                return self._slot(upload)
            if upload.expires_at <= self._now():
                raise AgentUploadExpired("upload has expired")
            parts_fd = self._upload_parts_fd(upload, create=False)
            completed_fd = self._completed_fd(upload, create=False)
            temporary = f".{upload.artifact_id}.{uuid4().hex}.tmp"
            final_name = f"{upload.artifact_kind}-{upload.artifact_id}.bin"
            try:
                size, checksum = await asyncio.to_thread(
                    self._assemble_upload,
                    parts_fd,
                    completed_fd,
                    temporary,
                    upload,
                )
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
                try:
                    self._save_state(upload.team_id, upload.analysis_id)
                except AgentUploadCommittedError:
                    raise
                except AgentUploadError:
                    upload.finalized_at = None
                    os.unlink(final_name, dir_fd=completed_fd)
                    os.fsync(completed_fd)
                    raise
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

    def _assemble_upload(
        self,
        parts_fd: int,
        completed_fd: int,
        temporary: str,
        upload: _Upload,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
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
                    if not self._is_private_regular(os.fstat(source.fileno())):
                        raise OSError
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > upload.size:
                            raise AgentUploadMismatch("uploaded artifact does not match")
                        digest.update(chunk)
                        output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        return size, base64.b64encode(digest.digest()).decode("ascii")

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
        committed = False
        try:
            try:
                state_status = os.stat("state.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                state_status = None
            if state_status is not None and not self._is_private_regular(state_status):
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
            committed = True
            os.fsync(directory)
        except OSError:
            if committed:
                raise AgentUploadCommittedError(
                    "local artifact storage is unavailable"
                ) from None
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

    @staticmethod
    def _directory_entries(directory_fd: int) -> tuple[str, ...]:
        entries = os.listdir(directory_fd)
        if any(not isinstance(name, str) or name in {".", ".."} for name in entries):
            raise OSError
        return tuple(sorted(entries))

    @staticmethod
    def _read_bounded_regular(directory_fd: int, name: str, limit: int) -> bytes:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not LocalAgentArtifactService._is_private_regular(metadata)
                or not 0 < metadata.st_size <= limit
            ):
                raise OSError
            payload = b""
            while len(payload) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
            if not payload or len(payload) > limit:
                raise OSError
            return payload
        finally:
            os.close(descriptor)

    @staticmethod
    def _parse_state(payload: bytes) -> Mapping[str, object]:
        def reject_constant(_value: str) -> object:
            raise ValueError

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        document = json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
        if (
            not isinstance(document, Mapping)
            or set(document) != {"schema_version", "inputs", "uploads"}
            or document.get("schema_version") != "1.0"
            or not isinstance(document.get("inputs"), list)
            or not isinstance(document.get("uploads"), list)
        ):
            raise ValueError
        return document

    def _recover_input(
        self,
        raw: object,
        *,
        team_id: UUID,
        analysis_id: UUID,
        analysis_fd: int,
    ) -> _Input:
        if not isinstance(raw, Mapping) or set(raw) != {
            "team_id",
            "analysis_id",
            "artifact_id",
            "upload_id",
            "mime",
            "size",
            "sha256_b64",
        }:
            raise ValueError
        item_team = self._document_uuid(raw["team_id"])
        item_analysis = self._document_uuid(raw["analysis_id"])
        artifact_id = self._document_uuid(raw["artifact_id"])
        upload_id = self._document_uuid(raw["upload_id"])
        mime, size, checksum = self._artifact_metadata(
            mime=raw["mime"], size=raw["size"], sha256_b64=raw["sha256_b64"]
        )
        if (
            item_team != team_id
            or item_analysis != analysis_id
            or mime != "application/vnd.android.package-archive"
        ):
            raise ValueError
        uploads_fd = self._open_directory(analysis_fd, "uploads", create=False)
        try:
            self._verify_file(
                uploads_fd,
                f"{upload_id}.bin",
                expected_size=size,
                expected_sha256_b64=checksum,
            )
        finally:
            os.close(uploads_fd)
        return _Input(
            item_team,
            item_analysis,
            artifact_id,
            upload_id,
            mime,
            size,
            checksum,
        )

    def _recover_upload(
        self,
        raw: object,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifacts_fd: int,
    ) -> _Upload | None:
        if not isinstance(raw, Mapping) or set(raw) != {
            "team_id",
            "analysis_id",
            "agent_id",
            "execution_id",
            "lease_version",
            "artifact_id",
            "upload_id",
            "artifact_kind",
            "mime",
            "size",
            "sha256_b64",
            "part_count",
            "expires_at",
            "parts",
            "finalized_at",
        }:
            raise ValueError
        item_team = self._document_uuid(raw["team_id"])
        item_analysis = self._document_uuid(raw["analysis_id"])
        agent_id = self._document_uuid(raw["agent_id"])
        execution_id = self._document_uuid(raw["execution_id"])
        artifact_id = self._document_uuid(raw["artifact_id"])
        upload_id = self._document_uuid(raw["upload_id"])
        kind, mime, size, checksum, expected_count = self._upload_metadata(
            artifact_kind=raw["artifact_kind"],
            mime=raw["mime"],
            size=raw["size"],
            sha256_b64=raw["sha256_b64"],
        )
        lease_version = raw["lease_version"]
        part_count = raw["part_count"]
        expires_at = self._aware_document_time(raw["expires_at"])
        now = self._now()
        if (
            item_team != team_id
            or item_analysis != analysis_id
            or isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version < 1
            or isinstance(part_count, bool)
            or not isinstance(part_count, int)
            or part_count != expected_count
            or not 1 <= part_count <= _MAX_PARTS
        ):
            raise ValueError
        finalized_raw = raw["finalized_at"]
        finalized_at = (
            None
            if finalized_raw is None
            else self._aware_document_time(finalized_raw)
        )
        if finalized_at is not None and (
            finalized_at > now or finalized_at > expires_at
        ):
            raise ValueError
        parts_raw = raw["parts"]
        if not isinstance(parts_raw, list) or len(parts_raw) > part_count:
            raise ValueError
        parts: dict[int, str] = {}
        for number, part in enumerate(parts_raw, start=1):
            if (
                not isinstance(part, Mapping)
                or set(part) != {"part_number", "etag"}
                or part.get("part_number") != number
                or not isinstance(part.get("etag"), str)
                or _ETAG.fullmatch(part["etag"]) is None
            ):
                raise ValueError
            parts[number] = part["etag"]
        upload = _Upload(
            item_team,
            item_analysis,
            agent_id,
            execution_id,
            lease_version,
            artifact_id,
            upload_id,
            kind,
            mime,
            size,
            checksum,
            part_count,
            expires_at,
            parts,
            finalized_at,
        )
        parts_fd = self._open_directory(artifacts_fd, "parts", create=False)
        try:
            upload_fd = self._open_directory(parts_fd, str(upload_id), create=False)
            try:
                actual_entries = self._directory_entries(upload_fd)
                expected_entries = tuple(f"{number}.part" for number in parts)
                for name in actual_entries:
                    if name not in expected_entries:
                        self._remove_owned_part_temp(
                            upload_fd, name, part_count=part_count
                        )
                if self._directory_entries(upload_fd) != expected_entries:
                    raise ValueError
                for number, etag in parts.items():
                    self._verify_file(
                        upload_fd,
                        f"{number}.part",
                        expected_size=min(
                            _PART_BYTES, size - (number - 1) * _PART_BYTES
                        ),
                        expected_hex_digest=etag[1:-1],
                    )
            finally:
                os.close(upload_fd)
            completed_fd = self._open_directory(artifacts_fd, "completed", create=False)
            try:
                completed_name = f"{kind}-{artifact_id}.bin"
                if finalized_at is None:
                    try:
                        os.stat(completed_name, dir_fd=completed_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError
                else:
                    self._verify_file(
                        completed_fd,
                        completed_name,
                        expected_size=size,
                        expected_sha256_b64=checksum,
                    )
            finally:
                os.close(completed_fd)
            if finalized_at is None and expires_at <= now:
                upload_fd = self._open_directory(parts_fd, str(upload_id), create=False)
                try:
                    for name in self._directory_entries(upload_fd):
                        os.unlink(name, dir_fd=upload_fd)
                    os.fsync(upload_fd)
                finally:
                    os.close(upload_fd)
                os.rmdir(str(upload_id), dir_fd=parts_fd)
                os.fsync(parts_fd)
                return None
        finally:
            os.close(parts_fd)
        return upload

    @staticmethod
    def _remove_owned_part_temp(
        directory_fd: int, name: str, *, part_count: int
    ) -> None:
        matched = _PART_TEMP.fullmatch(name)
        if matched is None:
            raise ValueError
        part_number = int(matched.group(1))
        temporary_id = UUID(hex=matched.group(2))
        if part_number > part_count or temporary_id.version != 4:
            raise ValueError
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            held = os.fstat(descriptor)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not LocalAgentArtifactService._is_private_regular(held)
                or not stat.S_ISREG(current.st_mode)
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise OSError
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(descriptor)

    @staticmethod
    def _verify_file(
        directory_fd: int,
        name: str,
        *,
        expected_size: int,
        expected_sha256_b64: str | None = None,
        expected_hex_digest: str | None = None,
    ) -> None:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not LocalAgentArtifactService._is_private_regular(metadata)
                or metadata.st_size != expected_size
            ):
                raise OSError
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                if size > expected_size:
                    raise OSError
                digest.update(chunk)
            if size != expected_size:
                raise OSError
            if expected_sha256_b64 is not None and not hmac.compare_digest(
                base64.b64encode(digest.digest()).decode("ascii"), expected_sha256_b64
            ):
                raise OSError
            if expected_hex_digest is not None and not hmac.compare_digest(
                digest.hexdigest(), expected_hex_digest
            ):
                raise OSError
        finally:
            os.close(descriptor)

    def _recover(self) -> None:
        self._verify_root()
        recovered_inputs: dict[UUID, _Input] = {}
        recovered_uploads: dict[UUID, _Upload] = {}
        recovered_artifacts: dict[UUID, _Upload] = {}
        cleaned_analyses: set[tuple[UUID, UUID]] = set()
        for team_name in self._directory_entries(self._root_fd):
            team_id = self._document_uuid(team_name)
            team_fd = self._open_directory(self._root_fd, team_name, create=False)
            try:
                if self._directory_entries(team_fd) != ("analyses",):
                    raise ValueError
                analyses_fd = self._open_directory(team_fd, "analyses", create=False)
                try:
                    for analysis_name in self._directory_entries(analyses_fd):
                        analysis_id = self._document_uuid(analysis_name)
                        analysis_fd = self._open_directory(
                            analyses_fd, analysis_name, create=False
                        )
                        try:
                            entries = self._directory_entries(analysis_fd)
                            if "agent-artifacts" not in entries:
                                continue
                            artifacts_fd = self._open_directory(
                                analysis_fd, "agent-artifacts", create=False
                            )
                            try:
                                artifact_entries = self._directory_entries(artifacts_fd)
                                if (
                                    "state.json" not in artifact_entries
                                    or not set(artifact_entries).issubset(
                                        {"completed", "parts", "state.json"}
                                    )
                                ):
                                    raise ValueError
                                payload = self._read_bounded_regular(
                                    artifacts_fd, "state.json", _MAX_STATE_BYTES
                                )
                                document = self._parse_state(payload)
                                if document["uploads"] and not {
                                    "completed",
                                    "parts",
                                }.issubset(artifact_entries):
                                    raise ValueError
                                for raw in document["inputs"]:
                                    item = self._recover_input(
                                        raw,
                                        team_id=team_id,
                                        analysis_id=analysis_id,
                                        analysis_fd=analysis_fd,
                                    )
                                    if item.artifact_id in recovered_inputs:
                                        raise ValueError
                                    recovered_inputs[item.artifact_id] = item
                                for raw in document["uploads"]:
                                    item = self._recover_upload(
                                        raw,
                                        team_id=team_id,
                                        analysis_id=analysis_id,
                                        artifacts_fd=artifacts_fd,
                                    )
                                    if item is None:
                                        cleaned_analyses.add((team_id, analysis_id))
                                        continue
                                    if (
                                        item.upload_id in recovered_uploads
                                        or item.artifact_id in recovered_artifacts
                                    ):
                                        raise ValueError
                                    recovered_uploads[item.upload_id] = item
                                    recovered_artifacts[item.artifact_id] = item
                            finally:
                                os.close(artifacts_fd)
                        finally:
                            os.close(analysis_fd)
                finally:
                    os.close(analyses_fd)
            finally:
                os.close(team_fd)
        self._inputs.update(recovered_inputs)
        self._uploads.update(recovered_uploads)
        self._artifacts.update(recovered_artifacts)
        self._upload_locks.update(
            (upload_id, asyncio.Lock()) for upload_id in recovered_uploads
        )
        for team_id, analysis_id in cleaned_analyses:
            self._save_state(team_id, analysis_id)

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
        candidates = tuple(
            upload
            for upload in self._uploads.values()
            if upload.execution_id == access.execution_id
            and upload.team_id == access.team_id
            and upload.analysis_id == access.analysis_id
            and upload.agent_id == access.agent_id
        )
        for upload in candidates:
            lock = self._upload_locks.setdefault(upload.upload_id, asyncio.Lock())
            async with lock:
                if upload.finalized_at is None and self._uploads.get(upload.upload_id) is upload:
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
                    self._upload_locks.pop(upload.upload_id, None)
        self._save_state(access.team_id, access.analysis_id)

    async def project_cancellation(self, **kwargs: object) -> None:
        if kwargs.get("reason_code") != "analysis_canceled":
            raise AgentUploadInvalidRequest("cancellation reason is invalid")


__all__ = ["LocalAgentArtifactService", "LocalArtifactBody"]
