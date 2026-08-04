"""Atomic, bounded persistence for the loopback-only analysis runtime."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from perfpilot_api.reports.contracts import canonical_json_bytes


_DOCUMENT_NAME = re.compile(
    r"(?:state|projection|report|smartperfetto-report|round-[123])\.json\Z"
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


class LocalAnalysisStore:
    def __init__(self, data_root: Path) -> None:
        if not isinstance(data_root, Path):
            raise TypeError("data_root must be a Path")
        resolved_root = data_root.resolve()
        self._root = resolved_root / "analyses"
        self._root.mkdir(parents=True, exist_ok=True)
        self._root = self._root.resolve()

    def _analysis_directory(self, analysis_id: UUID, *, create: bool) -> Path:
        if not isinstance(analysis_id, UUID):
            raise TypeError("analysis_id must be a UUID")
        directory = self._root / str(analysis_id)
        if directory.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        if create:
            directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if not directory.exists() or not directory.is_dir():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        try:
            directory.relative_to(self._root)
        except ValueError:
            raise LocalAnalysisStoreError("unsafe local analysis path") from None
        return directory

    @staticmethod
    def _document_path(directory: Path, name: str) -> Path:
        if not isinstance(name, str) or _DOCUMENT_NAME.fullmatch(name) is None:
            raise LocalAnalysisStoreError("invalid local analysis document name")
        target = directory / name
        if target.is_symlink():
            raise LocalAnalysisStoreError("unsafe local analysis path")
        return target

    def save_document(
        self,
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
        directory = self._analysis_directory(analysis_id, create=True)
        target = self._document_path(directory, name)
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

    def save_state(self, analysis_id: UUID, value: Mapping[str, object]) -> None:
        self.save_document(analysis_id, "state.json", value)

    def load_document(self, analysis_id: UUID, name: str) -> dict[str, object] | None:
        directory = self._analysis_directory(analysis_id, create=False)
        target = self._document_path(directory, name)
        if not target.exists():
            return None
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

    def load_states(self) -> dict[UUID, dict[str, object]]:
        states: dict[UUID, dict[str, object]] = {}
        try:
            candidates = sorted(self._root.iterdir(), key=lambda path: path.name)
        except OSError:
            raise LocalAnalysisStoreError("local analysis persistence failed") from None
        for candidate in candidates:
            try:
                analysis_id = UUID(candidate.name)
            except ValueError:
                continue
            if candidate.is_symlink():
                raise LocalAnalysisStoreError("unsafe local analysis path")
            if not candidate.is_dir():
                continue
            state = self.load_document(analysis_id, "state.json")
            if state is not None:
                states[analysis_id] = state
        return states


__all__ = [
    "LocalAnalysisStore",
    "LocalAnalysisStoreError",
]
