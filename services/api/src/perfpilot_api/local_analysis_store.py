"""Atomic, tenant-scoped persistence for the loopback-only analysis runtime."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from perfpilot_api.reports.contracts import canonical_json_bytes


_DOCUMENT_NAME = re.compile(
    r"(?:state|projection|report|smartperfetto-report|normalized-core|android-memory-result|source-context|agent-capture-manifest|round-[123])\.json\Z"
)
_MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
_RUNTIME_STATUS_KEYS = {
    "current_stage",
    "stage_state",
    "started_at",
    "updated_at",
    "last_progress_at",
    "attempt",
    "max_attempts",
    "generation",
    "waiting_for",
    "progress_summary",
    "available_actions",
}
_RUNTIME_STAGES = frozenset(
    {
        "input_validation",
        "device_claim",
        "device_capture",
        "smartperfetto",
        "source_code",
        "perfpilot_ai",
        "report",
    }
)
_RUNTIME_STAGE_STATES = frozenset(
    {
        "pending",
        "running",
        "waiting",
        "slow",
        "waiting_for_upstream",
        "completed",
        "failed",
        "canceled",
        "cancel_requested",
        "not_requested",
    }
)
_RUNTIME_WAITING_FOR = frozenset(
    {
        "agent",
        "device",
        "smartperfetto",
        "source_agent",
        "ai_provider",
        "storage",
        "report_publish",
    }
)
_RUNTIME_ACTIONS = frozenset({"cancel", "retry"})
_LEGACY_STAGE_ORDER = (
    "input_validation",
    "smartperfetto",
    "perfpilot_ai",
    "report",
)
_TERMINAL_STATES = frozenset(
    {"completed", "partially_completed", "failed", "canceled", "deleted"}
)
_STAGE_SUMMARIES = {
    "input_validation": "正在校验分析输入",
    "device_claim": "正在等待设备",
    "device_capture": "正在采集真机 Trace",
    "smartperfetto": "正在分析 Trace",
    "source_code": "正在读取并匹配源码",
    "perfpilot_ai": "正在生成中文分析结论",
    "report": "正在生成分析报告",
}


class LocalAnalysisStoreError(RuntimeError):
    committed = False


class LocalAnalysisStoreDurabilityError(LocalAnalysisStoreError):
    committed = True


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


def _utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.astimezone(UTC).utcoffset() == parsed.utcoffset()


def validate_analysis_runtime_status(value: object) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping) or set(value) != _RUNTIME_STATUS_KEYS:
            raise ValueError
        current_stage = value["current_stage"]
        stage_state = value["stage_state"]
        attempt = value["attempt"]
        max_attempts = value["max_attempts"]
        generation = value["generation"]
        waiting_for = value["waiting_for"]
        progress_summary = value["progress_summary"]
        actions = value["available_actions"]
        if current_stage not in _RUNTIME_STAGES or stage_state not in _RUNTIME_STAGE_STATES:
            raise ValueError
        if any(
            not _utc_timestamp(value[key])
            for key in ("started_at", "updated_at", "last_progress_at")
        ):
            raise ValueError
        if (
            type(attempt) is not int
            or type(max_attempts) is not int
            or type(generation) is not int
            or not 1 <= attempt <= max_attempts
            or generation < 1
        ):
            raise ValueError
        if waiting_for is not None and waiting_for not in _RUNTIME_WAITING_FOR:
            raise ValueError
        if not isinstance(progress_summary, str) or len(progress_summary) > 240:
            raise ValueError
        if (
            not isinstance(actions, list)
            or len(actions) != len(set(actions))
            or any(action not in _RUNTIME_ACTIONS for action in actions)
        ):
            raise ValueError
        return {
            "current_stage": current_stage,
            "stage_state": stage_state,
            "started_at": value["started_at"],
            "updated_at": value["updated_at"],
            "last_progress_at": value["last_progress_at"],
            "attempt": attempt,
            "max_attempts": max_attempts,
            "generation": generation,
            "waiting_for": waiting_for,
            "progress_summary": progress_summary,
            "available_actions": list(actions),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("analysis runtime status rejected") from None


def migrate_analysis_runtime_status(
    value: object,
    *,
    state: str,
    generation: int,
    updated_at: str,
    stages: Mapping[str, object],
) -> dict[str, object]:
    if value is not None:
        return validate_analysis_runtime_status(value)
    if type(generation) is not int or generation < 1 or not _utc_timestamp(updated_at):
        raise ValueError("analysis runtime status rejected")
    current_stage = "report"
    stage_state = "pending"
    if state in _TERMINAL_STATES:
        stage_state = (
            "completed"
            if state in {"completed", "partially_completed"}
            else state
        )
    else:
        for candidate in _LEGACY_STAGE_ORDER:
            candidate_state = stages.get(candidate)
            if candidate_state == "running":
                current_stage = candidate
                stage_state = "running"
                break
        else:
            for candidate in _LEGACY_STAGE_ORDER:
                candidate_state = stages.get(candidate)
                if candidate_state == "pending":
                    current_stage = candidate
                    stage_state = "pending"
                    break
    return validate_analysis_runtime_status(
        {
            "current_stage": current_stage,
            "stage_state": stage_state,
            "started_at": updated_at,
            "updated_at": updated_at,
            "last_progress_at": updated_at,
            "attempt": 1,
            "max_attempts": 2,
            "generation": generation,
            "waiting_for": None,
            "progress_summary": _STAGE_SUMMARIES[current_stage],
            "available_actions": [] if state in _TERMINAL_STATES else ["cancel"],
        }
    )


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
        committed = False
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
            committed = True
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            if not committed:
                temporary.unlink(missing_ok=True)
                raise LocalAnalysisStoreError(
                    "local analysis persistence failed"
                ) from None
            raise LocalAnalysisStoreDurabilityError(
                "local analysis durability uncertain"
            ) from None

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

    @staticmethod
    def _remove_directory_contents(directory_fd: int) -> None:
        try:
            entries = list(os.scandir(directory_fd))
        except OSError:
            raise LocalAnalysisStoreError("local analysis removal failed") from None
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    os.unlink(entry.name, dir_fd=directory_fd)
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise LocalAnalysisStoreError("unsafe local analysis path")
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    LocalAnalysisStore._remove_directory_contents(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(entry.name, dir_fd=directory_fd)
            except LocalAnalysisStoreError:
                raise
            except OSError:
                raise LocalAnalysisStoreError(
                    "local analysis removal failed"
                ) from None

    def remove_analysis(self, team_id: UUID, analysis_id: UUID) -> None:
        """Remove one exact tenant analysis without following filesystem links."""

        self._verify_trusted_root()
        _require_uuid(team_id, "team_id")
        _require_uuid(analysis_id, "analysis_id")
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        try:
            try:
                team_fd = os.open(str(team_id), flags, dir_fd=self._root_fd)
            except FileNotFoundError:
                return
            descriptors.append(team_fd)
            try:
                analyses_fd = os.open("analyses", flags, dir_fd=team_fd)
            except FileNotFoundError:
                return
            descriptors.append(analyses_fd)
            try:
                analysis_fd = os.open(str(analysis_id), flags, dir_fd=analyses_fd)
            except FileNotFoundError:
                return
            descriptors.append(analysis_fd)
            self._remove_directory_contents(analysis_fd)
            os.close(descriptors.pop())
            os.rmdir(str(analysis_id), dir_fd=analyses_fd)
            os.fsync(analyses_fd)
        except LocalAnalysisStoreError:
            raise
        except OSError:
            raise LocalAnalysisStoreError("local analysis removal failed") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


__all__ = [
    "LocalAnalysisStore",
    "LocalAnalysisStoreDurabilityError",
    "LocalAnalysisStoreError",
    "migrate_analysis_runtime_status",
    "validate_analysis_runtime_status",
]
