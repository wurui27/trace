"""Bounded, verified materialization of Android memory engine inputs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import re
import stat
import tarfile
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import BinaryIO, Final

import httpx

from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.engines.contracts import EngineInput
from perfpilot_api.engines.errors import EngineAdapterError


_MAX_FILES: Final = 2048
_RUN_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
# This registry coordinates Stager instances in this process. Descriptor-relative
# no-follow traversal protects untrusted entries and refuses changed bindings. It
# does not serialize arbitrary writers in other OS processes.
_ACTIVE_OWNERS_LOCK: Final = threading.Lock()
_ACTIVE_OWNERS: Final[dict[tuple[int, int, str], object]] = {}

_ROLE_LAYOUT: Final[dict[str, tuple[str, str]]] = {
    "auto": ("memory_evidence", "unclassified/{id}.bin"),
    "handoff_archive": ("memory_evidence", "archives/handoff-{id}.tar"),
    "meminfo": ("memory_evidence", "meminfo/meminfo-{id}.txt"),
    "smaps": ("memory_evidence", "smaps/smaps-{id}.txt"),
    "showmap": ("memory_evidence", "showmap/showmap-{id}.txt"),
    "hprof": ("memory_evidence", "hprof/hprof-{id}.hprof"),
    "gfxinfo": ("memory_evidence", "gfxinfo/gfxinfo-{id}.txt"),
    "proc_meminfo": ("memory_evidence", "proc-meminfo/proc-meminfo-{id}.txt"),
    "pressure_memory": (
        "memory_evidence",
        "pressure-memory/pressure-memory-{id}.txt",
    ),
    "zram": ("memory_evidence", "zram/zram-{id}.txt"),
    "dmabuf": ("memory_evidence", "dmabuf/dmabuf-{id}.txt"),
    "exit_info": ("memory_evidence", "exit-info/exit-info-{id}.txt"),
    "analysis_report": (
        "memory_evidence",
        "reports/analysis-report-{id}.json",
    ),
    "comparison_report": (
        "memory_evidence",
        "reports/comparison-report-{id}.json",
    ),
    "perfetto_trace": ("trace", "traces/perfetto-trace-{id}.pftrace"),
    "native_heap_profile": (
        "memory_evidence",
        "native-heap/native-heap-profile-{id}.heapprofd",
    ),
    "phase_metadata": (
        "capture_manifest",
        "metadata/phase-metadata-{id}.json",
    ),
    "device_context": (
        "memory_evidence",
        "metadata/device-context-{id}.json",
    ),
    "previous_ai_context": (
        "memory_evidence",
        "context/previous-ai-context-{id}.json",
    ),
    "previous_analysis_report": (
        "memory_evidence",
        "reports/previous-analysis-report-{id}.json",
    ),
    "android_log": ("log", "logs/android-log-{id}.txt"),
    "qa_screenshot": ("screenshot", "screenshots/qa-screenshot-{id}.png"),
}


class AndroidMemoryStagingError(EngineAdapterError):
    """Stable, redacted failure raised only by the host stager."""

    __slots__ = ()


def _error(stable_code: str, *, retryable: bool = False) -> AndroidMemoryStagingError:
    return AndroidMemoryStagingError(stable_code=stable_code, retryable=retryable)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


@dataclass(frozen=True, slots=True)
class _OwnedWorkspace:
    run_id: str
    input_dir: Path
    run_stat: os.stat_result
    input_stat: os.stat_result
    root_fd: int
    run_fd: int
    input_fd: int
    owner_key: tuple[int, int, str]
    owner_token: object


@dataclass(frozen=True, slots=True)
class _CapturedFailure:
    error: BaseException
    traceback: TracebackType | None


def _captured_failure(error: BaseException) -> _CapturedFailure:
    if isinstance(
        error,
        (AndroidMemoryStagingError, asyncio.CancelledError, KeyboardInterrupt, SystemExit),
    ):
        selected = error
    else:
        selected = _error("download_failed", retryable=True)
    return _CapturedFailure(error=selected, traceback=error.__traceback__)


def _raise_captured(
    primary: _CapturedFailure | None,
    secondary: _CapturedFailure | None = None,
) -> None:
    selected = primary
    if selected is None:
        selected = secondary
    elif (
        secondary is not None
        and isinstance(
            secondary.error,
            (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
        )
        and not isinstance(
            selected.error,
            (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
        )
    ):
        selected = secondary
    if selected is None:
        return

    selected.error.__cause__ = None
    selected.error.__context__ = None
    selected.error.__suppress_context__ = False
    raise selected.error.with_traceback(selected.traceback)


def _is_control_failure(failure: _CapturedFailure | None) -> bool:
    return failure is not None and isinstance(
        failure.error,
        (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
    )


def _reserve_owner(owner_key: tuple[int, int, str]) -> object | None:
    with _ACTIVE_OWNERS_LOCK:
        if owner_key in _ACTIVE_OWNERS:
            return None
        token = object()
        _ACTIVE_OWNERS[owner_key] = token
        return token


def _release_owner(owner_key: tuple[int, int, str], owner_token: object) -> None:
    with _ACTIVE_OWNERS_LOCK:
        if _ACTIVE_OWNERS.get(owner_key) is owner_token:
            del _ACTIVE_OWNERS[owner_key]


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except Exception:
        pass


def _close_destination(destination: BinaryIO) -> _CapturedFailure | None:
    try:
        destination.close()
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
        return _captured_failure(error)
    except Exception as error:
        return _captured_failure(error)
    return None


def _clear_directory_fd(directory_fd: int) -> bool:
    try:
        names = tuple(entry.name for entry in os.scandir(directory_fd))
    except Exception:
        return False

    for name in names:
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except Exception:
            return False

        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd: int | None = None
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                child_stat = os.fstat(child_fd)
            except Exception:
                _close_fd(child_fd)
                return False
            if not _same_file(entry_stat, child_stat) or not _clear_directory_fd(child_fd):
                _close_fd(child_fd)
                return False
            _close_fd(child_fd)

            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except Exception:
                return False
            if not _same_file(current, entry_stat):
                continue
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except Exception:
                return False
            continue

        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except Exception:
            return False
    return True


def _remove_owned_directory(owned: _OwnedWorkspace) -> bool:
    try:
        current = os.stat(
            owned.run_id,
            dir_fd=owned.root_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    except Exception:
        return False

    if not _same_file(current, owned.run_stat) or not stat.S_ISDIR(current.st_mode):
        return True
    if not _clear_directory_fd(owned.run_fd):
        return False

    try:
        current = os.stat(
            owned.run_id,
            dir_fd=owned.root_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    except Exception:
        return False
    if not _same_file(current, owned.run_stat):
        return True

    try:
        os.rmdir(owned.run_id, dir_fd=owned.root_fd)
    except FileNotFoundError:
        return True
    except Exception:
        try:
            current = os.stat(
                owned.run_id,
                dir_fd=owned.root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return True
        except Exception:
            return False
        return not _same_file(current, owned.run_stat)
    return True


@dataclass(slots=True)
class _CleanupState:
    owned: _OwnedWorkspace
    _finalized: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _finalize(self) -> None:
        if self._finalized:
            return
        _close_fd(self.owned.input_fd)
        _close_fd(self.owned.run_fd)
        _close_fd(self.owned.root_fd)
        _release_owner(self.owned.owner_key, self.owned.owner_token)
        self._finalized = True

    async def cleanup(self) -> None:
        async with self._lock:
            if self._finalized:
                return
            try:
                removed = await asyncio.to_thread(_remove_owned_directory, self.owned)
            except Exception:
                removed = False
            if removed:
                self._finalize()

    async def abandon(self) -> None:
        """Best-effort removal followed by unconditional ownership release."""

        async with self._lock:
            if self._finalized:
                return
            try:
                await asyncio.to_thread(_remove_owned_directory, self.owned)
            except Exception:
                pass
            finally:
                self._finalize()


async def _abandon_after_stage_failure(cleanup_state: _CleanupState) -> None:
    abandon_task = asyncio.create_task(cleanup_state.abandon())
    while not abandon_task.done():
        try:
            await asyncio.shield(abandon_task)
        except BaseException:
            continue
    try:
        abandon_task.result()
    except BaseException:
        cleanup_state._finalize()


@dataclass(frozen=True, slots=True)
class StagedMemoryInput:
    """A verified input directory whose lifetime is explicitly owned by the caller."""

    manifest: MemoryCaptureManifest
    input_dir: Path
    _cleanup_state: _CleanupState = field(repr=False, compare=False)

    async def cleanup(self) -> None:
        """Remove this stage's directory without affecting a later owner."""

        await self._cleanup_state.cleanup()

    async def abandon(self) -> None:
        """Best-effort remove this stage and unconditionally release ownership."""

        await self._cleanup_state.abandon()


