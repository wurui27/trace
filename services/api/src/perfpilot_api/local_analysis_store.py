"""Atomic, tenant-scoped persistence for the loopback-only analysis runtime."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from perfpilot_api.reports.contracts import canonical_json_bytes


_DOCUMENT_NAME = re.compile(
    r"(?:state|projection|report|smartperfetto-report|normalized-core|android-memory-result|round-[123])\.json\Z"
)
_MAX_DOCUMENT_BYTES = 12 * 1024 * 1024


class LocalAnalysisStoreError(RuntimeError):
    pass


def _reject_constant(_value: str) -> object:
    raise ValueError


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _require_uuid(value: UUID, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")
    return value


class LocalAnalysisStore:
    def __init__(self, data_root: Path) -> None:
        if not isinstance(data_root, Path):
            raise TypeError("data_root must be a Path")
        anchor = data_root.resolve()
        root = anchor / "teams"
        if root.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink() or root.resolve() != root.absolute():
                raise ValueError
            root.resolve().relative_to(anchor)
            self._root = root.absolute()
            self._root_fd = os.open(
                self._root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_status = os.fstat(self._root_fd)
            self._root_identity = (root_status.st_dev, root_status.st_ino)
            self._verify_trusted_root()
        except LocalAnalysisStoreError:
            self.close()
            raise
        except (OSError, ValueError):
            self.close()
            raise LocalAnalysisStoreError("unsafe local analysis path") from None

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            self._root_fd = -1
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def _verify_trusted_root(self) -> None:
        try:
            current = os.lstat(self._root)
            held = os.fstat(self._root_fd)
            if (
                self._root_fd < 0
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self._root_identity
                or (held.st_dev, held.st_ino) != self._root_identity
            ):
                raise ValueError
        except (OSError, ValueError):
            raise LocalAnalysisStoreError("unsafe local analysis path") from None

    @staticmethod
    def _contained(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError:
            raise LocalAnalysisStoreError("unsafe local analysis path") from None

    def _team_directory(self, team_id: UUID, *, create: bool) -> Path:
        self._verify_trusted_root()
        _require_uuid(team_id, "team_id")
        directory = self._root / str(team_id)
        if directory.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        if create:
            directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if not directory.exists() or not directory.is_dir():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        self._contained(directory, self._root)
        return directory

    def _analyses_directory(self, team_id: UUID, *, create: bool) -> Path:
        team = self._team_directory(team_id, create=create)
        directory = team / "analyses"
        if directory.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        if create:
            directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if not directory.exists() or not directory.is_dir():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        self._contained(directory, team)
        return directory

    def _analysis_directory(
        self,
        team_id: UUID,
        analysis_id: UUID,
        *,
        create: bool,
    ) -> Path:
        _require_uuid(analysis_id, "analysis_id")
        analyses = self._analyses_directory(team_id, create=create)
        directory = analyses / str(analysis_id)
        if directory.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        if create:
            directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if not directory.exists() or not directory.is_dir():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        self._contained(directory, analyses)
        return directory

    @staticmethod
    def _document_path(directory: Path, name: str) -> Path:
        if not isinstance(name, str) or _DOCUMENT_NAME.fullmatch(name) is None:
            raise LocalAnalysisStoreError("invalid local analysis document name")
        target = directory / name
        if target.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        return target

    def analysis_subdirectory(
        self,
        team_id: UUID,
        analysis_id: UUID,
        name: str,
    ) -> Path:
        if name not in {"uploads", "device-captures"}:
            raise LocalAnalysisStoreError("invalid local analysis directory name")
        analysis = self._analysis_directory(team_id, analysis_id, create=True)
        directory = analysis / name
        if directory.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if not directory.is_dir():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        self._contained(directory, analysis)
        return directory

    def upload_path(
        self,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: str,
    ) -> Path:
        if not isinstance(upload_id, str):
            raise TypeError("upload_id must be a string")
        try:
            parsed = UUID(upload_id)
        except ValueError:
            raise LocalAnalysisStoreError("invalid local upload identifier") from None
        if str(parsed) != upload_id:
            raise LocalAnalysisStoreError("invalid local upload identifier")
        directory = self.analysis_subdirectory(team_id, analysis_id, "uploads")
        target = directory / f"{upload_id}.bin"
        if target.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        self._contained(target, directory)
        return target

    def save_document(
        self,
        team_id: UUID,
        analysis_id: UUID,
        name: str,
        value: Mapping[str, object],
    ) -> None:
        if not isinstance(value, Mapping):
            raise LocalAnalysisStoreError("invalid local analysis document")
        try:
            payload = canonical_json_bytes(dict(value))
        except Exception:
            raise LocalAnalysisStoreError("invalid local analysis document") from None
        if not payload or len(payload) > _MAX_DOCUMENT_BYTES:
            raise LocalAnalysisStoreError("invalid local analysis document")
        directory = self._analysis_directory(team_id, analysis_id, create=True)
        target = self._document_path(directory, name)
        if target.exists() and not target.is_file():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        temporary = directory / f".{name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise LocalAnalysisStoreError("local analysis persistence failed") from None

    def save_state(
        self,
        team_id: UUID,
        analysis_id: UUID,
        value: Mapping[str, object],
    ) -> None:
        if value.get("team_id") != str(team_id) or value.get("analysis_id") != str(
            analysis_id
        ):
            raise LocalAnalysisStoreError("invalid local analysis document")
        self.save_document(team_id, analysis_id, "state.json", value)

    def load_document(
        self,
        team_id: UUID,
        analysis_id: UUID,
        name: str,
    ) -> dict[str, object] | None:
        directory = self._analysis_directory(team_id, analysis_id, create=False)
        target = self._document_path(directory, name)
        if not target.exists():
            return None
        if not target.is_file():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        try:
            size = target.stat().st_size
            if not 0 < size <= _MAX_DOCUMENT_BYTES:
                raise ValueError
            payload = target.read_bytes()
            document = json.loads(
                payload.decode("utf-8", errors="strict"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise LocalAnalysisStoreError("invalid local analysis document") from None
        if not isinstance(document, dict):
            raise LocalAnalysisStoreError("invalid local analysis document")
        return document

    def load_states(self) -> dict[tuple[UUID, UUID], dict[str, object]]:
        self._verify_trusted_root()
        states: dict[tuple[UUID, UUID], dict[str, object]] = {}
        try:
            teams = sorted(self._root.iterdir(), key=lambda path: path.name)
        except OSError:
            raise LocalAnalysisStoreError("local analysis persistence failed") from None
        for team_candidate in teams:
            try:
                team_id = UUID(team_candidate.name)
            except ValueError:
                continue
            if str(team_id) != team_candidate.name:
                continue
            if team_candidate.is_symlink():
                raise LocalAnalysisStoreError("unsafe local analysis path")
            if not team_candidate.is_dir():
                continue
            analyses = team_candidate / "analyses"
            if analyses.is_symlink():
                raise LocalAnalysisStoreError("unsafe local analysis path")
            if not analyses.exists():
                continue
            if not analyses.is_dir():
                raise LocalAnalysisStoreError("unsafe local analysis path")
            for candidate in sorted(analyses.iterdir(), key=lambda path: path.name):
                try:
                    analysis_id = UUID(candidate.name)
                except ValueError:
                    continue
                if str(analysis_id) != candidate.name:
                    continue
                if candidate.is_symlink():
                    raise LocalAnalysisStoreError("unsafe local analysis path")
                if not candidate.is_dir():
                    continue
                state = self.load_document(team_id, analysis_id, "state.json")
                if state is None:
                    continue
                if state.get("team_id") != str(team_id) or state.get(
                    "analysis_id"
                ) != str(analysis_id):
                    raise LocalAnalysisStoreError("invalid local analysis document")
                states[(team_id, analysis_id)] = state
        return states


__all__ = [
    "LocalAnalysisStore",
    "LocalAnalysisStoreError",
]
