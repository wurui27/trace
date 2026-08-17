"""Immutable private binding for a SmartPerfetto original JSON document."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, TypeAlias
from uuid import UUID, uuid4, uuid5

from perfpilot_api.reports.contracts import canonical_json_bytes


MAX_SMARTPERFETTO_ORIGINAL_BYTES = 2 * 1024 * 1024
MAX_SMARTPERFETTO_ORIGINAL_COLLECTION_BYTES = (
    2 * MAX_SMARTPERFETTO_ORIGINAL_BYTES + 64 * 1024
)
_VERSION = 1
_MIME = "application/json"
_ARTIFACT_NAMESPACE = UUID("9987841c-09df-53fa-8cad-fca8888f5d27")
SmartPerfettoScenario = Literal["startup", "scroll"]
_SCENARIO_LABELS: dict[SmartPerfettoScenario, str] = {
    "startup": "启动",
    "scroll": "滑动",
}


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
    mime: Literal["application/json"]
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


@dataclass(frozen=True, slots=True)
class SmartPerfettoScenarioOriginalBinding:
    scenario_type: SmartPerfettoScenario
    binding: SmartPerfettoOriginalBinding

    def public_document(self) -> dict[str, object]:
        return {
            "scenario_type": self.scenario_type,
            "label": _SCENARIO_LABELS[self.scenario_type],
            **self.binding.public_document(),
        }

    def private_document(self) -> dict[str, object]:
        return {
            "scenario_type": self.scenario_type,
            "binding": self.binding.private_document(),
        }

    @classmethod
    def from_private_document(
        cls, value: object
    ) -> "SmartPerfettoScenarioOriginalBinding":
        try:
            if not isinstance(value, Mapping) or set(value) != {
                "scenario_type",
                "binding",
            }:
                raise ValueError
            scenario_type = value["scenario_type"]
            if scenario_type not in _SCENARIO_LABELS:
                raise ValueError
            binding = SmartPerfettoOriginalBinding.from_private_document(
                value["binding"]
            )
        except (KeyError, TypeError, ValueError, SmartPerfettoOriginalInvalid):
            raise SmartPerfettoOriginalInvalid from None
        return cls(scenario_type=scenario_type, binding=binding)


@dataclass(frozen=True, slots=True)
class SmartPerfettoOriginalCollectionBinding:
    reports: tuple[SmartPerfettoScenarioOriginalBinding, ...]

    def __post_init__(self) -> None:
        scenarios = tuple(item.scenario_type for item in self.reports)
        if not reports_are_ordered(scenarios):
            raise SmartPerfettoOriginalInvalid
        identities = {
            (item.binding.team_id, item.binding.analysis_id) for item in self.reports
        }
        if len(identities) != 1:
            raise SmartPerfettoOriginalInvalid
        if any(
            type(item.binding.size) is not int
            or not 0 < item.binding.size <= MAX_SMARTPERFETTO_ORIGINAL_BYTES
            for item in self.reports
        ):
            raise SmartPerfettoOriginalInvalid
        if (
            sum(item.binding.size for item in self.reports)
            + 64 * 1024
            > MAX_SMARTPERFETTO_ORIGINAL_COLLECTION_BYTES
        ):
            raise SmartPerfettoOriginalInvalid

    def public_document(self) -> dict[str, object]:
        return {
            "available": True,
            "mode": "scenario_collection",
            "reports": [item.public_document() for item in self.reports],
        }

    def private_document(self) -> dict[str, object]:
        return {
            "available": True,
            "mode": "scenario_collection",
            "reports": [item.private_document() for item in self.reports],
        }

    @classmethod
    def from_private_document(
        cls, value: object
    ) -> "SmartPerfettoOriginalCollectionBinding":
        try:
            if not isinstance(value, Mapping) or set(value) != {
                "available",
                "mode",
                "reports",
            }:
                raise ValueError
            reports = value["reports"]
            if (
                value["available"] is not True
                or value["mode"] != "scenario_collection"
                or not isinstance(reports, list)
            ):
                raise ValueError
            parsed = tuple(
                SmartPerfettoScenarioOriginalBinding.from_private_document(item)
                for item in reports
            )
        except (KeyError, TypeError, ValueError, SmartPerfettoOriginalInvalid):
            raise SmartPerfettoOriginalInvalid from None
        return cls(reports=parsed)

    def for_scenario(
        self, scenario_type: SmartPerfettoScenario
    ) -> SmartPerfettoScenarioOriginalBinding:
        for item in self.reports:
            if item.scenario_type == scenario_type:
                return item
        raise SmartPerfettoOriginalNotFound


SmartPerfettoOriginalReference: TypeAlias = (
    SmartPerfettoOriginalBinding | SmartPerfettoOriginalCollectionBinding
)


def reports_are_ordered(scenarios: tuple[SmartPerfettoScenario, ...]) -> bool:
    return (
        bool(scenarios)
        and len(set(scenarios)) == len(scenarios)
        and scenarios
        == tuple(item for item in ("startup", "scroll") if item in scenarios)
    )


def restore_smartperfetto_original(value: object) -> SmartPerfettoOriginalReference:
    if isinstance(value, Mapping) and value.get("mode") == "scenario_collection":
        return SmartPerfettoOriginalCollectionBinding.from_private_document(value)
    return SmartPerfettoOriginalBinding.from_private_document(value)


def _artifact_path(root: Path, team_id: UUID, analysis_id: UUID, version: int) -> Path:
    return (
        root
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / f"smartperfetto-original-v{version}.json"
    )


def _scenario_artifact_path(
    root: Path,
    team_id: UUID,
    analysis_id: UUID,
    scenario_type: SmartPerfettoScenario,
    version: int,
) -> Path:
    return (
        root
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / f"smartperfetto-original-{scenario_type}-v{version}.json"
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


def persist_smartperfetto_original(
    *,
    root: Path,
    team_id: UUID,
    analysis_id: UUID,
    document: object | None = None,
    payload: bytes | None = None,
) -> SmartPerfettoOriginalBinding:
    if (document is None) == (payload is None):
        raise SmartPerfettoOriginalInvalid
    if payload is None:
        try:
            payload = canonical_json_bytes(document)
        except Exception:
            raise SmartPerfettoOriginalInvalid from None
    elif type(payload) is not bytes:
        raise SmartPerfettoOriginalInvalid
    if not 0 < len(payload) <= MAX_SMARTPERFETTO_ORIGINAL_BYTES:
        raise SmartPerfettoOriginalInvalid
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise SmartPerfettoOriginalInvalid from None
    if not isinstance(parsed, dict):
        raise SmartPerfettoOriginalInvalid
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
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
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


def persist_smartperfetto_scenario_original(
    *,
    root: Path,
    team_id: UUID,
    analysis_id: UUID,
    scenario_type: SmartPerfettoScenario,
    document: object | None = None,
    payload: bytes | None = None,
) -> SmartPerfettoScenarioOriginalBinding:
    if scenario_type not in _SCENARIO_LABELS or (document is None) == (payload is None):
        raise SmartPerfettoOriginalInvalid
    if payload is None:
        try:
            payload = canonical_json_bytes(document)
        except Exception:
            raise SmartPerfettoOriginalInvalid from None
    elif type(payload) is not bytes:
        raise SmartPerfettoOriginalInvalid
    if not 0 < len(payload) <= MAX_SMARTPERFETTO_ORIGINAL_BYTES:
        raise SmartPerfettoOriginalInvalid
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise SmartPerfettoOriginalInvalid from None
    if not isinstance(parsed, dict):
        raise SmartPerfettoOriginalInvalid
    directory = _safe_analysis_directory(root, team_id, analysis_id)
    target = _scenario_artifact_path(
        root.resolve(), team_id, analysis_id, scenario_type, _VERSION
    )
    binding = SmartPerfettoOriginalBinding(
        artifact_id=uuid5(
            _ARTIFACT_NAMESPACE,
            f"{team_id}:{analysis_id}:{scenario_type}:{_VERSION}",
        ),
        team_id=team_id,
        analysis_id=analysis_id,
        version=_VERSION,
        mime=_MIME,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    expected = SmartPerfettoScenarioOriginalBinding(
        scenario_type=scenario_type,
        binding=binding,
    )
    if target.exists() or target.is_symlink():
        existing = read_smartperfetto_scenario_original(
            root=root,
            entry=expected,
            team_id=team_id,
            analysis_id=analysis_id,
        )
        if existing != payload:
            raise SmartPerfettoOriginalInvalid
        return expected
    temporary = directory / f".smartperfetto-original-{scenario_type}-{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
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
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError
        return payload
    except FileNotFoundError:
        raise SmartPerfettoOriginalNotFound from None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise SmartPerfettoOriginalInvalid from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_smartperfetto_scenario_original(
    *,
    root: Path,
    entry: SmartPerfettoScenarioOriginalBinding,
    team_id: UUID,
    analysis_id: UUID,
    maximum_bytes: int = MAX_SMARTPERFETTO_ORIGINAL_BYTES,
) -> bytes:
    binding = entry.binding
    if binding.team_id != team_id or binding.analysis_id != analysis_id:
        raise SmartPerfettoOriginalNotFound
    if (
        entry.scenario_type not in _SCENARIO_LABELS
        or binding.version != _VERSION
        or binding.mime != _MIME
        or type(maximum_bytes) is not int
        or not 0 < maximum_bytes <= MAX_SMARTPERFETTO_ORIGINAL_BYTES
    ):
        raise SmartPerfettoOriginalInvalid
    target = _scenario_artifact_path(
        root.resolve(), team_id, analysis_id, entry.scenario_type, binding.version
    )
    descriptor = -1
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
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
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError
        return payload
    except FileNotFoundError:
        raise SmartPerfettoOriginalNotFound from None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise SmartPerfettoOriginalInvalid from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_smartperfetto_original_collection(
    *,
    root: Path,
    binding: SmartPerfettoOriginalCollectionBinding,
    team_id: UUID,
    analysis_id: UUID,
    maximum_bytes: int = MAX_SMARTPERFETTO_ORIGINAL_COLLECTION_BYTES,
) -> bytes:
    if (
        type(maximum_bytes) is not int
        or not 0 < maximum_bytes <= MAX_SMARTPERFETTO_ORIGINAL_COLLECTION_BYTES
    ):
        raise SmartPerfettoOriginalInvalid
    reports: list[dict[str, object]] = []
    for entry in binding.reports:
        original = read_smartperfetto_scenario_original(
            root=root,
            entry=entry,
            team_id=team_id,
            analysis_id=analysis_id,
        )
        reports.append(
            {
                "scenario_type": entry.scenario_type,
                "label": _SCENARIO_LABELS[entry.scenario_type],
                "document": json.loads(original.decode("utf-8")),
            }
        )
    payload = canonical_json_bytes({"mode": "scenario_collection", "reports": reports})
    if len(payload) > maximum_bytes:
        raise SmartPerfettoOriginalInvalid
    return payload


__all__ = [
    "MAX_SMARTPERFETTO_ORIGINAL_BYTES",
    "MAX_SMARTPERFETTO_ORIGINAL_COLLECTION_BYTES",
    "SmartPerfettoOriginalBinding",
    "SmartPerfettoOriginalCollectionBinding",
    "SmartPerfettoOriginalError",
    "SmartPerfettoOriginalInvalid",
    "SmartPerfettoOriginalNotFound",
    "SmartPerfettoOriginalReference",
    "SmartPerfettoScenarioOriginalBinding",
    "persist_smartperfetto_original",
    "persist_smartperfetto_scenario_original",
    "read_smartperfetto_original",
    "read_smartperfetto_original_collection",
    "read_smartperfetto_scenario_original",
    "restore_smartperfetto_original",
]