class AndroidMemoryStager:
    """Download and verify one memory capture into a private execution directory.

    Descriptor-relative no-follow operations protect untrusted workspace entries,
    while the inode registry provides cooperative same-process ownership only.
    Arbitrary external OS writers remain outside this ownership boundary.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        workspace_root: Path,
        max_files: int,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> None:
        if (
            type(max_files) is not int
            or max_files < 1
            or max_files > _MAX_FILES
            or type(max_file_bytes) is not int
            or max_file_bytes < 1
            or type(max_total_bytes) is not int
            or max_total_bytes < 1
        ):
            raise ValueError("Android memory staging limits are invalid")

        self._client = client
        self._workspace_root = Path(workspace_root).absolute()
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes

    async def stage(
        self,
        *,
        run_id: str,
        inputs: Iterable[EngineInput],
    ) -> StagedMemoryInput:
        """Materialize one manifest and all of its referenced evidence."""

        if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
            raise _error("manifest_invalid")

        bounded_inputs = self._collect_bounded_inputs(inputs)
        manifest_source, evidence_by_id = self._validate_input_metadata(bounded_inputs)
        self._validate_declared_limits(bounded_inputs)

        owned = self._create_workspace(run_id)
        cleanup_state = _CleanupState(owned)
        try:
            manifest, ordered_evidence, manifest_size = await self._download_manifest(
                manifest_source,
                evidence_by_id,
            )
            await self._materialize_evidence(
                owned,
                ordered_evidence,
                initial_total=manifest_size,
            )
        except BaseException as error:
            primary = _captured_failure(error)
            await _abandon_after_stage_failure(cleanup_state)
            _raise_captured(primary)

        return StagedMemoryInput(
            manifest=manifest,
            input_dir=owned.input_dir,
            _cleanup_state=cleanup_state,
        )

    def _collect_bounded_inputs(self, inputs: Iterable[EngineInput]) -> tuple[EngineInput, ...]:
        collected: list[EngineInput] = []
        iterator = iter(inputs)
        for _ in range(self._max_files + 2):
            try:
                collected.append(next(iterator))
            except StopIteration:
                break

        if len(collected) > self._max_files + 1:
            raise _error("input_limit_exceeded")
        return tuple(collected)

    @staticmethod
    def _validate_input_metadata(
        inputs: tuple[EngineInput, ...],
    ) -> tuple[EngineInput, dict[object, EngineInput]]:
        manifests = tuple(source for source in inputs if source.kind == "memory_capture_manifest")
        artifact_ids = tuple(source.artifact_id for source in inputs)
        if len(manifests) != 1 or len(set(artifact_ids)) != len(artifact_ids):
            raise _error("manifest_invalid")

        manifest_source = manifests[0]
        evidence = {
            source.artifact_id: source
            for source in inputs
            if source.kind != "memory_capture_manifest"
        }
        return manifest_source, evidence

    def _validate_declared_limits(self, inputs: tuple[EngineInput, ...]) -> None:
        total = 0
        for source in inputs:
            if type(source.size_bytes) is not int or source.size_bytes < 0:
                raise _error("integrity_mismatch")
            if source.size_bytes > self._max_file_bytes:
                raise _error("input_limit_exceeded")
            total += source.size_bytes
            if total > self._max_total_bytes:
                raise _error("input_limit_exceeded")

    def _create_workspace(self, run_id: str) -> _OwnedWorkspace:
        run_dir = self._workspace_root / run_id
        input_dir = run_dir / "input"
        root_fd: int | None = None
        run_fd: int | None = None
        input_fd: int | None = None
        run_created = False
        created_run_stat: os.stat_result | None = None
        run_stat: os.stat_result | None = None
        input_stat: os.stat_result | None = None
        owner_key: tuple[int, int, str] | None = None
        owner_token: object | None = None
        failure: _CapturedFailure | None = None
        try:
            root_stat = os.lstat(self._workspace_root)
            if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                raise OSError
            root_fd = os.open(self._workspace_root, _DIRECTORY_FLAGS)
            opened_root_stat = os.fstat(root_fd)
            if not _same_file(root_stat, opened_root_stat):
                raise OSError
            owner_key = (opened_root_stat.st_dev, opened_root_stat.st_ino, run_id)
            owner_token = _reserve_owner(owner_key)
            if owner_token is None:
                raise _error("download_failed", retryable=True)

            os.mkdir(run_id, mode=0o700, dir_fd=root_fd)
            run_created = True
            created_run_stat = os.stat(
                run_id,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(created_run_stat.st_mode):
                raise OSError
            run_fd = os.open(run_id, _DIRECTORY_FLAGS, dir_fd=root_fd)
            run_stat = os.fstat(run_fd)
            if not _same_file(created_run_stat, run_stat):
                raise OSError
            os.mkdir("input", mode=0o700, dir_fd=run_fd)
            input_fd = os.open("input", _DIRECTORY_FLAGS, dir_fd=run_fd)
            input_stat = os.fstat(input_fd)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
            failure = _captured_failure(error)
        except Exception as error:
            failure = _captured_failure(error)

        if failure is not None:
            if root_fd is not None and run_created and created_run_stat is not None:
                if (
                    run_fd is not None
                    and run_stat is not None
                    and _same_file(created_run_stat, run_stat)
                ):
                    _clear_directory_fd(run_fd)
                try:
                    current = os.stat(
                        run_id,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except Exception:
                    current = None
                if (
                    current is not None
                    and stat.S_ISDIR(current.st_mode)
                    and _same_file(current, created_run_stat)
                ):
                    try:
                        os.rmdir(run_id, dir_fd=root_fd)
                    except Exception:
                        pass
            _close_fd(input_fd)
            _close_fd(run_fd)
            _close_fd(root_fd)
            if owner_key is not None and owner_token is not None:
                _release_owner(owner_key, owner_token)
            _raise_captured(failure)

        assert root_fd is not None
        assert run_fd is not None
        assert input_fd is not None
        assert run_stat is not None
        assert input_stat is not None
        assert owner_key is not None
        assert owner_token is not None

        return _OwnedWorkspace(
            run_id=run_id,
            input_dir=input_dir,
            run_stat=run_stat,
            input_stat=input_stat,
            root_fd=root_fd,
            run_fd=run_fd,
            input_fd=input_fd,
            owner_key=owner_key,
            owner_token=owner_token,
        )

    async def _download_manifest(
        self,
        source: EngineInput,
        evidence_by_id: dict[object, EngineInput],
    ) -> tuple[
        MemoryCaptureManifest,
        tuple[tuple[str, str, EngineInput, str], ...],
        int,
    ]:
        destination: BinaryIO | None = None
        creation_failure: _CapturedFailure | None = None
        try:
            destination = tempfile.SpooledTemporaryFile(
                max_size=min(self._max_file_bytes, 1024 * 1024),
                mode="w+b",
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
            creation_failure = _captured_failure(error)
        except Exception as error:
            creation_failure = _captured_failure(error)
        _raise_captured(creation_failure)
        assert destination is not None

        size = 0
        payload = b""
        manifest: MemoryCaptureManifest | None = None
        ordered_evidence: tuple[tuple[str, str, EngineInput, str], ...] | None = None
        download_failure: _CapturedFailure | None = None
        try:
            size = await self._download(
                source,
                destination,
                remaining_total=self._max_total_bytes,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
            download_failure = _captured_failure(error)
        except Exception as error:
            download_failure = _captured_failure(error)

        flush_failure: _CapturedFailure | None = None
        validation_failure: _CapturedFailure | None = None
        if download_failure is None:
            try:
                destination.flush()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                flush_failure = _captured_failure(error)
            except Exception as error:
                flush_failure = _captured_failure(error)

            try:
                destination.seek(0)
                payload = destination.read()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                validation_failure = _captured_failure(error)
            except Exception as error:
                validation_failure = _captured_failure(error)

        if download_failure is None and validation_failure is None:
            try:
                manifest = MemoryCaptureManifest.model_validate_json(payload)
            except Exception:
                validation_failure = _CapturedFailure(_error("manifest_invalid"), None)

        if download_failure is None and validation_failure is None and manifest is not None:
            try:
                ordered_evidence = self._match_manifest(manifest, evidence_by_id)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                validation_failure = _captured_failure(error)
            except Exception as error:
                validation_failure = _captured_failure(error)

        close_failure = _close_destination(destination)
        lifecycle_failure = flush_failure
        if lifecycle_failure is None or (
            _is_control_failure(close_failure) and not _is_control_failure(lifecycle_failure)
        ):
            lifecycle_failure = close_failure

        if download_failure is not None:
            _raise_captured(download_failure, lifecycle_failure)
        if validation_failure is not None:
            _raise_captured(validation_failure, lifecycle_failure)
        _raise_captured(lifecycle_failure)

        assert manifest is not None
        assert ordered_evidence is not None
        return manifest, ordered_evidence, size

    @staticmethod
    def _match_manifest(
        manifest: MemoryCaptureManifest,
        evidence_by_id: dict[object, EngineInput],
    ) -> tuple[tuple[str, str, EngineInput, str], ...]:
        referenced_ids = {reference.artifact_id for reference in manifest.artifacts}
        if referenced_ids != set(evidence_by_id):
            raise _error("manifest_invalid")

        matched: list[tuple[str, str, EngineInput, str]] = []
        for reference in manifest.artifacts:
            source = evidence_by_id[reference.artifact_id]
            expected_kind, path_template = _ROLE_LAYOUT[reference.role]
            if source.kind != expected_kind or (
                reference.role == "handoff_archive"
                and source.mime != "application/x-tar"
            ):
                raise _error("manifest_invalid")
            matched.append(
                (
                    reference.role,
                    path_template,
                    source,
                    path_template.format(id=reference.artifact_id),
                )
            )
        return tuple(matched)

    async def _materialize_evidence(
        self,
        owned: _OwnedWorkspace,
        ordered_evidence: tuple[tuple[str, str, EngineInput, str], ...],
        *,
        initial_total: int,
    ) -> None:
        total = initial_total
        owned_directories: set[str] = set()
        for role, _, source, relative_path_text in ordered_evidence:
            relative_path = Path(relative_path_text)
            directory_name = relative_path.parts[0]
            filename = relative_path.parts[1]
            input_fd = self._open_owned_input(owned)
            role_fd: int | None = None
            file_fd: int | None = None
            open_failure: _CapturedFailure | None = None
            try:
                if directory_name not in owned_directories:
                    os.mkdir(directory_name, mode=0o700, dir_fd=input_fd)
                    owned_directories.add(directory_name)
                role_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=input_fd)
                file_fd = os.open(filename, _FILE_FLAGS, 0o600, dir_fd=role_fd)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                open_failure = _captured_failure(error)
            except Exception as error:
                open_failure = _captured_failure(error)

            _close_fd(input_fd)
            _close_fd(role_fd)
            if open_failure is not None:
                _close_fd(file_fd)
                _raise_captured(open_failure)
            assert file_fd is not None

            destination: BinaryIO | None = None
            destination_failure: _CapturedFailure | None = None
            try:
                destination = os.fdopen(file_fd, "wb")
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                destination_failure = _captured_failure(error)
            except Exception as error:
                destination_failure = _captured_failure(error)
            if destination_failure is not None:
                _close_fd(file_fd)
                _raise_captured(destination_failure)
            assert destination is not None

            size = 0
            primary: _CapturedFailure | None = None
            try:
                size = await self._download(
                    source,
                    destination,
                    remaining_total=self._max_total_bytes - total,
                )
                destination.flush()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                primary = _captured_failure(error)
            except Exception as error:
                primary = _captured_failure(error)
            close_failure = _close_destination(destination)
            _raise_captured(primary, close_failure)
            total += size
            if role == "handoff_archive":
                expanded = self._extract_handoff_archive(
                    owned,
                    source=source,
                    archive_relative_path=relative_path,
                    remaining_total=self._max_total_bytes - total,
                    existing_file_count=len(ordered_evidence),
                )
                total += expanded

    def _extract_handoff_archive(
        self,
        owned: _OwnedWorkspace,
        *,
        source: EngineInput,
        archive_relative_path: Path,
        remaining_total: int,
        existing_file_count: int,
    ) -> int:
        """Expand one Agent tar without trusting member paths or tar link semantics."""

        input_fd: int | None = None
        archive_dir_fd: int | None = None
        archive_fd: int | None = None
        archive_file: BinaryIO | None = None
        root_fd: int | None = None
        try:
            input_fd = self._open_owned_input(owned)
            archive_dir_fd = os.open(
                archive_relative_path.parts[0],
                _DIRECTORY_FLAGS,
                dir_fd=input_fd,
            )
            archive_fd = os.open(
                archive_relative_path.parts[1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=archive_dir_fd,
            )
            archive_stat = os.fstat(archive_fd)
            if not stat.S_ISREG(archive_stat.st_mode):
                raise _error("manifest_invalid")
            archive_file = os.fdopen(archive_fd, "rb")
            archive_fd = None

            with tarfile.open(fileobj=archive_file, mode="r:") as archive:
                members: list[tarfile.TarInfo] = []
                while True:
                    member = archive.next()
                    if member is None:
                        break
                    members.append(member)
                    if existing_file_count + len(members) > self._max_files:
                        raise _error("input_limit_exceeded")
                validated = self._validated_handoff_members(
                    members,
                    remaining_total=remaining_total,
                    existing_file_count=existing_file_count,
                )
                root_fd = self._create_handoff_root(
                    input_fd,
                    artifact_id=str(source.artifact_id),
                )
                extracted = 0
                for member, parts in validated:
                    parent_fd = self._ensure_relative_directories(root_fd, parts[:-1])
                    try:
                        file_fd = os.open(
                            parts[-1],
                            _FILE_FLAGS,
                            0o600,
                            dir_fd=parent_fd,
                        )
                        with os.fdopen(file_fd, "wb") as destination:
                            extracted_member = archive.extractfile(member)
                            if extracted_member is None:
                                raise _error("manifest_invalid")
                            written = 0
                            with extracted_member:
                                while True:
                                    chunk = extracted_member.read(64 * 1024)
                                    if not chunk:
                                        break
                                    written += len(chunk)
                                    if written > member.size:
                                        raise _error("manifest_invalid")
                                    destination.write(chunk)
                            if written != member.size:
                                raise _error("manifest_invalid")
                            destination.flush()
                            extracted += written
                    finally:
                        _close_fd(parent_fd)
                return extracted
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except AndroidMemoryStagingError:
            raise
        except (tarfile.TarError, OSError, ValueError, EOFError):
            raise _error("manifest_invalid") from None
        finally:
            _close_fd(root_fd)
            if archive_file is not None:
                try:
                    archive_file.close()
                except Exception:
                    pass
            _close_fd(archive_fd)
            _close_fd(archive_dir_fd)
            _close_fd(input_fd)

    def _validated_handoff_members(
        self,
        members: list[tarfile.TarInfo],
        *,
        remaining_total: int,
        existing_file_count: int,
    ) -> tuple[tuple[tarfile.TarInfo, tuple[str, ...]], ...]:
        files: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        observed: set[tuple[str, ...]] = set()
        regular_paths: set[tuple[str, ...]] = set()
        expanded_total = 0
        for member in members:
            path = PurePosixPath(member.name)
            parts = path.parts
            if (
                path.is_absolute()
                or not parts
                or len(parts) > 16
                or len(member.name.encode("utf-8")) > 1024
                or any(
                    part in {"", ".", ".."}
                    or len(part.encode("utf-8")) > 255
                    or "\x00" in part
                    for part in parts
                )
                or parts in observed
                or any(parts[:index] in regular_paths for index in range(1, len(parts)))
            ):
                raise _error("manifest_invalid")
            observed.add(parts)
            if member.isdir():
                if parts in regular_paths:
                    raise _error("manifest_invalid")
                continue
            if not member.isfile() or member.size < 0:
                raise _error("manifest_invalid")
            if member.size > self._max_file_bytes:
                raise _error("input_limit_exceeded")
            expanded_total += member.size
            if expanded_total > remaining_total:
                raise _error("input_limit_exceeded")
            regular_paths.add(parts)
            files.append((member, parts))
        if not files:
            raise _error("manifest_invalid")
        if existing_file_count + len(files) > self._max_files:
            raise _error("input_limit_exceeded")
        return tuple(files)

    @staticmethod
    def _create_handoff_root(input_fd: int, *, artifact_id: str) -> int:
        handoff_fd: int | None = None
        try:
            try:
                os.mkdir("handoff", mode=0o700, dir_fd=input_fd)
            except FileExistsError:
                pass
            handoff_fd = os.open("handoff", _DIRECTORY_FLAGS, dir_fd=input_fd)
            os.mkdir(artifact_id, mode=0o700, dir_fd=handoff_fd)
            return os.open(artifact_id, _DIRECTORY_FLAGS, dir_fd=handoff_fd)
        finally:
            _close_fd(handoff_fd)

    @staticmethod
    def _ensure_relative_directories(root_fd: int, parts: tuple[str, ...]) -> int:
        current_fd = os.dup(root_fd)
        try:
            for part in parts:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = child_fd
            return current_fd
        except Exception:
            _close_fd(current_fd)
            raise

    @staticmethod
    def _open_owned_input(owned: _OwnedWorkspace) -> int:
        input_fd: int | None = None
        failure: _CapturedFailure | None = None
        try:
            current_run = os.stat(
                owned.run_id,
                dir_fd=owned.root_fd,
                follow_symlinks=False,
            )
            if not _same_file(owned.run_stat, current_run):
                raise OSError
            current_input = os.stat(
                "input",
                dir_fd=owned.run_fd,
                follow_symlinks=False,
            )
            if not _same_file(owned.input_stat, current_input):
                raise OSError
            input_fd = os.dup(owned.input_fd)
            if not _same_file(owned.input_stat, os.fstat(input_fd)):
                raise OSError
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
            failure = _captured_failure(error)
        except Exception as error:
            failure = _captured_failure(error)

        if failure is not None:
            _close_fd(input_fd)
            _raise_captured(failure)
        assert input_fd is not None
        return input_fd

    async def _download(
        self,
        source: EngineInput,
        destination: BinaryIO,
        *,
        remaining_total: int,
    ) -> int:
        expected_digest: bytes | None = None
        response: httpx.Response | None = None
        digest = hashlib.sha256()
        size = 0
        body_validation_started = False
        primary: _CapturedFailure | None = None
        try:
            expected_digest = self._decode_digest(source.sha256_b64)
            request = self._client.build_request(
                "GET",
                source.download_url.get_secret_value(),
            )
            response = await self._client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if response.status_code < 200 or response.status_code > 299:
                raise _error("download_failed", retryable=True)

            self._validate_content_length(
                response,
                source=source,
                remaining_total=remaining_total,
            )
            body_validation_started = True
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if (
                    size > source.size_bytes
                    or size > self._max_file_bytes
                    or size > remaining_total
                ):
                    raise _error("input_limit_exceeded")
                digest.update(chunk)
                destination.write(chunk)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
            primary = _captured_failure(error)
        except Exception as error:
            primary = _captured_failure(error)

        body_reached_natural_end = (
            body_validation_started and response is not None and response.is_closed
        )
        if (
            body_reached_natural_end
            and expected_digest is not None
            and (
                size != source.size_bytes
                or not hmac.compare_digest(digest.digest(), expected_digest)
            )
            and (
                primary is None
                or (
                    isinstance(primary.error, AndroidMemoryStagingError)
                    and primary.error.stable_code == "download_failed"
                )
            )
        ):
            primary = _CapturedFailure(_error("integrity_mismatch"), None)

        close_failure: _CapturedFailure | None = None
        if response is not None:
            try:
                await response.aclose()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                close_failure = _captured_failure(error)
            except Exception as error:
                close_failure = _captured_failure(error)

        _raise_captured(primary, close_failure)
        assert expected_digest is not None

        if size != source.size_bytes or not hmac.compare_digest(
            digest.digest(),
            expected_digest,
        ):
            raise _error("integrity_mismatch")
        return size

    @staticmethod
    def _decode_digest(encoded: str) -> bytes:
        digest: bytes | None = None
        invalid = False
        try:
            digest = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError):
            invalid = True
        if invalid or digest is None:
            raise _error("integrity_mismatch")
        if len(digest) != hashlib.sha256().digest_size:
            raise _error("integrity_mismatch")
        if not hmac.compare_digest(base64.b64encode(digest).decode("ascii"), encoded):
            raise _error("integrity_mismatch")
        return digest

    def _validate_content_length(
        self,
        response: httpx.Response,
        *,
        source: EngineInput,
        remaining_total: int,
    ) -> None:
        value = response.headers.get("content-length")
        if value is None:
            return
        invalid = False
        try:
            declared = int(value)
        except ValueError:
            invalid = True
            declared = -1
        if invalid or declared < 0:
            raise _error("download_failed", retryable=True)
        if (
            declared > source.size_bytes
            or declared > self._max_file_bytes
            or declared > remaining_total
        ):
            raise _error("input_limit_exceeded")


__all__ = ["AndroidMemoryStager", "AndroidMemoryStagingError", "StagedMemoryInput"]
