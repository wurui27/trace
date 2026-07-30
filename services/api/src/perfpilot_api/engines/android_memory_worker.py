"""Isolated local-development and OCI Android memory worker boundaries."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from perfpilot_api.config import is_valid_android_memory_image_reference
from perfpilot_api.engines.android_memory_stager import StagedMemoryInput
from perfpilot_api.engines.errors import EngineAdapterError


WorkerState = Literal["running", "completed", "failed", "canceled", "lost"]
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_STATE_LIMIT = 64 * 1024
_STREAM_RETAIN_LIMIT = 64 * 1024
_DRAIN_CHUNK = 64 * 1024
_CONTROL_TIMEOUT_SECONDS = 10.0
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


@dataclass(frozen=True, slots=True)
class MemoryWorkerResult:
    exit_code: int
    payload: bytes | None = field(repr=False)


class AndroidMemoryWorker(Protocol):
    isolation: Literal["local", "oci"]

    async def start(
        self,
        *,
        run_id: str,
        staged: StagedMemoryInput,
        question: str | None,
        timeout_seconds: int,
    ) -> None: ...

    async def status(self, run_id: str) -> WorkerState: ...

    async def result(self, run_id: str) -> MemoryWorkerResult: ...

    async def cancel(self, run_id: str) -> None: ...

    async def shutdown(self) -> None: ...


class AndroidMemoryWorkerError(EngineAdapterError):
    __slots__ = ()


def _error(code: str, *, retryable: bool = False) -> AndroidMemoryWorkerError:
    return AndroidMemoryWorkerError(stable_code=code, retryable=retryable)


def _safe_path(path: Path, *, allow_commas: bool = False) -> bool:
    rendered = str(path)
    return (
        path.is_absolute()
        and path != Path("/")
        and "\x00" not in rendered
        and "\n" not in rendered
        and "\r" not in rendered
        and (allow_commas or "," not in rendered)
    )


def _safe_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and _RUN_ID.fullmatch(run_id) is not None and ".." not in run_id


def _safe_question(question: object) -> bool:
    return question is None or (
        isinstance(question, str) and "\x00" not in question and len(question) <= 16_384
    )


async def _default_commit_resolver(repository_root: Path) -> str:
    failed = False
    stdout = b""
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/git",
            "-C",
            str(repository_root),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        failed = process.returncode != 0 or len(stdout) > 128
    except BaseException as caught:
        if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        failed = True
    if failed:
        raise _error("worker_unavailable", retryable=True)
    return stdout.decode("ascii", errors="ignore").strip()


async def _drain_bounded(
    reader: Any, *, retain_limit: int = _STREAM_RETAIN_LIMIT
) -> tuple[bytes, bool]:
    retained = bytearray()
    overflow = False
    if reader is None:
        return b"", False
    while True:
        chunk = await reader.read(_DRAIN_CHUNK)
        if not chunk:
            break
        remaining = retain_limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            overflow = True
    return bytes(retained), overflow


async def _safe_cleanup(staged: Any) -> None:
    try:
        await staged.cleanup()
    except BaseException as caught:
        if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise


async def _cleanup_cancellation_safe(active: _ActiveRun) -> None:
    if active.cleanup_task is None:
        active.cleanup_task = asyncio.create_task(_safe_cleanup(active.staged))
    canceled = False
    while not active.cleanup_task.done():
        try:
            await asyncio.shield(active.cleanup_task)
        except asyncio.CancelledError:
            canceled = True
    await active.cleanup_task
    if canceled:
        raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class _PersistedState:
    state: WorkerState
    exit_code: int | None = None
    output_size: int | None = None
    output_sha256: str | None = None
    ownership_token: str | None = None
    image_reference: str | None = None


@dataclass(frozen=True, slots=True)
class _TerminationResult:
    confirmed: bool
    exit_code: int | None = None
    had_error: bool = False


@dataclass(frozen=True, slots=True)
class _RuntimeResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    overflow: bool
    failed: bool


@dataclass(frozen=True, slots=True)
class _InspectResult:
    kind: Literal["live", "stopped", "absent", "unavailable", "foreign"]


@dataclass(slots=True)
class _ActiveRun:
    staged: Any
    process: Any
    task: asyncio.Task[None] | None = None
    state: WorkerState = "running"
    exit_code: int | None = None
    cancel_requested: bool = False
    termination_task: asyncio.Task[_TerminationResult] | None = None
    terminal_error: AndroidMemoryWorkerError | None = None
    ownership_token: str | None = None
    image_reference: str | None = None
    pending_terminal: _PersistedState | None = None
    process_confirmed: bool = False
    cleanup_task: asyncio.Task[None] | None = None


class _WorkerBase:
    isolation: Literal["local", "oci"]

    def __init__(
        self,
        *,
        run_root: Path,
        process_factory: Callable[..., Awaitable[Any]],
        max_output_bytes: int,
    ) -> None:
        run_root = Path(run_root)
        if not _safe_path(run_root) or type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("Android memory worker configuration is invalid")
        self._run_root = run_root
        self._process_factory = process_factory
        self._max_output_bytes = max_output_bytes
        self._active: dict[str, _ActiveRun] = {}
        self._recovered: set[str] = set()
        self._lock = asyncio.Lock()
        self._task_factory: Callable[[Awaitable[None]], asyncio.Task[None]] = asyncio.create_task
        self._root_identity: tuple[int, int] | None = None
        self._run_identities: dict[str, tuple[int, int]] = {}
        self._root_invalid = False
        self._bind_existing_root()

    def _bind_existing_root(self) -> None:
        descriptor: int | None = None
        missing = False
        failed = False
        try:
            descriptor = os.open(self._run_root, _DIRECTORY_FLAGS)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                failed = True
            else:
                self._root_identity = (metadata.st_dev, metadata.st_ino)
        except FileNotFoundError:
            missing = True
        except OSError:
            failed = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if failed and not missing:
            self._root_invalid = True

    def _open_root(self, *, create: bool, missing_ok: bool = False) -> int | None:
        descriptor: int | None = None
        missing = False
        failed = self._root_invalid
        if create and not failed:
            try:
                self._run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                failed = True
        if not failed:
            try:
                descriptor = os.open(self._run_root, _DIRECTORY_FLAGS)
                metadata = os.fstat(descriptor)
                identity = (metadata.st_dev, metadata.st_ino)
                if not stat.S_ISDIR(metadata.st_mode):
                    failed = True
                elif self._root_identity is None:
                    self._root_identity = identity
                elif identity != self._root_identity:
                    failed = True
            except FileNotFoundError:
                missing = True
            except OSError:
                failed = True
        if failed or (missing and not missing_ok):
            if descriptor is not None:
                os.close(descriptor)
            raise _error("worker_unavailable", retryable=True)
        if missing:
            return None
        return descriptor

    def _open_run(
        self,
        run_id: str,
        *,
        missing_ok: bool = False,
    ) -> tuple[int, int] | None:
        root_fd = self._open_root(create=False, missing_ok=missing_ok)
        if root_fd is None:
            return None
        run_fd: int | None = None
        missing = False
        failed = False
        try:
            run_fd = os.open(run_id, _DIRECTORY_FLAGS, dir_fd=root_fd)
            metadata = os.fstat(run_fd)
            identity = (metadata.st_dev, metadata.st_ino)
            bound_identity = self._run_identities.get(run_id)
            if bound_identity is None:
                self._run_identities[run_id] = identity
            elif identity != bound_identity:
                failed = True
        except FileNotFoundError:
            missing = True
        except OSError:
            failed = True
        if failed or (missing and not missing_ok):
            if run_fd is not None:
                os.close(run_fd)
            os.close(root_fd)
            raise _error("worker_unavailable", retryable=True)
        if missing:
            os.close(root_fd)
            return None
        assert run_fd is not None
        return root_fd, run_fd

    def _open_lock_file(self, run_id: str) -> int:
        root_fd = self._open_root(create=True)
        assert root_fd is not None
        lock_dir_fd: int | None = None
        lock_fd: int | None = None
        try:
            try:
                os.mkdir(".locks", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            lock_dir_fd = os.open(".locks", _DIRECTORY_FLAGS, dir_fd=root_fd)
            lock_dir_stat = os.fstat(lock_dir_fd)
            if not stat.S_ISDIR(lock_dir_stat.st_mode):
                raise OSError
            os.fchmod(lock_dir_fd, 0o700)
            filename = hashlib.sha256(run_id.encode("ascii")).hexdigest() + ".lock"
            lock_fd = os.open(
                filename,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=lock_dir_fd,
            )
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise OSError
            os.fchmod(lock_fd, 0o600)
            return lock_fd
        except OSError:
            if lock_fd is not None:
                os.close(lock_fd)
            raise _error("worker_unavailable", retryable=True) from None
        finally:
            if lock_dir_fd is not None:
                os.close(lock_dir_fd)
            os.close(root_fd)

    @asynccontextmanager
    async def _run_lock(self, run_id: str) -> AsyncIterator[None]:
        descriptor = self._open_lock_file(run_id)
        locked = False
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as caught:
                    if caught.errno not in (errno.EACCES, errno.EAGAIN):
                        raise _error("worker_unavailable", retryable=True) from None
                    await asyncio.sleep(0.001)
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    async def _create_run(self, run_id: str) -> tuple[int, int]:
        async with self._lock:
            if run_id in self._active or run_id in self._recovered:
                raise _error("worker_conflict")
            root_fd = self._open_root(create=True)
            assert root_fd is not None
            created = False
            failed = False
            try:
                os.mkdir(run_id, mode=0o700, dir_fd=root_fd)
                created = True
            except OSError:
                failed = True
            if failed:
                os.close(root_fd)
                raise _error("worker_conflict")
            run_fd: int | None = None
            created_identity: tuple[int, int] | None = None
            try:
                created_stat = os.stat(run_id, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISDIR(created_stat.st_mode):
                    raise OSError
                created_identity = (created_stat.st_dev, created_stat.st_ino)
                self._run_identities[run_id] = created_identity
                run_fd = os.open(run_id, _DIRECTORY_FLAGS, dir_fd=root_fd)
                metadata = os.fstat(run_fd)
                if (metadata.st_dev, metadata.st_ino) != created_identity:
                    raise OSError
            except OSError:
                failed = True
            if failed or run_fd is None:
                if run_fd is not None:
                    os.close(run_fd)
                self._run_identities.pop(run_id, None)
                if created and created_identity is not None:
                    self._rmdir_if_identity(root_fd, run_id, created_identity)
                os.close(root_fd)
                raise _error("worker_unavailable", retryable=True)
            return root_fd, run_fd

    @staticmethod
    def _rmdir_if_identity(
        root_fd: int,
        run_id: str,
        identity: tuple[int, int],
    ) -> bool:
        try:
            current = os.stat(run_id, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                return False
            os.rmdir(run_id, dir_fd=root_fd)
            return True
        except OSError:
            return False

    def _atomic_state(self, run_id: str, state: _PersistedState) -> None:
        opened = self._open_run(run_id)
        assert opened is not None
        root_fd, run_fd = opened
        rendered: dict[str, object] = {
            "schema_version": "1.0",
            "state": state.state,
        }
        if state.exit_code is not None:
            rendered["exit_code"] = state.exit_code
        if state.output_size is not None:
            rendered["output_size"] = state.output_size
        if state.output_sha256 is not None:
            rendered["output_sha256"] = state.output_sha256
        if state.ownership_token is not None:
            rendered["ownership_token"] = state.ownership_token
        if state.image_reference is not None:
            rendered["image_reference"] = state.image_reference
        payload = json.dumps(
            rendered,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        temporary = f".state-{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        failed = False
        try:
            descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=run_fd)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    failed = True
                    break
                view = view[written:]
            if not failed:
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(
                    temporary,
                    "state.json",
                    src_dir_fd=run_fd,
                    dst_dir_fd=run_fd,
                )
                os.fsync(run_fd)
        except OSError:
            failed = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=run_fd)
            except OSError:
                pass
            os.close(run_fd)
            os.close(root_fd)
        if failed:
            raise _error("worker_unavailable", retryable=True)

    @staticmethod
    def _read_regular_file(directory_fd: int, name: str, limit: int) -> bytes:
        descriptor: int | None = None
        failed = False
        missing = False
        payload = bytearray()
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                failed = True
            else:
                while len(payload) <= limit:
                    chunk = os.read(descriptor, min(_DRAIN_CHUNK, limit + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > limit:
                    failed = True
        except FileNotFoundError:
            missing = True
        except OSError:
            failed = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if missing:
            raise FileNotFoundError
        if failed:
            raise _error("worker_unavailable", retryable=True)
        return bytes(payload)

    def _read_state(self, run_id: str) -> _PersistedState | None:
        opened = self._open_run(run_id, missing_ok=True)
        if opened is None:
            return None
        root_fd, run_fd = opened
        missing = False
        try:
            payload = self._read_regular_file(run_fd, "state.json", _STATE_LIMIT)
        except FileNotFoundError:
            missing = True
            payload = b""
        finally:
            os.close(run_fd)
            os.close(root_fd)
        if missing:
            return None
        invalid = False
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                invalid = True
                decoded = {}
        except Exception:
            invalid = True
            decoded = {}
        state = decoded.get("state")
        exit_code = decoded.get("exit_code")
        output_size = decoded.get("output_size")
        output_sha256 = decoded.get("output_sha256")
        ownership_token = decoded.get("ownership_token")
        image_reference = decoded.get("image_reference")
        common = {"schema_version", "state"}
        valid = not invalid and decoded.get("schema_version") == "1.0"
        if state == "running":
            if self.isolation == "local":
                valid = valid and set(decoded) == common
            else:
                valid = (
                    valid
                    and set(decoded) == common | {"ownership_token", "image_reference"}
                    and isinstance(ownership_token, str)
                    and re.fullmatch(r"[a-f0-9]{64}", ownership_token) is not None
                    and image_reference == getattr(self, "_image_reference", None)
                )
        elif state == "completed":
            valid = (
                valid
                and set(decoded) == common | {"exit_code", "output_size", "output_sha256"}
                and type(exit_code) is int
                and exit_code in (0, 2)
                and type(output_size) is int
                and 0 <= output_size <= self._max_output_bytes
                and isinstance(output_sha256, str)
                and re.fullmatch(r"[a-f0-9]{64}", output_sha256) is not None
            )
        elif state == "failed":
            valid = (
                valid
                and set(decoded) == common | {"exit_code"}
                and type(exit_code) is int
                and -255 <= exit_code <= 255
                and exit_code not in (0, 2)
            )
        elif state in {"canceled", "lost"}:
            valid = valid and set(decoded) == common
        else:
            valid = False
        if not valid:
            raise _error("worker_unavailable", retryable=True)
        return _PersistedState(
            state,
            exit_code,
            output_size,
            output_sha256,
            ownership_token,
            image_reference,
        )

    def _output_components(self) -> tuple[str, ...]:
        raise NotImplementedError

    def _output_path(self, run_id: str) -> Path:
        raise NotImplementedError

    def _read_output(self, run_id: str, state: _PersistedState | None = None) -> bytes:
        opened = self._open_run(run_id)
        assert opened is not None
        root_fd, current_fd = opened
        extra_fd: int | None = None
        failed = False
        components = self._output_components()
        if len(components) == 2:
            try:
                extra_fd = os.open(components[0], _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError:
                failed = True
            if not failed and extra_fd is not None:
                os.close(current_fd)
                current_fd = extra_fd
                extra_fd = None
        try:
            if failed:
                raise _error("worker_unavailable", retryable=True)
            payload = self._read_regular_file(current_fd, components[-1], self._max_output_bytes)
        except FileNotFoundError:
            raise _error("worker_unavailable", retryable=True) from None
        finally:
            if extra_fd is not None:
                os.close(extra_fd)
            os.close(current_fd)
            os.close(root_fd)
        digest = hashlib.sha256(payload).hexdigest()
        if state is not None and (
            state.output_size != len(payload)
            or state.output_sha256 is None
            or digest != state.output_sha256
        ):
            raise _error("worker_unavailable", retryable=True)
        return payload

    def _validate_start(
        self, run_id: str, staged: Any, question: str | None, timeout: object
    ) -> None:
        input_dir = Path(getattr(staged, "input_dir", ""))
        if (
            not _safe_run_id(run_id)
            or not _safe_question(question)
            or not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout <= 0
            or not _safe_path(input_dir)
        ):
            raise _error("worker_invalid")
        failed = False
        try:
            input_stat = os.lstat(input_dir)
            failed = not stat.S_ISDIR(input_stat.st_mode) or stat.S_ISLNK(input_stat.st_mode)
        except OSError:
            failed = True
        if failed:
            raise _error("worker_invalid")

    def _prepare_run_fd(self, _run_fd: int) -> None:
        return None

    async def _rollback_run(self, run_id: str) -> None:
        opened = self._open_run(run_id, missing_ok=True)
        if opened is None:
            return
        root_fd, run_fd = opened
        metadata = os.fstat(run_fd)
        opened_identity = (metadata.st_dev, metadata.st_ino)
        try:
            try:
                names = os.listdir(run_fd)
            except OSError:
                names = []
            for name in names:
                if name == "output":
                    output_fd: int | None = None
                    try:
                        output_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=run_fd)
                        for child in os.listdir(output_fd):
                            try:
                                os.unlink(child, dir_fd=output_fd)
                            except OSError:
                                pass
                    except OSError:
                        pass
                    finally:
                        if output_fd is not None:
                            os.close(output_fd)
                    try:
                        os.rmdir(name, dir_fd=run_fd)
                    except OSError:
                        pass
                else:
                    try:
                        os.unlink(name, dir_fd=run_fd)
                    except OSError:
                        pass
        finally:
            removed = self._rmdir_if_identity(root_fd, run_id, opened_identity)
            os.close(run_fd)
            os.close(root_fd)
            if removed:
                self._run_identities.pop(run_id, None)

    async def _spawn(
        self,
        *,
        run_id: str,
        staged: Any,
        argv: tuple[str, ...],
        timeout_seconds: float,
        ownership_token: str | None = None,
        image_reference: str | None = None,
    ) -> None:
        process: Any | None = None
        active: _ActiveRun | None = None
        monitor_coroutine: Any | None = None
        cancellation: BaseException | None = None
        failed = False
        root_fd: int | None = None
        run_fd: int | None = None
        owns_run = False
        try:
            root_fd, run_fd = await self._create_run(run_id)
            owns_run = True
            self._prepare_run_fd(run_fd)
            process = await self._process_factory(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            active = _ActiveRun(
                staged=staged,
                process=process,
                ownership_token=ownership_token,
                image_reference=image_reference,
            )
            self._atomic_state(
                run_id,
                _PersistedState(
                    "running",
                    ownership_token=ownership_token,
                    image_reference=image_reference,
                ),
            )
            self._active[run_id] = active
            monitor_coroutine = self._monitor(run_id, active, float(timeout_seconds))
            active.task = self._task_factory(monitor_coroutine)
            monitor_coroutine = None
            # Ensure the monitor has entered its cancellation-safe body before
            # ownership of the running task is returned to the caller.
            await asyncio.sleep(0)
        except BaseException as caught:
            failed = True
            if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                cancellation = caught
        finally:
            if run_fd is not None:
                os.close(run_fd)
            if root_fd is not None:
                os.close(root_fd)
        if not failed:
            return
        if monitor_coroutine is not None:
            monitor_coroutine.close()
        termination_confirmed = process is None
        if process is not None:
            termination_confirmed = (
                await self._terminate_spawned(
                    run_id,
                    process,
                    ownership_token=ownership_token,
                    image_reference=image_reference,
                )
            ).confirmed
        if owns_run and termination_confirmed:
            try:
                await self._rollback_run(run_id)
            except AndroidMemoryWorkerError:
                pass
        if termination_confirmed:
            self._active.pop(run_id, None)
            await _safe_cleanup(staged)
        elif active is not None:
            self._active[run_id] = active
        if cancellation is not None:
            raise cancellation
        raise _error("worker_unavailable", retryable=True)

    async def _terminate_spawned(
        self,
        run_id: str,
        process: Any,
        *,
        ownership_token: str | None = None,
        image_reference: str | None = None,
    ) -> _TerminationResult:
        return await self._terminate_backend(run_id, process)

    async def _terminate_backend(
        self,
        _run_id: str,
        process: Any,
        *,
        ownership_token: str | None = None,
        image_reference: str | None = None,
    ) -> _TerminationResult:
        return_code = getattr(process, "returncode", None)
        if type(return_code) is int:
            return _TerminationResult(True, return_code)
        try:
            process.kill()
            exit_code = await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS)
        except ProcessLookupError:
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS)
            except BaseException as caught:
                if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return _TerminationResult(False, had_error=True)
            return _TerminationResult(True, exit_code)
        except BaseException as caught:
            if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return _TerminationResult(False, had_error=True)
        return _TerminationResult(True, exit_code)

    async def _terminate_once(self, run_id: str, active: _ActiveRun) -> _TerminationResult:
        if active.termination_task is None:
            active.termination_task = asyncio.create_task(
                self._terminate_backend(
                    run_id,
                    active.process,
                    ownership_token=active.ownership_token,
                    image_reference=active.image_reference,
                )
            )
        result = await asyncio.shield(active.termination_task)
        if not result.confirmed:
            active.termination_task = None
        return result

    async def _confirm_once(self, run_id: str, active: _ActiveRun) -> _TerminationResult:
        if active.termination_task is None:
            active.termination_task = asyncio.create_task(
                self._confirm_process_exit(run_id, active)
            )
        result = await asyncio.shield(active.termination_task)
        if not result.confirmed:
            active.termination_task = None
        return result

    async def _finalize_active(
        self,
        run_id: str,
        active: _ActiveRun,
        persisted: _PersistedState,
    ) -> None:
        active.pending_terminal = persisted
        async with self._run_lock(run_id):
            current = self._read_state(run_id)
            if current is not None and current.state == "running":
                self._atomic_state(run_id, persisted)
            elif current is not None:
                persisted = current
        active.state = persisted.state
        active.exit_code = persisted.exit_code
        try:
            await _cleanup_cancellation_safe(active)
        finally:
            if active.cleanup_task is not None and active.cleanup_task.done():
                if self._active.get(run_id) is active:
                    self._active.pop(run_id, None)

    async def _confirm_process_exit(
        self,
        _run_id: str,
        active: _ActiveRun,
    ) -> _TerminationResult:
        return_code = getattr(active.process, "returncode", None)
        if type(return_code) is int:
            return _TerminationResult(True, return_code)
        return await self._terminate_backend(
            _run_id,
            active.process,
            ownership_token=active.ownership_token,
            image_reference=active.image_reference,
        )

    async def _retry_active_terminal(self, run_id: str, active: _ActiveRun) -> WorkerState:
        pending = active.pending_terminal
        if pending is None:
            return active.state
        if not active.process_confirmed:
            confirmation = await self._confirm_once(run_id, active)
            if not confirmation.confirmed:
                active.terminal_error = _error("worker_unavailable", retryable=True)
                raise active.terminal_error
            active.process_confirmed = True
        try:
            await self._finalize_active(run_id, active, pending)
        except AndroidMemoryWorkerError as caught:
            active.terminal_error = caught
            raise
        active.terminal_error = None
        return active.state

    async def _monitor(self, run_id: str, active: _ActiveRun, timeout: float) -> None:
        stdout_task = asyncio.create_task(_drain_bounded(active.process.stdout))
        stderr_task = asyncio.create_task(_drain_bounded(active.process.stderr))
        timed_out = False
        exit_code = -1
        termination_result: _TerminationResult | None = None
        wait_failed = False
        try:
            try:
                exit_code = await asyncio.wait_for(active.process.wait(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                termination_result = await self._terminate_once(run_id, active)
                if not termination_result.confirmed:
                    active.terminal_error = _error("worker_unavailable", retryable=True)
                    return
                exit_code = (
                    termination_result.exit_code if termination_result.exit_code is not None else -1
                )
            except BaseException as caught:
                if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                wait_failed = True
            if termination_result is None and active.termination_task is not None:
                termination_result = await asyncio.shield(active.termination_task)
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            active.exit_code = exit_code
            if wait_failed or (termination_result is not None and termination_result.had_error):
                active.terminal_error = _error("worker_unavailable", retryable=True)
                persisted = _PersistedState("lost")
            elif active.cancel_requested:
                persisted = _PersistedState("canceled")
            elif timed_out or exit_code not in (0, 1, 2) or exit_code == 1:
                persisted = _PersistedState("failed", exit_code)
            else:
                try:
                    payload = self._read_output(run_id)
                except AndroidMemoryWorkerError:
                    persisted = _PersistedState("failed", -1)
                else:
                    persisted = _PersistedState(
                        "completed",
                        exit_code,
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                    )
            active.pending_terminal = persisted
            if termination_result is None:
                termination_result = await self._confirm_once(run_id, active)
            if not termination_result.confirmed:
                active.terminal_error = _error("worker_unavailable", retryable=True)
                return
            active.process_confirmed = True
            await self._finalize_active(run_id, active, persisted)
        except asyncio.CancelledError:
            if self._active.get(run_id) is not active:
                raise
            if active.pending_terminal is not None and active.process_confirmed:
                try:
                    await self._finalize_active(run_id, active, active.pending_terminal)
                except AndroidMemoryWorkerError as caught:
                    active.terminal_error = caught
                raise
            result = await self._terminate_once(run_id, active)
            if result.confirmed:
                active.process_confirmed = True
                pending = active.pending_terminal or _PersistedState("lost")
                active.pending_terminal = pending
                try:
                    await self._finalize_active(run_id, active, pending)
                except AndroidMemoryWorkerError as caught:
                    active.terminal_error = caught
            else:
                active.terminal_error = _error("worker_unavailable", retryable=True)
            raise
        except AndroidMemoryWorkerError as caught:
            active.terminal_error = caught
        except BaseException:
            if active.process_confirmed and active.pending_terminal is not None:
                active.terminal_error = _error("worker_unavailable", retryable=True)
                return
            result = await self._terminate_once(run_id, active)
            if result.confirmed:
                active.process_confirmed = True
                active.terminal_error = _error("worker_unavailable", retryable=True)
                pending = active.pending_terminal or _PersistedState("lost")
                active.pending_terminal = pending
                try:
                    await self._finalize_active(run_id, active, pending)
                except AndroidMemoryWorkerError as caught:
                    active.terminal_error = caught
            else:
                active.terminal_error = _error("worker_unavailable", retryable=True)
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    async def status(self, run_id: str) -> WorkerState:
        if not _safe_run_id(run_id):
            raise _error("worker_invalid")
        active = self._active.get(run_id)
        if active is not None:
            if active.pending_terminal is not None:
                return await self._retry_active_terminal(run_id, active)
            return active.state
        async with self._run_lock(run_id):
            persisted = self._read_state(run_id)
        if persisted is None:
            return "lost"
        if persisted.state != "running":
            self._recovered.discard(run_id)
            return persisted.state
        return await self._recover_running(run_id)

    async def _recover_running(self, run_id: str) -> WorkerState:
        async with self._run_lock(run_id):
            current = self._read_state(run_id)
            if current is not None and current.state == "running":
                self._atomic_state(run_id, _PersistedState("lost"))
        return "lost"

    async def result(self, run_id: str) -> MemoryWorkerResult:
        state = await self.status(run_id)
        active = self._active.get(run_id)
        persisted = self._read_state(run_id)
        exit_code = (
            active.exit_code
            if active is not None and active.exit_code is not None
            else (-1 if persisted is None or persisted.exit_code is None else persisted.exit_code)
        )
        if state == "running":
            raise _error("worker_not_ready", retryable=True)
        if state == "completed":
            payload = self._read_output(run_id, persisted)
            return MemoryWorkerResult(exit_code=exit_code, payload=payload)
        return MemoryWorkerResult(exit_code=exit_code, payload=None)

    async def cancel(self, run_id: str) -> None:
        if not _safe_run_id(run_id):
            raise _error("worker_invalid")
        active = self._active.get(run_id)
        if active is None or active.state != "running":
            return
        if active.pending_terminal is not None:
            await self._retry_active_terminal(run_id, active)
            if active.terminal_error is not None:
                raise active.terminal_error
            return
        active.cancel_requested = True
        active.terminal_error = None
        if active.task is None:
            result = await self._terminate_once(run_id, active)
            active.exit_code = result.exit_code
            if not result.confirmed:
                active.terminal_error = _error("worker_unavailable", retryable=True)
                raise active.terminal_error
            if result.had_error:
                active.terminal_error = _error("worker_unavailable", retryable=True)
            await self._rollback_run(run_id)
            await _safe_cleanup(active.staged)
            self._active.pop(run_id, None)
            if active.terminal_error is not None:
                raise active.terminal_error
            return
        async with self._run_lock(run_id):
            current = self._read_state(run_id)
            if current is None or current.state == "running":
                result = await self._terminate_once(run_id, active)
                active.exit_code = result.exit_code
                if not result.confirmed:
                    active.terminal_error = _error("worker_unavailable", retryable=True)
                    raise active.terminal_error
                if result.had_error:
                    active.terminal_error = _error("worker_unavailable", retryable=True)
                    self._atomic_state(run_id, _PersistedState("lost"))
                else:
                    self._atomic_state(run_id, _PersistedState("canceled"))
        if active.task is not None:
            await asyncio.shield(active.task)
        if active.terminal_error is not None:
            raise active.terminal_error

    async def shutdown(self) -> None:
        while True:
            run_ids = tuple(self._active)
            if not run_ids:
                return
            await asyncio.gather(
                *(self.cancel(run_id) for run_id in run_ids),
                return_exceptions=True,
            )
            if tuple(self._active) == run_ids:
                return


class LocalAndroidMemoryWorker(_WorkerBase):
    isolation: Literal["local"] = "local"

    def __init__(
        self,
        *,
        python_binary: Path,
        repository_root: Path,
        run_root: Path,
        runtime_commit: str,
        max_output_bytes: int,
        process_factory: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
        commit_resolver: Callable[[Path], Awaitable[str]] = _default_commit_resolver,
    ) -> None:
        python_binary = Path(python_binary)
        repository_root = Path(repository_root)
        if (
            not _safe_path(python_binary, allow_commas=True)
            or not _safe_path(repository_root, allow_commas=True)
            or _COMMIT.fullmatch(runtime_commit) is None
        ):
            raise ValueError("Android memory worker configuration is invalid")
        super().__init__(
            run_root=run_root,
            process_factory=process_factory,
            max_output_bytes=max_output_bytes,
        )
        self._python_binary = python_binary
        self._repository_root = repository_root
        self._runtime_commit = runtime_commit
        self._commit_resolver = commit_resolver

    def _output_components(self) -> tuple[str, ...]:
        return ("context.json",)

    def _output_path(self, run_id: str) -> Path:
        return self._run_root / run_id / "context.json"

    async def start(
        self,
        *,
        run_id: str,
        staged: StagedMemoryInput,
        question: str | None,
        timeout_seconds: int,
    ) -> None:
        delegated = False
        cancellation: BaseException | None = None
        failure: AndroidMemoryWorkerError | None = None
        try:
            self._validate_start(run_id, staged, question, timeout_seconds)
            resolved_commit = await self._commit_resolver(self._repository_root)
            if resolved_commit != self._runtime_commit:
                failure = _error("worker_unavailable", retryable=True)
            else:
                argv = (
                    str(self._python_binary),
                    str(self._repository_root / "tools" / "ai_context.py"),
                    "--dump-dir",
                    str(staged.input_dir),
                    "--question",
                    question or "",
                    "--format",
                    "json",
                    "--strict",
                    "--output",
                    str(self._output_path(run_id)),
                )
                delegated = True
                await self._spawn(
                    run_id=run_id,
                    staged=staged,
                    argv=argv,
                    timeout_seconds=timeout_seconds,
                )
        except BaseException as caught:
            if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                cancellation = caught
            elif isinstance(caught, AndroidMemoryWorkerError):
                failure = caught
            else:
                failure = _error("worker_unavailable", retryable=True)
        if cancellation is not None:
            if not delegated:
                await _safe_cleanup(staged)
            raise cancellation
        if failure is not None:
            if not delegated:
                await _safe_cleanup(staged)
            raise failure


class OciAndroidMemoryWorker(_WorkerBase):
    isolation: Literal["oci"] = "oci"

    def __init__(
        self,
        *,
        container_runtime: Path,
        image_reference: str,
        run_root: Path,
        max_output_bytes: int,
        pids_limit: int,
        memory_bytes: int,
        cpu_limit: float,
        tmpfs_bytes: int,
        process_factory: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
    ) -> None:
        container_runtime = Path(container_runtime)
        if (
            not _safe_path(container_runtime)
            or not is_valid_android_memory_image_reference(image_reference)
            or type(pids_limit) is not int
            or not 16 <= pids_limit <= 4096
            or type(memory_bytes) is not int
            or memory_bytes < 1
            or not isinstance(cpu_limit, int | float)
            or isinstance(cpu_limit, bool)
            or not math.isfinite(float(cpu_limit))
            or not 0 < cpu_limit <= 64
            or type(tmpfs_bytes) is not int
            or tmpfs_bytes < 1
        ):
            raise ValueError("Android memory worker configuration is invalid")
        super().__init__(
            run_root=run_root,
            process_factory=process_factory,
            max_output_bytes=max_output_bytes,
        )
        self._container_runtime = container_runtime
        self._image_reference = image_reference
        self._pids_limit = pids_limit
        self._memory_bytes = memory_bytes
        self._cpu_limit = float(cpu_limit)
        self._tmpfs_bytes = tmpfs_bytes

    def _output_components(self) -> tuple[str, ...]:
        return ("output", "context.json")

    def _output_dir(self, run_id: str) -> Path:
        return self._run_root / run_id / "output"

    def _output_path(self, run_id: str) -> Path:
        return self._output_dir(run_id) / "context.json"

    def _prepare_run_fd(self, run_fd: int) -> None:
        output_fd: int | None = None
        failed = False
        try:
            os.mkdir("output", mode=0o733, dir_fd=run_fd)
            output_fd = os.open("output", _DIRECTORY_FLAGS, dir_fd=run_fd)
            metadata = os.fstat(output_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                failed = True
            os.fchmod(output_fd, 0o733)
        except OSError:
            failed = True
        finally:
            if output_fd is not None:
                os.close(output_fd)
        if failed:
            raise _error("worker_unavailable", retryable=True)

    async def start(
        self,
        *,
        run_id: str,
        staged: StagedMemoryInput,
        question: str | None,
        timeout_seconds: int,
    ) -> None:
        delegated = False
        cancellation: BaseException | None = None
        failure: AndroidMemoryWorkerError | None = None
        try:
            self._validate_start(run_id, staged, question, timeout_seconds)
            output_dir = self._output_dir(run_id)
            ownership_token = secrets.token_hex(32)
            argv = (
                str(self._container_runtime),
                "run",
                "--rm",
                "--name",
                run_id,
                "--label",
                f"com.perfpilot.memory.owner={ownership_token}",
                "--label",
                f"com.perfpilot.memory.run={run_id}",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(self._pids_limit),
                "--memory",
                str(self._memory_bytes),
                "--cpus",
                str(self._cpu_limit),
                "--mount",
                f"type=bind,src={staged.input_dir},dst=/work/input,readonly",
                "--mount",
                f"type=bind,src={output_dir},dst=/work/output",
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,nodev,size={self._tmpfs_bytes}",
                self._image_reference,
                "--dump-dir",
                "/work/input",
                "--question",
                question or "",
                "--format",
                "json",
                "--strict",
                "--output",
                "/work/output/context.json",
            )
            delegated = True
            await self._spawn(
                run_id=run_id,
                staged=staged,
                argv=argv,
                timeout_seconds=timeout_seconds,
                ownership_token=ownership_token,
                image_reference=self._image_reference,
            )
        except BaseException as caught:
            if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                cancellation = caught
            elif isinstance(caught, AndroidMemoryWorkerError):
                failure = caught
            else:
                failure = _error("worker_unavailable", retryable=True)
        if cancellation is not None:
            if not delegated:
                await _safe_cleanup(staged)
            raise cancellation
        if failure is not None:
            if not delegated:
                await _safe_cleanup(staged)
            raise failure

    async def _runtime_command(self, argv: tuple[str, ...]) -> _RuntimeResult:
        process: Any | None = None
        stdout_task: asyncio.Task[tuple[bytes, bool]] | None = None
        stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        failed = False
        exit_code = -1
        stdout = b""
        stderr = b""
        overflow = False
        try:
            process = await self._process_factory(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_task = asyncio.create_task(_drain_bounded(process.stdout))
            stderr_task = asyncio.create_task(_drain_bounded(process.stderr))
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                failed = True
                exit_code = await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS)
            try:
                (stdout, stdout_overflow), (stderr, stderr_overflow) = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task),
                    timeout=_CONTROL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                failed = True
                overflow = True
                for task in (stdout_task, stderr_task):
                    task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                return _RuntimeResult(exit_code, b"", b"", overflow, failed)
            overflow = stdout_overflow or stderr_overflow
        except BaseException as caught:
            if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            failed = True
            if process is not None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS)
                except Exception:
                    pass
        finally:
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            if stdout_task is not None and stderr_task is not None:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return _RuntimeResult(exit_code, stdout, stderr, overflow, failed)

    async def _inspect_container(
        self,
        run_id: str,
        ownership_token: str,
        image_reference: str,
    ) -> _InspectResult:
        result = await self._runtime_command(
            (
                str(self._container_runtime),
                "inspect",
                "--format",
                "{{json .}}",
                run_id,
            )
        )
        if result.failed or result.overflow:
            return _InspectResult("unavailable")
        if result.exit_code != 0:
            if b"no such object" in result.stderr.lower():
                return _InspectResult("absent")
            return _InspectResult("unavailable")
        try:
            decoded = json.loads(result.stdout)
            labels = decoded["Config"]["Labels"]
            running = decoded["State"]["Running"]
            name = decoded["Name"]
            image = decoded["Config"]["Image"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _InspectResult("unavailable")
        if type(running) is not bool:
            return _InspectResult("unavailable")
        if (
            name != f"/{run_id}"
            or image != image_reference
            or not isinstance(labels, dict)
            or labels.get("com.perfpilot.memory.owner") != ownership_token
            or labels.get("com.perfpilot.memory.run") != run_id
        ):
            return _InspectResult("foreign")
        return _InspectResult("live" if running else "stopped")

    async def _reap_original(self, process: Any) -> tuple[int | None, bool]:
        return_code = getattr(process, "returncode", None)
        if type(return_code) is int:
            return return_code, False
        try:
            return await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS), False
        except TimeoutError:
            try:
                process.kill()
                return (
                    await asyncio.wait_for(process.wait(), timeout=_CONTROL_TIMEOUT_SECONDS),
                    True,
                )
            except Exception:
                return None, True
        except Exception:
            return None, True

    async def _terminate_owned(
        self,
        run_id: str,
        process: Any | None,
        ownership_token: str,
        image_reference: str,
    ) -> _TerminationResult:
        inspected = await self._inspect_container(run_id, ownership_token, image_reference)
        if inspected.kind in {"unavailable", "foreign"}:
            return _TerminationResult(False, had_error=True)
        if inspected.kind in {"absent", "stopped"}:
            if process is None:
                return _TerminationResult(True)
            exit_code, reap_error = await self._reap_original(process)
            return _TerminationResult(True, exit_code, reap_error)
        had_error = False
        if inspected.kind == "live":
            killed = await self._runtime_command((str(self._container_runtime), "kill", run_id))
            if killed.failed or killed.overflow or killed.exit_code != 0:
                had_error = True
        inspected = await self._inspect_container(run_id, ownership_token, image_reference)
        if inspected.kind == "live":
            removed = await self._runtime_command(
                (str(self._container_runtime), "rm", "--force", run_id)
            )
            if removed.failed or removed.overflow or removed.exit_code != 0:
                had_error = True
            inspected = await self._inspect_container(run_id, ownership_token, image_reference)
        if inspected.kind not in {"absent", "stopped"}:
            return _TerminationResult(False, had_error=True)
        exit_code: int | None = None
        if process is not None:
            exit_code, reap_error = await self._reap_original(process)
            had_error = had_error or reap_error
        return _TerminationResult(True, exit_code, had_error)

    async def _confirm_process_exit(
        self,
        run_id: str,
        active: _ActiveRun,
    ) -> _TerminationResult:
        if active.ownership_token is None or active.image_reference is None:
            return _TerminationResult(False, had_error=True)
        return await self._terminate_owned(
            run_id,
            active.process,
            active.ownership_token,
            active.image_reference,
        )

    async def _terminate_spawned(
        self,
        run_id: str,
        process: Any,
        *,
        ownership_token: str | None = None,
        image_reference: str | None = None,
    ) -> _TerminationResult:
        if ownership_token is None or image_reference is None:
            return _TerminationResult(False, had_error=True)
        return await self._terminate_owned(run_id, process, ownership_token, image_reference)

    async def _terminate_backend(
        self,
        run_id: str,
        process: Any,
        *,
        ownership_token: str | None = None,
        image_reference: str | None = None,
    ) -> _TerminationResult:
        if ownership_token is None or image_reference is None:
            persisted = self._read_state(run_id)
            if (
                persisted is None
                or persisted.ownership_token is None
                or persisted.image_reference is None
            ):
                return _TerminationResult(False, had_error=True)
            ownership_token = persisted.ownership_token
            image_reference = persisted.image_reference
        return await self._terminate_owned(
            run_id,
            process,
            ownership_token,
            image_reference,
        )

    async def _recover_running(self, run_id: str) -> WorkerState:
        async with self._run_lock(run_id):
            persisted = self._read_state(run_id)
            if persisted is None or persisted.state != "running":
                self._recovered.discard(run_id)
                return "lost" if persisted is None else persisted.state
            assert persisted.ownership_token is not None
            assert persisted.image_reference is not None
            inspected = await self._inspect_container(
                run_id, persisted.ownership_token, persisted.image_reference
            )
            if inspected.kind == "live":
                self._recovered.add(run_id)
                return "running"
            if inspected.kind in {"unavailable", "foreign"}:
                raise _error(
                    "worker_conflict" if inspected.kind == "foreign" else "worker_unavailable",
                    retryable=inspected.kind == "unavailable",
                )
            self._recovered.discard(run_id)
            self._atomic_state(run_id, _PersistedState("lost"))
            return "lost"

    async def cancel(self, run_id: str) -> None:
        if not _safe_run_id(run_id):
            raise _error("worker_invalid")
        active = self._active.get(run_id)
        if active is not None:
            await super().cancel(run_id)
            return
        async with self._run_lock(run_id):
            persisted = self._read_state(run_id)
            if persisted is None or persisted.state != "running":
                self._recovered.discard(run_id)
                return
            assert persisted.ownership_token is not None
            assert persisted.image_reference is not None
            result = await self._terminate_owned(
                run_id,
                None,
                persisted.ownership_token,
                persisted.image_reference,
            )
            self._recovered.discard(run_id)
            if not result.confirmed:
                raise _error("worker_unavailable", retryable=True)
            if result.had_error:
                self._atomic_state(run_id, _PersistedState("lost"))
                raise _error("worker_unavailable", retryable=True)
            self._atomic_state(run_id, _PersistedState("canceled"))


__all__ = [
    "AndroidMemoryWorker",
    "AndroidMemoryWorkerError",
    "LocalAndroidMemoryWorker",
    "MemoryWorkerResult",
    "OciAndroidMemoryWorker",
    "WorkerState",
]
