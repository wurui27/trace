"""Immutable private binding for native SmartPerfetto HTML bytes."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias
from uuid import UUID, uuid4, uuid5


MAX_SMARTPERFETTO_ORIGINAL_BYTES = 16 * 1024 * 1024
_VERSION = 2
_MIME = "text/html"
_ARTIFACT_NAMESPACE = UUID("9987841c-09df-53fa-8cad-fca8888f5d27")


class SmartPerfettoOriginalError(RuntimeError):
    pass


class SmartPerfettoOriginalNotFound(SmartPerfettoOriginalError):
    def __init__(self) -> None:
        super().__init__("smartperfetto_original_not_found")


class SmartPerfettoOriginalInvalid(SmartPerfettoOriginalError):
    def __init__(self) -> None:
        super().__init__("smartperfetto_original_invalid")


@dataclass(frozen=True, slots=True)
class SmartPerfettoOriginalBinding:
    artifact_id: UUID
    team_id: UUID
    analysis_id: UUID
    version: int
    mime: Literal["text/html"]
    size: int
    sha256: str

    def public_document(self) -> dict[str, object]:
        return {
            "available": True,
            "artifact_id": str(self.artifact_id),
            "version": self.version,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
        }

    def private_document(self) -> dict[str, object]:
        return {
            **self.public_document(),
            "team_id": str(self.team_id),
            "analysis_id": str(self.analysis_id),
        }

    @classmethod
    def from_private_document(cls, value: object) -> "SmartPerfettoOriginalBinding":
        expected = {
            "available",
            "artifact_id",
            "team_id",
            "analysis_id",
            "version",
            "mime",
            "size",
            "sha256",
        }
        try:
            if not isinstance(value, Mapping) or set(value) != expected:
                raise ValueError
            artifact_id = UUID(str(value["artifact_id"]))
            team_id = UUID(str(value["team_id"]))
            analysis_id = UUID(str(value["analysis_id"]))
            version = value["version"]
            size = value["size"]
            sha256 = value["sha256"]
            if (
                value["available"] is not True
                or version != _VERSION
                or value["mime"] != _MIME
                or type(size) is not int
                or not 0 < size <= MAX_SMARTPERFETTO_ORIGINAL_BYTES
                or not isinstance(sha256, str)
                or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)
                or str(artifact_id) != value["artifact_id"]
                or str(team_id) != value["team_id"]
                or str(analysis_id) != value["analysis_id"]
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise SmartPerfettoOriginalInvalid from None
        return cls(
            artifact_id=artifact_id,
            team_id=team_id,
            analysis_id=analysis_id,
            version=version,
            mime=_MIME,
            size=size,
            sha256=sha256,
        )


SmartPerfettoOriginalReference: TypeAlias = SmartPerfettoOriginalBinding


def restore_smartperfetto_original(value: object) -> SmartPerfettoOriginalBinding:
    return SmartPerfettoOriginalBinding.from_private_document(value)


def _artifact_path(root: Path, team_id: UUID, analysis_id: UUID, version: int) -> Path:
    return (
        root
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / f"smartperfetto-original-v{version}.html"
    )


def _safe_analysis_directory(root: Path, team_id: UUID, analysis_id: UUID) -> Path:
    if (
        not isinstance(root, Path)
        or not isinstance(team_id, UUID)
        or not isinstance(analysis_id, UUID)
    ):
        raise TypeError("root and identifiers have invalid types")
    anchor = root.resolve()
    directory = _artifact_path(anchor, team_id, analysis_id, _VERSION).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = anchor
    for component in directory.relative_to(anchor).parts:
        current = current / component
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SmartPerfettoOriginalInvalid
    return directory


def _validate_html(payload: bytes) -> bytes:
    if (
        type(payload) is not bytes
        or not 0 < len(payload) <= MAX_SMARTPERFETTO_ORIGINAL_BYTES
    ):
        raise SmartPerfettoOriginalInvalid
    prefix = payload[:4096].lstrip(b"\xef\xbb\xbf\x00\t\n\r ").lower()
    if not (prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")):
        raise SmartPerfettoOriginalInvalid
    return payload


def persist_smartperfetto_original(
    *,
    root: Path,
    team_id: UUID,
    analysis_id: UUID,
    payload: bytes,
) -> SmartPerfettoOriginalBinding:
    payload = _validate_html(payload)
    directory = _safe_analysis_directory(root, team_id, analysis_id)
    target = _artifact_path(root.resolve(), team_id, analysis_id, _VERSION)
    artifact_id = uuid5(
        _ARTIFACT_NAMESPACE,
        f"{team_id}:{analysis_id}:{_VERSION}",
    )
    expected = SmartPerfettoOriginalBinding(
        artifact_id=artifact_id,
        team_id=team_id,
        analysis_id=analysis_id,
        version=_VERSION,
        mime=_MIME,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    if target.exists() or target.is_symlink():
        existing = read_smartperfetto_original(
            root=root,
            binding=expected,
            team_id=team_id,
            analysis_id=analysis_id,
        )
        if existing != payload:
            raise SmartPerfettoOriginalInvalid
        return expected
    temporary = directory / f".smartperfetto-original-{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise SmartPerfettoOriginalInvalid from None
    return expected


def read_smartperfetto_original(
    *,
    root: Path,
    binding: SmartPerfettoOriginalBinding,
    team_id: UUID,
    analysis_id: UUID,
    maximum_bytes: int = MAX_SMARTPERFETTO_ORIGINAL_BYTES,
) -> bytes:
    if binding.team_id != team_id or binding.analysis_id != analysis_id:
        raise SmartPerfettoOriginalNotFound
    if (
        binding.version != _VERSION
        or binding.mime != _MIME
        or type(maximum_bytes) is not int
        or not 0 < maximum_bytes <= MAX_SMARTPERFETTO_ORIGINAL_BYTES
    ):
        raise SmartPerfettoOriginalInvalid
    target = _artifact_path(root.resolve(), team_id, analysis_id, binding.version)
    descriptor = -1
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size != binding.size
            or metadata.st_size > maximum_bytes
        ):
            raise ValueError
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != binding.sha256:
            raise ValueError
        return _validate_html(payload)
    except FileNotFoundError:
        raise SmartPerfettoOriginalNotFound from None
    except (OSError, ValueError):
        raise SmartPerfettoOriginalInvalid from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


__all__ = [
    "MAX_SMARTPERFETTO_ORIGINAL_BYTES",
    "SmartPerfettoOriginalBinding",
    "SmartPerfettoOriginalError",
    "SmartPerfettoOriginalInvalid",
    "SmartPerfettoOriginalNotFound",
    "SmartPerfettoOriginalReference",
    "persist_smartperfetto_original",
    "read_smartperfetto_original",
    "restore_smartperfetto_original",
]
