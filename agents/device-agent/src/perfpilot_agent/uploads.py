from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from perfpilot_agent.control_client import (
    InputAuthorizationResponse,
    UploadPartAuthorizationResponse,
    UploadPartReceipt,
    UploadSlotResponse,
)
from perfpilot_agent.security import TaskInputArtifact

ArtifactKind = Literal["startup_trace", "scroll_trace", "memory_evidence", "agent_log"]
_MAXIMUM_CHECKPOINT_BYTES = 128 * 1024
_MAXIMUM_UPLOAD_PARTS = 32
_MIME_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


class ArtifactTransferError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent artifact transfer failed")


def _canonical_sha256(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("artifact checksum is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("artifact checksum is invalid")
    return value


def hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        raise ArtifactTransferError from None
    if size < 1:
        raise ArtifactTransferError
    return size, base64.b64encode(digest.digest()).decode("ascii")


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    kind: ArtifactKind
    mime: str
    path: Path = field(repr=False)
    size: int
    sha256_b64: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.kind not in {"startup_trace", "scroll_trace", "memory_evidence", "agent_log"}
            or _MIME_TYPE.fullmatch(self.mime) is None
            or not self.path.is_absolute()
            or isinstance(self.size, bool)
            or not 1 <= self.size <= 512 * 1024 * 1024
        ):
            raise ValueError("artifact descriptor is invalid")
        _canonical_sha256(self.sha256_b64)


def describe_artifact(*, kind: ArtifactKind, mime: str, path: Path) -> ArtifactDescriptor:
    resolved = path.resolve(strict=True)
    size, checksum = hash_file(resolved)
    return ArtifactDescriptor(
        kind=kind,
        mime=mime,
        path=resolved,
        size=size,
        sha256_b64=checksum,
    )


@dataclass(frozen=True, slots=True)
class UploadedArtifact:
    artifact_id: UUID
    kind: ArtifactKind
    mime: str
    size: int
    sha256_b64: str = field(repr=False)


class UploadCheckpointPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    part_number: int = Field(strict=True, ge=1, le=_MAXIMUM_UPLOAD_PARTS)
    etag: str = Field(min_length=1, max_length=1_024, pattern=r"^[^\x00-\x1f\x7f]+$", repr=False)


class UploadCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    upload_id: UUID
    artifact_id: UUID
    artifact_kind: ArtifactKind
    size: int = Field(strict=True, ge=1, le=512 * 1024 * 1024)
    sha256_b64: str = Field(repr=False)
    part_size_bytes: int = Field(strict=True, ge=1, le=512 * 1024 * 1024)
    part_count: int = Field(strict=True, ge=1, le=_MAXIMUM_UPLOAD_PARTS)
    parts: tuple[UploadCheckpointPart, ...] = Field(max_length=_MAXIMUM_UPLOAD_PARTS)

    @field_validator("sha256_b64")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        return _canonical_sha256(value)

    @model_validator(mode="after")
    def validate_parts(self) -> Self:
        expected_count = (self.size + self.part_size_bytes - 1) // self.part_size_bytes
        if expected_count != self.part_count or any(
            part.part_number != index for index, part in enumerate(self.parts, start=1)
        ):
            raise ValueError("upload checkpoint is invalid")
        return self


def load_upload_checkpoint(path: Path) -> UploadCheckpoint | None:
    try:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ArtifactTransferError
        with path.open("rb") as source:
            payload = source.read(_MAXIMUM_CHECKPOINT_BYTES + 1)
        if not payload or len(payload) > _MAXIMUM_CHECKPOINT_BYTES:
            raise ArtifactTransferError
        return UploadCheckpoint.model_validate_json(payload)
    except ArtifactTransferError:
        raise
    except (OSError, ValidationError, ValueError, TypeError, UnicodeError):
        raise ArtifactTransferError from None


def save_upload_checkpoint(path: Path, checkpoint: UploadCheckpoint) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = checkpoint.model_dump_json().encode("utf-8")
        if len(payload) > _MAXIMUM_CHECKPOINT_BYTES:
            raise ArtifactTransferError
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except ArtifactTransferError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError:
        temporary.unlink(missing_ok=True)
        raise ArtifactTransferError from None


class InputControl(Protocol):
    async def authorize_input(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        artifact_id: UUID,
    ) -> InputAuthorizationResponse: ...


class InputDownloader:
    def __init__(
        self,
        *,
        control: InputControl,
        workspace_root: Path,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._control = control
        self._workspace_root = workspace_root.resolve(strict=False)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _safe_target(self, target: Path) -> Path:
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(self._workspace_root):
            raise ArtifactTransferError
        return resolved

    async def download(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        artifact: TaskInputArtifact,
        target: Path,
    ) -> Path:
        destination = self._safe_target(target)
        temporary = destination.with_name(f"{destination.name}.part")
        try:
            authorization = await self._control.authorize_input(
                execution_id=execution_id,
                lease_version=lease_version,
                artifact_id=artifact.artifact_id,
            )
            if (
                authorization.artifact_id != artifact.artifact_id
                or authorization.mime != artifact.mime
                or authorization.size != artifact.size
                or authorization.sha256_b64 != artifact.sha256_b64
            ):
                raise ArtifactTransferError
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                async with self._client.stream("GET", authorization.download_url) as response:
                    if response.status_code != 200:
                        raise ArtifactTransferError
                    length = response.headers.get("content-length")
                    if length is not None and int(length) != artifact.size:
                        raise ArtifactTransferError
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > artifact.size:
                            raise ArtifactTransferError
                        digest.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            observed = base64.b64encode(digest.digest()).decode("ascii")
            if size != artifact.size or observed != artifact.sha256_b64:
                raise ArtifactTransferError
            os.replace(temporary, destination)
            return destination
        except asyncio.CancelledError:
            temporary.unlink(missing_ok=True)
            raise
        except ArtifactTransferError:
            temporary.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, UnicodeError):
            temporary.unlink(missing_ok=True)
            raise ArtifactTransferError from None


class UploadControl(Protocol):
    async def create_upload(self, **kwargs: object) -> UploadSlotResponse: ...

    async def authorize_upload_part(self, **kwargs: object) -> UploadPartAuthorizationResponse: ...

    async def complete_upload(self, **kwargs: object) -> UploadSlotResponse: ...


class MultipartUploader:
    def __init__(
        self,
        *,
        control: UploadControl,
        checkpoint_path: Path,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._control = control
        self._checkpoint_path = checkpoint_path
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _uploaded(slot: UploadSlotResponse) -> UploadedArtifact:
        return UploadedArtifact(
            artifact_id=slot.artifact_id,
            kind=slot.artifact_kind,
            mime=slot.mime,
            size=slot.size,
            sha256_b64=slot.sha256_b64,
        )

    @staticmethod
    def _matches(
        checkpoint: UploadCheckpoint,
        slot: UploadSlotResponse,
        descriptor: ArtifactDescriptor,
    ) -> bool:
        return (
            checkpoint.upload_id == slot.upload_id
            and checkpoint.artifact_id == slot.artifact_id
            and checkpoint.artifact_kind == descriptor.kind
            and checkpoint.size == descriptor.size
            and checkpoint.sha256_b64 == descriptor.sha256_b64
            and checkpoint.part_size_bytes == slot.part_size_bytes
            and checkpoint.part_count == slot.part_count
        )

    @staticmethod
    def _read_part(path: Path, *, offset: int, size: int) -> bytes:
        try:
            with path.open("rb") as source:
                source.seek(offset)
                payload = source.read(size)
            if len(payload) != size:
                raise ArtifactTransferError
            return payload
        except ArtifactTransferError:
            raise
        except OSError:
            raise ArtifactTransferError from None

    async def upload(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        descriptor: ArtifactDescriptor,
    ) -> UploadedArtifact:
        observed_size, observed_hash = hash_file(descriptor.path)
        if observed_size != descriptor.size or observed_hash != descriptor.sha256_b64:
            raise ArtifactTransferError
        slot = await self._control.create_upload(
            execution_id=execution_id,
            lease_version=lease_version,
            artifact_kind=descriptor.kind,
            mime=descriptor.mime,
            size=descriptor.size,
            sha256_b64=descriptor.sha256_b64,
        )
        if slot.part_count > _MAXIMUM_UPLOAD_PARTS:
            raise ArtifactTransferError
        if slot.state == "finalized":
            self._checkpoint_path.unlink(missing_ok=True)
            return self._uploaded(slot)
        if slot.state != "pending":
            raise ArtifactTransferError
        checkpoint = load_upload_checkpoint(self._checkpoint_path)
        if checkpoint is None or not self._matches(checkpoint, slot, descriptor):
            checkpoint = UploadCheckpoint(
                schema_version="1.0",
                upload_id=slot.upload_id,
                artifact_id=slot.artifact_id,
                artifact_kind=descriptor.kind,
                size=descriptor.size,
                sha256_b64=descriptor.sha256_b64,
                part_size_bytes=slot.part_size_bytes,
                part_count=slot.part_count,
                parts=(),
            )
            save_upload_checkpoint(self._checkpoint_path, checkpoint)
        completed = list(checkpoint.parts)
        try:
            for part_number in range(len(completed) + 1, slot.part_count + 1):
                offset = (part_number - 1) * slot.part_size_bytes
                length = min(slot.part_size_bytes, descriptor.size - offset)
                authorization = await self._control.authorize_upload_part(
                    execution_id=execution_id,
                    lease_version=lease_version,
                    upload_id=slot.upload_id,
                    part_number=part_number,
                )
                payload = self._read_part(descriptor.path, offset=offset, size=length)
                response = await self._client.put(
                    authorization.put_url,
                    content=payload,
                    headers=authorization.required_headers,
                )
                if response.status_code not in {200, 201, 204}:
                    raise ArtifactTransferError
                etags = response.headers.get_list("etag")
                if len(etags) != 1:
                    raise ArtifactTransferError
                receipt = UploadCheckpointPart(part_number=part_number, etag=etags[0])
                completed.append(receipt)
                checkpoint = checkpoint.model_copy(update={"parts": tuple(completed)})
                checkpoint = UploadCheckpoint.model_validate(checkpoint)
                save_upload_checkpoint(self._checkpoint_path, checkpoint)
            receipts = tuple(
                UploadPartReceipt(part_number=part.part_number, etag=part.etag)
                for part in completed
            )
            finalized = await self._control.complete_upload(
                execution_id=execution_id,
                lease_version=lease_version,
                upload_id=slot.upload_id,
                parts=receipts,
            )
            if (
                finalized.artifact_id != slot.artifact_id
                or finalized.artifact_kind != descriptor.kind
                or finalized.mime != descriptor.mime
                or finalized.size != descriptor.size
                or finalized.sha256_b64 != descriptor.sha256_b64
            ):
                raise ArtifactTransferError
            self._checkpoint_path.unlink(missing_ok=True)
            return self._uploaded(finalized)
        except asyncio.CancelledError:
            raise
        except ArtifactTransferError:
            raise
        except (httpx.HTTPError, OSError, ValidationError, ValueError, TypeError, UnicodeError):
            raise ArtifactTransferError from None


__all__ = [
    "ArtifactDescriptor",
    "ArtifactKind",
    "ArtifactTransferError",
    "InputDownloader",
    "MultipartUploader",
    "UploadCheckpoint",
    "UploadCheckpointPart",
    "UploadedArtifact",
    "describe_artifact",
    "hash_file",
    "load_upload_checkpoint",
    "save_upload_checkpoint",
]
