from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

import perfpilot_api.engines.android_memory_worker as worker_module
from perfpilot_api.engines.android_memory_worker import (
    AndroidMemoryWorkerError,
    LocalAndroidMemoryWorker,
    MemoryWorkerResult,
    OciAndroidMemoryWorker,
)


COMMIT = "d5514972ced78c3faa7fc17589c1ea9231645056"
IMAGE = "registry.example/android-memory@sha256:" + "a" * 64
OWNER = "b" * 64
OWNER_LABEL = "com.perfpilot.memory.owner"
RUN_LABEL = "com.perfpilot.memory.run"


def _running_state(*, owner: str | None = None, image: str | None = None) -> dict[str, object]:
    state: dict[str, object] = {"schema_version": "1.0", "state": "running"}
    if owner is not None:
        state["ownership_token"] = owner
    if image is not None:
        state["image_reference"] = image
    return state


def _inspect_json(
    *,
    owner: str = OWNER,
    image: str = IMAGE,
    run_id: str = "memory-run-1",
    running: bool = True,
) -> bytes:
    return (
        json.dumps(
            {
                "Id": "container-id",
                "Name": f"/{run_id}",
                "Config": {
                    "Image": image,
                    "Labels": {OWNER_LABEL: owner, RUN_LABEL: run_id},
                },
                "State": {"Running": running},
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


@dataclass
class FakeStaged:
    input_dir: Path
    cleanup_calls: int = 0
    cleanup_error: Exception | None = None
    events: list[str] | None = None
    process: FakeProcess | None = None

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self.events is not None:
            self.events.append("cleanup")
        if self.process is not None:
            assert self.process.returncode is not None
        if self.cleanup_error is not None:
            raise self.cleanup_error


class FakeReader:
    def __init__(self, payload: bytes, *, never_eof: bool = False) -> None:
        self._chunks = [payload, b""]
        self._never_eof = never_eof
        self._blocked = asyncio.Event()

    async def read(self, _size: int = -1) -> bytes:
        await asyncio.sleep(0)
        if self._never_eof and len(self._chunks) == 1:
            await self._blocked.wait()
        return self._chunks.pop(0)


class FakeProcess:
    def __init__(
        self,
        *,
        exit_code: int,
        stdout: bytes = b"ignored stdout",
        stderr: bytes = b"",
        blocked: bool = False,
        label: str = "run",
        events: list[str] | None = None,
        pipe_never_eof: bool = False,
        wait_failure: Exception | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.returncode: int | None = None
        self.stdout = FakeReader(stdout, never_eof=pipe_never_eof)
        self.stderr = FakeReader(stderr, never_eof=pipe_never_eof)
        self._done = asyncio.Event()
        if not blocked:
            self._done.set()
        self.terminated = 0
        self.killed = 0
        self.label = label
        self.events = events
        self.wait_failure = wait_failure

    async def wait(self) -> int:
        if self.wait_failure is not None:
            failure = self.wait_failure
            self.wait_failure = None
            raise failure
        await self._done.wait()
        self.returncode = self.exit_code
        if self.events is not None:
            self.events.append(f"reaped:{self.label}")
        return self.exit_code

    def terminate(self) -> None:
        self.terminated += 1
        self.exit_code = -15
        self._done.set()

    def kill(self) -> None:
        self.killed += 1
        self.exit_code = -9
        self._done.set()


class FakeProcessFactory:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        payload: bytes | None = b'{"context_type":"android-memory-ai-context"}',
        stderr: bytes = b"",
        blocked: bool = False,
        failure: Exception | None = None,
        inspect_exit: int | list[int] = 0,
        inspect_stdout: bytes | list[bytes] | None = None,
        inspect_stderr: bytes | list[bytes] = b"",
        kill_exit: int = 0,
        kill_stderr: bytes = b"",
        kill_failure: Exception | None = None,
        rm_exit: int = 0,
        rm_failure: Exception | None = None,
        pipe_never_eof: bool = False,
        wait_failure: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.payload = payload
        self.stderr = stderr
        self.blocked = blocked
        self.failure = failure
        self.inspect_exit = inspect_exit
        self.inspect_stdout = inspect_stdout
        self.inspect_stderr = inspect_stderr
        self.kill_exit = kill_exit
        self.kill_stderr = kill_stderr
        self.kill_failure = kill_failure
        self.rm_exit = rm_exit
        self.rm_failure = rm_failure
        self.pipe_never_eof = pipe_never_eof
        self.wait_failure = wait_failure
        self.events = events
        self.owner = OWNER
        self.image = IMAGE
        self.run_id = "memory-run-1"
        self.container_exists = True
        self.container_running = True
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *argv: object, **kwargs: object) -> FakeProcess:
        rendered = tuple(str(value) for value in argv)
        self.calls.append((rendered, kwargs))
        if len(rendered) >= 2 and rendered[1] == "inspect":
            inspect_exit = (
                self.inspect_exit.pop(0)
                if isinstance(self.inspect_exit, list)
                else self.inspect_exit
            )
            stderr = (
                self.inspect_stderr.pop(0)
                if isinstance(self.inspect_stderr, list)
                else self.inspect_stderr
            )
            if self.inspect_stdout is None:
                if self.container_exists:
                    stdout = _inspect_json(
                        owner=self.owner,
                        image=self.image,
                        run_id=self.run_id,
                        running=self.container_running,
                    )
                else:
                    stdout = b""
                    inspect_exit = 1
                    stderr = b"Error: No such object"
            else:
                stdout = (
                    self.inspect_stdout.pop(0)
                    if isinstance(self.inspect_stdout, list)
                    else self.inspect_stdout
                )
            process = FakeProcess(
                exit_code=inspect_exit,
                stdout=stdout,
                stderr=stderr,
                label="inspect",
                events=self.events,
            )
            self.processes.append(process)
            return process
        if len(rendered) >= 2 and rendered[1] == "kill":
            if self.kill_failure is not None:
                raise self.kill_failure
            if self.kill_exit == 0:
                self.container_running = False
                self.container_exists = False
                for process in self.processes:
                    if process.label == "run" and process.returncode is None:
                        process.exit_code = -9
                        process._done.set()
            process = FakeProcess(
                exit_code=self.kill_exit,
                stderr=self.kill_stderr,
                label="kill",
                events=self.events,
            )
            self.processes.append(process)
            return process
        if len(rendered) >= 2 and rendered[1] == "rm":
            if self.rm_failure is not None:
                raise self.rm_failure
            if self.rm_exit == 0:
                self.container_running = False
                self.container_exists = False
                for process in self.processes:
                    if process.label == "run" and process.returncode is None:
                        process.exit_code = -9
                        process._done.set()
            process = FakeProcess(exit_code=self.rm_exit, label="rm", events=self.events)
            self.processes.append(process)
            return process
        if self.failure is not None:
            raise self.failure

        process = FakeProcess(
            exit_code=self.exit_code,
            stderr=self.stderr,
            blocked=self.blocked,
            label="run",
            events=self.events,
            pipe_never_eof=self.pipe_never_eof,
            wait_failure=self.wait_failure,
        )
        self.processes.append(process)
        if len(rendered) >= 2 and rendered[1] == "run":
            self.run_id = rendered[rendered.index("--name") + 1]
            self.image = next(value for value in rendered if "@sha256:" in value)
            labels = [
                rendered[index + 1] for index, value in enumerate(rendered) if value == "--label"
            ]
            for label in labels:
                key, _, value = label.partition("=")
                if key == OWNER_LABEL:
                    self.owner = value
            self.container_exists = True
            self.container_running = True
        if self.payload is not None:
            output_path = self._host_output(rendered)
            output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            output_path.write_bytes(self.payload)
        return process

    @staticmethod
    def _host_output(argv: tuple[str, ...]) -> Path:
        output = argv[argv.index("--output") + 1]
        if output != "/work/output/context.json":
            return Path(output)
        output_mount = next(
            value
            for value in argv
            if value.startswith("type=bind,src=") and "dst=/work/output" in value
        )
        source = output_mount.removeprefix("type=bind,src=").split(",dst=", 1)[0]
        return Path(source) / "context.json"


async def _commit(_root: Path) -> str:
    return COMMIT


def _staged(tmp_path: Path) -> FakeStaged:
    input_dir = tmp_path / "staged-input"
    input_dir.mkdir(mode=0o700)
    return FakeStaged(input_dir=input_dir)


def _local(
    tmp_path: Path,
    factory: FakeProcessFactory,
    **changes: object,
) -> LocalAndroidMemoryWorker:
    repository = tmp_path / "checkout"
    repository.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "python_binary": Path("/usr/local/bin/python3"),
        "repository_root": repository,
        "run_root": tmp_path / "runs",
        "runtime_commit": COMMIT,
        "process_factory": factory,
        "commit_resolver": _commit,
        "max_output_bytes": 1024,
    }
    values.update(changes)
    return LocalAndroidMemoryWorker(**values)  # type: ignore[arg-type]


def _oci(
    tmp_path: Path,
    factory: FakeProcessFactory,
    **changes: object,
) -> OciAndroidMemoryWorker:
    values: dict[str, object] = {
        "container_runtime": Path("/usr/bin/docker"),
        "image_reference": IMAGE,
        "run_root": tmp_path / "runs",
        "process_factory": factory,
        "max_output_bytes": 1024,
        "pids_limit": 128,
        "memory_bytes": 8 * 1024**3,
        "cpu_limit": 4.0,
        "tmpfs_bytes": 1024**3,
    }
    values.update(changes)
    return OciAndroidMemoryWorker(**values)  # type: ignore[arg-type]


class FakeDelayedTerminalWorker:
    def __init__(self) -> None:
        self.status_calls = 0

    async def status(self, _run_id: str) -> str:
        self.status_calls += 1
        return "running" if self.status_calls <= 100 else "failed"


async def _terminal(worker: object, run_id: str) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        state = await worker.status(run_id)  # type: ignore[attr-defined]
        if state != "running":
            return state
        if loop.time() >= deadline:
            raise AssertionError("worker did not reach a terminal state within 5.0 seconds")
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_terminal_wait_is_not_limited_by_scheduler_turn_count() -> None:
    worker = FakeDelayedTerminalWorker()

    assert await _terminal(worker, "memory-run-1") == "failed"
    assert worker.status_calls == 101


def test_worker_result_is_frozen_slots_and_worker_isolation_is_explicit(tmp_path: Path) -> None:
    result = MemoryWorkerResult(exit_code=0, payload=b"{}")
    assert result.payload == b"{}"
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]
    assert _local(tmp_path, FakeProcessFactory()).isolation == "local"
    assert _oci(tmp_path, FakeProcessFactory()).isolation == "oci"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "state", "has_payload"),
    [(0, "completed", True), (1, "failed", False), (2, "completed", True), (7, "failed", False)],
)
async def test_local_worker_maps_exit_codes_and_cleans_exactly_once(
    tmp_path: Path,
    exit_code: int,
    state: str,
    has_payload: bool,
) -> None:
    staged = _staged(tmp_path)
    worker = _local(tmp_path, FakeProcessFactory(exit_code=exit_code))
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == state
    result = await worker.result("memory-run-1")
    assert result.exit_code == exit_code
    assert (result.payload is not None) is has_payload
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_local_worker_uses_fixed_no_shell_argv_and_question_is_one_argument(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(exit_code=2)
    worker = _local(tmp_path, factory)
    staged = _staged(tmp_path)
    question = "retained activity; $(touch /tmp/forbidden)"

    await worker.start(run_id="memory-run-1", staged=staged, question=question, timeout_seconds=60)  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == "completed"

    argv, kwargs = factory.calls[0]
    assert argv == (
        "/usr/local/bin/python3",
        str(tmp_path / "checkout" / "tools" / "ai_context.py"),
        "--dump-dir",
        str(staged.input_dir),
        "--question",
        question,
        "--format",
        "json",
        "--strict",
        "--output",
        str(tmp_path / "runs" / "memory-run-1" / "context.json"),
    )
    assert "shell" not in kwargs
    assert "env" not in kwargs
    assert not Path("/tmp/forbidden").exists()


@pytest.mark.asyncio
async def test_local_checkout_commit_mismatch_fails_closed_and_cleans_input(tmp_path: Path) -> None:
    async def mismatch(_root: Path) -> str:
        return "0" * 40

    staged = _staged(tmp_path)
    factory = FakeProcessFactory()
    worker = _local(tmp_path, factory, commit_resolver=mismatch)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    assert caught.value.stable_code == "worker_unavailable"
    assert COMMIT not in repr(caught.value)
    assert staged.cleanup_calls == 1
    assert factory.calls == []


@pytest.mark.asyncio
async def test_process_start_failure_preserves_primary_error_and_cleanup_failure_is_secondary(
    tmp_path: Path,
) -> None:
    primary = "process-secret-marker"
    secondary = "cleanup-secret-marker"
    staged = _staged(tmp_path)
    staged.cleanup_error = RuntimeError(secondary)
    worker = _local(tmp_path, FakeProcessFactory(failure=RuntimeError(primary)))

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.start(
            run_id="memory-run-1", staged=staged, question="private question", timeout_seconds=60
        )  # type: ignore[arg-type]

    rendered = f"{caught.value!s} {caught.value!r}"
    assert primary not in rendered
    assert secondary not in rendered
    assert "private question" not in rendered
    assert staged.cleanup_calls == 1
    assert await worker.status("memory-run-1") == "lost"


@pytest.mark.asyncio
async def test_state_write_failure_after_spawn_reaps_process_before_single_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "state-write-secret-marker"
    events: list[str] = []
    factory = FakeProcessFactory(blocked=True, events=events)
    staged = _staged(tmp_path)
    staged.events = events
    worker = _local(tmp_path, factory)
    original = worker_module._WorkerBase._atomic_state

    def fail_running_state(self: object, run_id: str, state: object) -> None:
        if state.state == "running":  # type: ignore[attr-defined]
            raise OSError(marker)
        original(self, run_id, state)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module._WorkerBase, "_atomic_state", fail_running_state)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert events.index("reaped:run") < events.index("cleanup")
    assert staged.cleanup_calls == 1
    assert worker._active == {}
    assert not (tmp_path / "runs/memory-run-1").exists()


@pytest.mark.asyncio
async def test_task_creation_failure_closes_coroutine_and_rolls_back_spawn(
    tmp_path: Path,
) -> None:
    marker = "task-create-secret-marker"
    events: list[str] = []
    factory = FakeProcessFactory(blocked=True, events=events)
    staged = _staged(tmp_path)
    staged.events = events
    worker = _local(tmp_path, factory)
    closed = False

    def fail_task_creation(coroutine: object) -> asyncio.Task[None]:
        nonlocal closed
        coroutine.close()  # type: ignore[attr-defined]
        closed = True
        raise RuntimeError(marker)

    worker._task_factory = fail_task_creation  # type: ignore[attr-defined]

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    assert closed is True
    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert events.index("reaped:run") < events.index("cleanup")
    assert staged.cleanup_calls == 1
    assert worker._active == {}
    assert not (tmp_path / "runs/memory-run-1").exists()


@pytest.mark.asyncio
async def test_oci_output_directory_failure_cleans_without_active_or_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "output-mkdir-secret-marker"
    real_mkdir = os.mkdir

    def fail_output(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if os.fspath(path) == "output" or os.fspath(path).endswith("/output"):
            raise OSError(marker)
        real_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    staged = _staged(tmp_path)
    factory = FakeProcessFactory()
    worker = _oci(tmp_path, factory)
    monkeypatch.setattr(os, "mkdir", fail_output)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert staged.cleanup_calls == 1
    assert worker._active == {}
    assert factory.calls == []
    assert not (tmp_path / "runs/memory-run-1").exists()


@pytest.mark.asyncio
async def test_stderr_is_drained_but_never_exposed_or_persisted(tmp_path: Path) -> None:
    marker = "stderr-private-marker"
    worker = _local(tmp_path, FakeProcessFactory(exit_code=1, stderr=marker.encode() * 1000))
    staged = _staged(tmp_path)
    await worker.start(
        run_id="memory-run-1", staged=staged, question="secret question", timeout_seconds=60
    )  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == "failed"
    result = await worker.result("memory-run-1")
    state = (tmp_path / "runs" / "memory-run-1" / "state.json").read_text()
    assert marker not in repr(result)
    assert marker not in state
    assert "secret question" not in state


@pytest.mark.asyncio
async def test_output_limit_and_symlink_output_fail_closed(tmp_path: Path) -> None:
    worker = _local(
        tmp_path,
        FakeProcessFactory(payload=b"x" * 17),
        max_output_bytes=16,
    )
    staged = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == "failed"
    assert (await worker.result("memory-run-1")).payload is None


@pytest.mark.asyncio
async def test_timeout_kills_process_marks_failed_and_cleans(tmp_path: Path) -> None:
    factory = FakeProcessFactory(blocked=True)
    staged = _staged(tmp_path)
    worker = _local(tmp_path, factory)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=0.001)  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == "failed"
    assert factory.processes[0].killed == 1
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_oci_timeout_uses_runtime_kill_then_reaps_run_before_cleanup(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    factory = FakeProcessFactory(blocked=True, stderr=b"run-secret", events=events)
    staged = _staged(tmp_path)
    staged.events = events
    worker = _oci(tmp_path, factory)

    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=0.001,
    )

    assert await _terminal(worker, "memory-run-1") == "failed"
    control_calls = [call[0] for call in factory.calls][1:]
    assert control_calls[:2] == [
        (
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{json .}}",
            "memory-run-1",
        ),
        ("/usr/bin/docker", "kill", "memory-run-1"),
    ]
    assert events.index("reaped:kill") < events.index("reaped:run") < events.index("cleanup")
    assert "run-secret" not in (tmp_path / "runs/memory-run-1/state.json").read_text()


@pytest.mark.asyncio
async def test_cancel_running_process_is_idempotent_and_preserves_terminal_result(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(blocked=True)
    staged = _staged(tmp_path)
    worker = _local(tmp_path, factory)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    await worker.cancel("memory-run-1")
    await worker.cancel("memory-run-1")
    assert await worker.status("memory-run-1") == "canceled"
    assert staged.cleanup_calls == 1
    assert factory.processes[0].terminated + factory.processes[0].killed >= 1


@pytest.mark.asyncio
async def test_oci_kill_failure_reaps_run_cleans_once_and_raises_redacted_error(
    tmp_path: Path,
) -> None:
    marker = "runtime-kill-secret-marker"
    events: list[str] = []
    factory = FakeProcessFactory(
        blocked=True,
        kill_exit=1,
        kill_stderr=marker.encode(),
        events=events,
    )
    staged = _staged(tmp_path)
    staged.events = events
    worker = _oci(tmp_path, factory)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.cancel("memory-run-1")

    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert events.index("reaped:run") < events.index("cleanup")
    assert staged.cleanup_calls == 1
    assert await worker.status("memory-run-1") == "lost"


@pytest.mark.asyncio
async def test_unconfirmed_oci_termination_retains_authority_and_cleanup_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "termination-control-secret-marker"
    factory = FakeProcessFactory(
        blocked=True,
        kill_exit=1,
        kill_stderr=marker.encode(),
        rm_exit=1,
    )
    staged = _staged(tmp_path)
    worker = _oci(tmp_path, factory)
    monkeypatch.setattr(worker_module, "_CONTROL_TIMEOUT_SECONDS", 0.01)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.cancel("memory-run-1")

    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert staged.cleanup_calls == 0
    assert factory.processes[0].returncode is None
    assert "memory-run-1" in worker._active

    factory.kill_exit = 0
    factory.rm_exit = 0
    await worker.cancel("memory-run-1")

    assert factory.processes[0].returncode is not None
    assert staged.cleanup_calls == 1
    assert "memory-run-1" not in worker._active
    assert await worker.status("memory-run-1") == "canceled"


@pytest.mark.asyncio
async def test_unconfirmed_spawn_rollback_retains_process_authority_until_shutdown_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeProcessFactory(blocked=True, kill_exit=1, rm_exit=1)
    staged = _staged(tmp_path)
    worker = _oci(tmp_path, factory)

    def fail_state(*_args: object, **_kwargs: object) -> None:
        raise OSError("state-private-marker")

    monkeypatch.setattr(worker, "_atomic_state", fail_state)
    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(
            run_id="memory-run-1",
            staged=staged,  # type: ignore[arg-type]
            question=None,
            timeout_seconds=60,
        )

    assert factory.processes[0].returncode is None
    assert staged.cleanup_calls == 0
    assert "memory-run-1" in worker._active

    factory.kill_exit = 0
    await worker.shutdown()

    assert factory.processes[0].returncode is not None
    assert staged.cleanup_calls == 1
    assert worker._active == {}


@pytest.mark.asyncio
async def test_monitor_task_cancellation_terminates_local_process_before_cleanup(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    factory = FakeProcessFactory(blocked=True, events=events)
    staged = _staged(tmp_path)
    staged.events = events
    worker = _local(tmp_path, factory)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    task = worker._active["memory-run-1"].task
    assert task is not None

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert factory.processes[0].returncode is not None
    assert events.index("reaped:run") < events.index("cleanup")
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_does_not_wait_for_non_eof_pipes(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(blocked=True, pipe_never_eof=True)
    staged = _staged(tmp_path)
    worker = _local(tmp_path, factory)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    await asyncio.wait_for(worker.shutdown(), timeout=0.2)
    await worker.shutdown()

    assert factory.processes[0].returncode is not None
    assert staged.cleanup_calls == 1
    assert worker._active == {}


@pytest.mark.asyncio
async def test_canceling_completed_oci_run_is_idempotent_and_preserves_result(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(payload=b"verified")
    worker = _oci(tmp_path, factory)
    staged = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == "completed"
    kill_count = len([call for call in factory.calls if call[0][1] == "kill"])

    await worker.cancel("memory-run-1")
    await worker.cancel("memory-run-1")

    assert len([call for call in factory.calls if call[0][1] == "kill"]) == kill_count
    assert (await worker.result("memory-run-1")).payload == b"verified"


@pytest.mark.asyncio
async def test_terminal_state_and_verified_payload_replay_in_a_new_local_instance(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(payload=b"verified payload")
    staged = _staged(tmp_path)
    first = _local(tmp_path, factory)
    await first.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    assert await _terminal(first, "memory-run-1") == "completed"

    replay = _local(tmp_path, FakeProcessFactory())
    assert await replay.status("memory-run-1") == "completed"
    assert await replay.result("memory-run-1") == MemoryWorkerResult(
        exit_code=0,
        payload=b"verified payload",
    )


@pytest.mark.asyncio
async def test_restarted_local_worker_marks_unverifiable_running_state_lost(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state()))
    worker = _local(tmp_path, FakeProcessFactory())
    assert await worker.status("memory-run-1") == "lost"


@pytest.mark.asyncio
async def test_duplicate_and_unsafe_run_ids_fail_without_deleting_first_owner(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(blocked=True)
    worker = _local(tmp_path, factory)
    first = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=first, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    second_dir = tmp_path / "staged-two"
    second_dir.mkdir()
    second = FakeStaged(second_dir)
    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(run_id="memory-run-1", staged=second, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    assert (tmp_path / "runs" / "memory-run-1").is_dir()
    assert second.cleanup_calls == 1
    for unsafe in ("..", "../escape", "/absolute", "slash/value", "nul\x00", "x" * 129):
        staged = FakeStaged(second_dir)
        with pytest.raises(AndroidMemoryWorkerError):
            await worker.start(run_id=unsafe, staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
        assert staged.cleanup_calls == 1
    await worker.cancel("memory-run-1")


@pytest.mark.asyncio
async def test_symlinked_run_root_fails_closed_and_cleans_staged_input(tmp_path: Path) -> None:
    real_root = tmp_path / "redirected-runs"
    real_root.mkdir()
    run_root = tmp_path / "runs"
    run_root.symlink_to(real_root, target_is_directory=True)
    worker = _local(
        tmp_path,
        FakeProcessFactory(),
        run_root=run_root,
    )
    staged = _staged(tmp_path)

    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(
            run_id="memory-run-1",
            staged=staged,  # type: ignore[arg-type]
            question=None,
            timeout_seconds=60,
        )

    assert staged.cleanup_calls == 1
    assert list(real_root.iterdir()) == []


@pytest.mark.parametrize(
    "change",
    [
        {"image_reference": "registry.example/android-memory:latest"},
        {"image_reference": "registry.example/android-memory@sha256:" + "A" * 64},
        {"image_reference": "--env=PRIVATE@sha256:" + "a" * 64},
        {"image_reference": "registry.example/android=memory@sha256:" + "a" * 64},
        {"cpu_limit": float("inf")},
        {"cpu_limit": True},
        {"pids_limit": True},
        {"memory_bytes": 0},
        {"tmpfs_bytes": 0},
        {"container_runtime": Path("docker")},
    ],
)
def test_oci_constructor_rejects_unpinned_or_invalid_runtime_configuration(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _oci(tmp_path, FakeProcessFactory(), **change)


def test_oci_constructor_accepts_registry_path_and_localhost_port(tmp_path: Path) -> None:
    digest = "a" * 64
    assert (
        _oci(
            tmp_path,
            FakeProcessFactory(),
            image_reference=f"registry.example.com/team/android-memory@sha256:{digest}",
        ).isolation
        == "oci"
    )
    assert (
        _oci(
            tmp_path,
            FakeProcessFactory(),
            image_reference=f"localhost:5000/team/android-memory@sha256:{digest}",
        ).isolation
        == "oci"
    )


@pytest.mark.asyncio
async def test_oci_worker_uses_exact_hardened_no_shell_argv(tmp_path: Path) -> None:
    factory = FakeProcessFactory(exit_code=0)
    worker = _oci(tmp_path, factory)
    staged = _staged(tmp_path)
    question = "native growth; $(touch /tmp/forbidden)"
    await worker.start(run_id="memory-run-1", staged=staged, question=question, timeout_seconds=60)  # type: ignore[arg-type]
    assert await _terminal(worker, "memory-run-1") == "completed"

    argv, kwargs = factory.calls[0]
    ownership_token = factory.owner
    assert re.fullmatch(r"[a-f0-9]{64}", ownership_token)
    output_dir = tmp_path / "runs" / "memory-run-1" / "output"
    assert argv == (
        "/usr/bin/docker",
        "run",
        "--rm",
        "--name",
        "memory-run-1",
        "--label",
        f"{OWNER_LABEL}={ownership_token}",
        "--label",
        f"{RUN_LABEL}=memory-run-1",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "--memory",
        str(8 * 1024**3),
        "--cpus",
        "4.0",
        "--mount",
        f"type=bind,src={staged.input_dir},dst=/work/input,readonly",
        "--mount",
        f"type=bind,src={output_dir},dst=/work/output",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={1024**3}",
        IMAGE,
        "--dump-dir",
        "/work/input",
        "--question",
        question,
        "--format",
        "json",
        "--strict",
        "--output",
        "/work/output/context.json",
    )
    assert "shell" not in kwargs and "env" not in kwargs
    assert "--privileged" not in argv and "host" not in argv


@pytest.mark.asyncio
async def test_oci_output_directory_grants_nonowner_write_without_read(tmp_path: Path) -> None:
    factory = FakeProcessFactory(blocked=True)
    worker = _oci(tmp_path, factory)
    staged = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    try:
        output = tmp_path / "runs/memory-run-1/output"
        mode = stat.S_IMODE(os.lstat(output).st_mode)
        assert mode == 0o733
        assert mode & stat.S_IWOTH
        assert mode & stat.S_IXOTH
        assert not mode & stat.S_IROTH
        assert stat.S_IMODE(os.lstat(output.parent).st_mode) == 0o700
    finally:
        await worker.cancel("memory-run-1")


@pytest.mark.asyncio
async def test_oci_running_state_and_argv_bind_random_container_ownership(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(blocked=True)
    worker = _oci(tmp_path, factory)
    staged = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    try:
        argv = factory.calls[0][0]
        labels = [argv[index + 1] for index, value in enumerate(argv) if value == "--label"]
        owner_labels = [label for label in labels if label.startswith(f"{OWNER_LABEL}=")]
        assert len(owner_labels) == 1
        owner_label = owner_labels[0]
        token = owner_label.split("=", 1)[1]
        assert re.fullmatch(r"[a-f0-9]{64}", token)
        assert f"{RUN_LABEL}=memory-run-1" in labels
        assert json.loads((tmp_path / "runs/memory-run-1/state.json").read_text()) == (
            _running_state(owner=token, image=IMAGE)
        )
    finally:
        await worker.cancel("memory-run-1")


@pytest.mark.asyncio
async def test_oci_rejects_mount_option_injection_and_cleans_staged_input(tmp_path: Path) -> None:
    unsafe = tmp_path / "input,readonly=false"
    unsafe.mkdir()
    staged = FakeStaged(unsafe)
    worker = _oci(tmp_path, FakeProcessFactory())
    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_oci_cancel_invokes_runtime_kill_and_awaits_original(tmp_path: Path) -> None:
    factory = FakeProcessFactory(blocked=True)
    worker = _oci(tmp_path, factory)
    staged = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
    await worker.cancel("memory-run-1")
    assert ("/usr/bin/docker", "kill", "memory-run-1") in [call[0] for call in factory.calls]
    assert await worker.status("memory-run-1") == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspect_exit", "inspect_stdout", "inspect_stderr", "expected"),
    [
        (0, _inspect_json(running=True), b"", "running"),
        (0, _inspect_json(running=False), b"", "lost"),
        (1, b"", b"Error: No such object", "lost"),
    ],
)
async def test_restarted_oci_worker_requires_strict_bounded_running_inspect(
    tmp_path: Path,
    inspect_exit: int,
    inspect_stdout: bytes,
    inspect_stderr: bytes,
    expected: str,
) -> None:
    run_dir = tmp_path / "runs" / "memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state(owner=OWNER, image=IMAGE)))
    factory = FakeProcessFactory(
        inspect_exit=inspect_exit,
        inspect_stdout=inspect_stdout,
        inspect_stderr=inspect_stderr,
    )
    worker = _oci(tmp_path, factory)
    assert await worker.status("memory-run-1") == expected
    assert factory.calls[0][0] == (
        "/usr/bin/docker",
        "inspect",
        "--format",
        "{{json .}}",
        "memory-run-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspect_exit", "inspect_stdout"),
    [
        (0, b"garbage"),
        (0, b"x" * (64 * 1024 + 1)),
        (1, b""),
    ],
)
async def test_restarted_oci_inspect_unavailable_is_retryable_and_preserves_authority(
    tmp_path: Path,
    inspect_exit: int,
    inspect_stdout: bytes,
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    state_path.write_text(json.dumps(_running_state(owner=OWNER, image=IMAGE)))
    marker = "inspect-daemon-secret-marker"
    worker = _oci(
        tmp_path,
        FakeProcessFactory(
            inspect_exit=inspect_exit,
            inspect_stdout=inspect_stdout,
            inspect_stderr=marker.encode(),
        ),
    )

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")

    assert caught.value.retryable is True
    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert json.loads(state_path.read_text()) == _running_state(owner=OWNER, image=IMAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inspect_payload",
    [
        _inspect_json(owner="c" * 64),
        _inspect_json(image="registry.example/foreign@sha256:" + "d" * 64),
        _inspect_json(run_id="foreign-run"),
    ],
)
async def test_foreign_same_name_container_is_never_killed(
    tmp_path: Path,
    inspect_payload: bytes,
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    expected_state = _running_state(owner=OWNER, image=IMAGE)
    state_path.write_text(json.dumps(expected_state))
    factory = FakeProcessFactory(inspect_exit=0, inspect_stdout=inspect_payload)
    worker = _oci(tmp_path, factory)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.cancel("memory-run-1")

    assert caught.value.stable_code in {"worker_conflict", "worker_unavailable"}
    assert [call for call in factory.calls if call[0][1] == "kill"] == []
    assert json.loads(state_path.read_text()) == expected_state


@pytest.mark.asyncio
async def test_temporary_inspect_failure_can_recover_without_losing_authority(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    expected_state = _running_state(owner=OWNER, image=IMAGE)
    state_path.write_text(json.dumps(expected_state))
    marker = "temporary-inspect-secret-marker"
    factory = FakeProcessFactory(
        inspect_exit=[1, 0],
        inspect_stdout=[b"", _inspect_json()],
        inspect_stderr=[marker.encode(), b""],
    )
    worker = _oci(tmp_path, factory)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")
    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert json.loads(state_path.read_text()) == expected_state

    assert await worker.status("memory-run-1") == "running"


@pytest.mark.asyncio
async def test_recovered_oci_status_reinspects_live_container_until_it_stops(
    tmp_path: Path,
) -> None:
    run_id = "memory-run-1"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state(owner=OWNER, image=IMAGE)))
    marker = "inspect-recheck-secret-marker"
    factory = FakeProcessFactory(
        inspect_exit=0,
        inspect_stdout=[_inspect_json(running=True), _inspect_json(running=False)],
        inspect_stderr=marker.encode(),
    )
    worker = _oci(tmp_path, factory)

    assert await worker.status(run_id) == "running"
    assert await worker.status(run_id) == "lost"

    inspect_argv = (
        "/usr/bin/docker",
        "inspect",
        "--format",
        "{{json .}}",
        run_id,
    )
    assert [call[0] for call in factory.calls] == [inspect_argv, inspect_argv]
    assert json.loads((run_dir / "state.json").read_text())["state"] == "lost"
    assert run_id not in worker._recovered
    assert marker not in (run_dir / "state.json").read_text()


@pytest.mark.asyncio
async def test_recovered_live_oci_run_can_be_canceled_and_persisted_idempotently(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state(owner=OWNER, image=IMAGE)))
    factory = FakeProcessFactory(
        inspect_exit=0,
        inspect_stdout=[
            _inspect_json(running=True),
            _inspect_json(running=True),
            _inspect_json(running=False),
        ],
    )
    worker = _oci(tmp_path, factory)
    assert await worker.status("memory-run-1") == "running"

    await worker.cancel("memory-run-1")
    await worker.cancel("memory-run-1")

    assert [call[0] for call in factory.calls] == [
        (
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{json .}}",
            "memory-run-1",
        ),
        (
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{json .}}",
            "memory-run-1",
        ),
        ("/usr/bin/docker", "kill", "memory-run-1"),
        (
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{json .}}",
            "memory-run-1",
        ),
    ]
    assert await worker.status("memory-run-1") == "canceled"


@pytest.mark.asyncio
async def test_concurrent_recovered_cancel_issues_one_runtime_kill(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state(owner=OWNER, image=IMAGE)))
    factory = FakeProcessFactory(
        inspect_exit=0,
        inspect_stdout=[
            _inspect_json(running=True),
            _inspect_json(running=True),
            _inspect_json(running=False),
        ],
    )
    worker = _oci(tmp_path, factory)
    assert await worker.status("memory-run-1") == "running"

    await asyncio.gather(
        worker.cancel("memory-run-1"),
        worker.cancel("memory-run-1"),
    )

    assert len([call for call in factory.calls if call[0][1] == "kill"]) == 1
    assert await worker.status("memory-run-1") == "canceled"


@pytest.mark.asyncio
async def test_two_worker_instances_issue_one_kill_and_converge_on_canceled(
    tmp_path: Path,
) -> None:
    class SlowKillFactory(FakeProcessFactory):
        async def __call__(self, *argv: object, **kwargs: object) -> FakeProcess:
            if len(argv) >= 2 and argv[1] == "kill":
                await asyncio.sleep(0.01)
            return await super().__call__(*argv, **kwargs)

    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state(owner=OWNER, image=IMAGE)))
    factory = SlowKillFactory()
    first = _oci(tmp_path, factory)
    second = _oci(tmp_path, factory)
    assert await first.status("memory-run-1") == "running"
    assert await second.status("memory-run-1") == "running"

    await asyncio.gather(
        first.cancel("memory-run-1"),
        second.cancel("memory-run-1"),
    )

    assert len([call for call in factory.calls if call[0][1] == "kill"]) == 1
    assert await first.status("memory-run-1") == "canceled"
    assert await second.status("memory-run-1") == "canceled"


@pytest.mark.asyncio
async def test_active_and_recovered_workers_serialize_cancel_to_one_kill(tmp_path: Path) -> None:
    factory = FakeProcessFactory(blocked=True)
    active_worker = _oci(tmp_path, factory)
    staged = _staged(tmp_path)
    await active_worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    recovered_worker = _oci(tmp_path, factory)
    assert await recovered_worker.status("memory-run-1") == "running"

    await asyncio.gather(
        active_worker.cancel("memory-run-1"),
        recovered_worker.cancel("memory-run-1"),
    )

    assert len([call for call in factory.calls if call[0][1] == "kill"]) == 1
    assert await active_worker.status("memory-run-1") == "canceled"
    assert await recovered_worker.status("memory-run-1") == "canceled"
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_cross_instance_lock_files_are_regular_nofollow_and_nonsecret(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"schema_version": "1.0", "state": "lost"}))
    worker = _local(tmp_path, FakeProcessFactory())

    assert await worker.status("memory-run-1") == "lost"

    lock_dir = tmp_path / "runs/.locks"
    assert stat.S_IMODE(os.lstat(lock_dir).st_mode) == 0o700
    lock_files = list(lock_dir.iterdir())
    assert len(lock_files) == 1
    assert stat.S_ISREG(os.lstat(lock_files[0]).st_mode)
    assert stat.S_IMODE(os.lstat(lock_files[0]).st_mode) == 0o600
    assert "memory-run-1" not in lock_files[0].name


def test_corrupt_or_symlinked_state_is_never_trusted(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "memory-run-1"
    run_dir.mkdir(parents=True)
    marker = tmp_path / "marker"
    marker.write_text("private-marker")
    (run_dir / "state.json").symlink_to(marker)
    worker = _local(tmp_path, FakeProcessFactory())
    with pytest.raises(AndroidMemoryWorkerError) as caught:
        asyncio.run(worker.status("memory-run-1"))
    assert "private-marker" not in repr(caught.value)


def test_replayed_output_symlink_is_never_followed(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    marker = b"output-symlink-secret-marker"
    target = tmp_path / "attacker-output"
    target.write_bytes(marker)
    (run_dir / "context.json").symlink_to(target)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "state": "completed",
                "exit_code": 0,
                "output_size": len(marker),
                "output_sha256": hashlib.sha256(marker).hexdigest(),
            }
        )
    )
    worker = _local(tmp_path, FakeProcessFactory())

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        asyncio.run(worker.result("memory-run-1"))

    assert marker.decode() not in f"{caught.value!s} {caught.value!r}"


def test_state_files_are_atomic_and_leave_no_temporary_files_after_completion(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        worker = _local(tmp_path, FakeProcessFactory())
        staged = _staged(tmp_path)
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]
        assert await _terminal(worker, "memory-run-1") == "completed"

    asyncio.run(exercise())
    run_dir = tmp_path / "runs" / "memory-run-1"
    assert (run_dir / "state.json").is_file()
    assert [path for path in run_dir.iterdir() if ".tmp" in path.name] == []


@pytest.mark.asyncio
async def test_stale_fixed_temporary_state_does_not_block_running_recovery(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(_running_state()))
    (run_dir / ".state.tmp").write_text("stale-private-marker")

    worker = _local(tmp_path, FakeProcessFactory())

    assert await worker.status("memory-run-1") == "lost"
    assert json.loads((run_dir / "state.json").read_text())["state"] == "lost"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["write", "replace", "fsync"])
async def test_atomic_state_faults_are_redacted_clean_their_temp_and_keep_state_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    marker = f"/private/state-{fault}-secret-marker"
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    state_path.write_text(json.dumps(_running_state()))
    worker = _local(tmp_path, FakeProcessFactory())

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError(marker)

    monkeypatch.setattr(os, fault, fail)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")

    rendered = f"{caught.value!s} {caught.value!r}"
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert json.loads(state_path.read_text())["state"] in {"running", "lost"}
    assert [path for path in run_dir.iterdir() if ".tmp" in path.name] == []


@pytest.mark.asyncio
async def test_bound_run_root_rejects_parent_rebinding_without_reading_attacker_state(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    real_run = run_root / "memory-run-1"
    real_run.mkdir(parents=True)
    (real_run / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "failed", "exit_code": 1})
    )
    worker = _local(tmp_path, FakeProcessFactory())
    original_root = tmp_path / "original-runs"
    run_root.rename(original_root)
    attacker_run = run_root / "memory-run-1"
    attacker_run.mkdir(parents=True)
    marker = "attacker-state-secret-marker"
    (attacker_run / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "completed", "exit_code": 0})
    )
    (attacker_run / "context.json").write_text(marker)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")

    assert marker not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.asyncio
async def test_bound_worker_rejects_run_directory_symlink_without_reading_state(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run_root.mkdir()
    worker = _local(tmp_path, FakeProcessFactory())
    attacker = tmp_path / "attacker-run"
    attacker.mkdir()
    marker = "run-symlink-secret-marker"
    (attacker / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "failed", "exit_code": 1})
    )
    (attacker / "context.json").write_text(marker)
    (run_root / "memory-run-1").symlink_to(attacker, target_is_directory=True)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")

    assert marker not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.asyncio
async def test_bound_worker_rejects_run_directory_inode_replacement(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    run_dir = run_root / "memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "failed", "exit_code": 1})
    )
    worker = _local(tmp_path, FakeProcessFactory())
    assert await worker.status("memory-run-1") == "failed"
    run_dir.rename(run_root / "original-memory-run-1")
    run_dir.mkdir()
    marker = "replacement-run-secret-marker"
    (run_dir / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "completed", "exit_code": 0})
    )
    (run_dir / "context.json").write_text(marker)

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")

    assert marker not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.asyncio
async def test_start_rechecks_bound_root_after_validation_before_writing(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run_root.mkdir()
    attacker_root = tmp_path / "attacker-runs"

    async def swap_root(_root: Path) -> str:
        run_root.rename(tmp_path / "original-runs")
        attacker_root.mkdir()
        attacker_root.rename(run_root)
        return COMMIT

    staged = _staged(tmp_path)
    worker = _local(
        tmp_path,
        FakeProcessFactory(),
        commit_resolver=swap_root,
    )

    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    assert staged.cleanup_calls == 1
    assert list(run_root.iterdir()) == []


@pytest.mark.asyncio
async def test_spawn_rollback_never_deletes_replacement_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "replacement-directory-marker"
    worker = _local(tmp_path, FakeProcessFactory(blocked=True))
    staged = _staged(tmp_path)
    original_atomic = worker_module._WorkerBase._atomic_state

    def replace_then_fail(self: object, run_id: str, state: object) -> None:
        if state.state == "running":  # type: ignore[attr-defined]
            run_dir = tmp_path / "runs" / run_id
            run_dir.rename(tmp_path / "owned-orphan")
            run_dir.mkdir()
            (run_dir / "marker").write_text(marker)
            raise OSError("rollback-private-marker")
        original_atomic(self, run_id, state)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module._WorkerBase, "_atomic_state", replace_then_fail)

    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    replacement = tmp_path / "runs/memory-run-1"
    assert replacement.is_dir()
    assert (replacement / "marker").read_text() == marker
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_state"),
    [(0, "completed"), (1, "failed")],
)
async def test_terminal_local_runs_evict_active_and_replay_from_disk(
    tmp_path: Path,
    exit_code: int,
    expected_state: str,
) -> None:
    worker = _local(tmp_path, FakeProcessFactory(exit_code=exit_code))
    staged = _staged(tmp_path)
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    assert await _terminal(worker, "memory-run-1") == expected_state
    assert worker._active == {}
    assert await worker.status("memory-run-1") == expected_state
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_canceled_run_and_cleanup_failure_do_not_retain_active_objects(
    tmp_path: Path,
) -> None:
    worker = _local(tmp_path, FakeProcessFactory(blocked=True))
    staged = _staged(tmp_path)
    staged.cleanup_error = RuntimeError("cleanup-private-marker")
    await worker.start(run_id="memory-run-1", staged=staged, question=None, timeout_seconds=60)  # type: ignore[arg-type]

    await worker.cancel("memory-run-1")

    assert worker._active == {}
    assert await worker.status("memory-run-1") == "canceled"
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state",
    [
        {"schema_version": "1.0", "state": "lost", "extra": "strict-state-secret"},
        {"schema_version": "1.0", "state": "running", "exit_code": None},
        {
            "schema_version": "1.0",
            "state": "completed",
            "exit_code": 1,
            "output_size": 0,
            "output_sha256": "a" * 64,
        },
        {
            "schema_version": "1.0",
            "state": "completed",
            "exit_code": True,
            "output_size": 0,
            "output_sha256": "a" * 64,
        },
        {
            "schema_version": "1.0",
            "state": "completed",
            "exit_code": 0,
            "output_size": -1,
            "output_sha256": "a" * 64,
        },
        {
            "schema_version": "1.0",
            "state": "completed",
            "exit_code": 0,
            "output_size": 0,
            "output_sha256": "A" * 64,
        },
        {
            "schema_version": "1.0",
            "state": "failed",
            "exit_code": 1,
            "output_size": 0,
        },
        {"schema_version": "1.0", "state": "canceled", "exit_code": -9},
        {"schema_version": "1.0", "state": "lost", "ownership_token": OWNER},
    ],
)
async def test_persisted_state_schema_rejects_extra_or_impossible_combinations(
    tmp_path: Path,
    invalid_state: dict[str, object],
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    marker = "strict-state-secret"
    (run_dir / "state.json").write_text(json.dumps(invalid_state))
    worker = _local(tmp_path, FakeProcessFactory())

    with pytest.raises(AndroidMemoryWorkerError) as caught:
        await worker.status("memory-run-1")

    assert marker not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_state"),
    [(0, "completed"), (1, "failed"), (2, "completed")],
)
async def test_oci_cli_exit_confirms_and_terminates_owned_live_container_before_finalize(
    tmp_path: Path,
    exit_code: int,
    expected_state: str,
) -> None:
    events: list[str] = []
    factory = FakeProcessFactory(exit_code=exit_code, events=events)
    staged = _staged(tmp_path)
    staged.events = events
    worker = _oci(tmp_path, factory)

    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    assert await _terminal(worker, "memory-run-1") == expected_state

    commands = [call[0][1] for call in factory.calls]
    assert commands[:4] == ["run", "inspect", "kill", "inspect"]
    assert events.index("reaped:kill") < events.index("cleanup")
    assert staged.cleanup_calls == 1
    assert worker._active == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspect_stdout", "inspect_exit", "inspect_stderr"),
    [
        (b"", 1, b"runtime temporarily unavailable"),
        (_inspect_json(owner="c" * 64), 0, b""),
    ],
)
async def test_oci_cli_exit_without_owned_container_confirmation_retains_authority(
    tmp_path: Path,
    inspect_stdout: bytes,
    inspect_exit: int,
    inspect_stderr: bytes,
) -> None:
    factory = FakeProcessFactory(
        inspect_exit=inspect_exit,
        inspect_stdout=inspect_stdout,
        inspect_stderr=inspect_stderr,
    )
    staged = _staged(tmp_path)
    worker = _oci(tmp_path, factory)
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    active = worker._active["memory-run-1"]
    assert active.task is not None
    await active.task

    assert active.state == "running"
    assert getattr(active, "pending_terminal", None) is not None
    assert staged.cleanup_calls == 0
    assert "memory-run-1" in worker._active
    assert json.loads((tmp_path / "runs/memory-run-1/state.json").read_text())["state"] == (
        "running"
    )


@pytest.mark.asyncio
async def test_oci_cli_exit_live_container_failed_termination_retries_via_status(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(kill_exit=1, rm_exit=1)
    staged = _staged(tmp_path)
    worker = _oci(tmp_path, factory)
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    active = worker._active["memory-run-1"]
    assert active.task is not None
    await active.task

    assert active.state == "running"
    assert staged.cleanup_calls == 0
    assert "memory-run-1" in worker._active

    factory.kill_exit = 0
    factory.rm_exit = 0
    assert await worker.status("memory-run-1") == "completed"
    assert staged.cleanup_calls == 1
    assert worker._active == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_kind",
    ["stopped", "absent"],
)
async def test_oci_cli_exit_accepts_initial_stopped_or_absent_confirmation_once(
    tmp_path: Path,
    initial_kind: str,
) -> None:
    class InitiallyGoneFactory(FakeProcessFactory):
        async def __call__(self, *argv: object, **kwargs: object) -> FakeProcess:
            process = await super().__call__(*argv, **kwargs)
            if len(argv) >= 2 and argv[1] == "run":
                self.container_running = False
                self.container_exists = initial_kind != "absent"
            return process

    factory = InitiallyGoneFactory()
    staged = _staged(tmp_path)
    worker = _oci(tmp_path, factory)
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )

    assert await _terminal(worker, "memory-run-1") == "completed"
    assert len([call for call in factory.calls if call[0][1] == "inspect"]) == 1
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
async def test_oci_cli_wait_exception_confirms_container_before_lost_cleanup(
    tmp_path: Path,
) -> None:
    factory = FakeProcessFactory(wait_failure=RuntimeError("cli-private-marker"))
    staged = _staged(tmp_path)
    worker = _oci(tmp_path, factory)
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )

    assert await _terminal(worker, "memory-run-1") == "lost"
    assert [call[0][1] for call in factory.calls][:4] == [
        "run",
        "inspect",
        "kill",
        "inspect",
    ]
    assert staged.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_marker", [False, True])
async def test_rollback_top_level_recheck_never_deletes_replacement_after_verified_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_marker: bool,
) -> None:
    run_root = tmp_path / "runs"
    run_dir = run_root / "memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"schema_version": "1.0", "state": "lost"}))
    worker = _local(tmp_path, FakeProcessFactory())
    assert await worker.status("memory-run-1") == "lost"
    real_stat = os.stat
    swapped = False

    def swap_before_final_stat(
        path: object,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal swapped
        if path == "memory-run-1" and dir_fd is not None and not swapped:
            swapped = True
            run_dir.rename(tmp_path / "owned-orphan")
            run_dir.mkdir()
            if with_marker:
                (run_dir / "marker").write_text("replacement-marker")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", swap_before_final_stat)
    await worker._rollback_run("memory-run-1")

    assert run_dir.is_dir()
    if with_marker:
        assert (run_dir / "marker").read_text() == "replacement-marker"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_marker", [False, True])
async def test_create_open_failure_rollback_never_deletes_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_marker: bool,
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    real_open = os.open
    swapped = False

    def swap_and_fail_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "memory-run-1" and dir_fd is not None and not swapped:
            swapped = True
            run_dir.rename(tmp_path / "created-orphan")
            run_dir.mkdir()
            if with_marker:
                (run_dir / "marker").write_text("replacement-marker")
            raise OSError("open-race-private-marker")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_and_fail_open)
    staged = _staged(tmp_path)
    worker = _local(tmp_path, FakeProcessFactory())
    with pytest.raises(AndroidMemoryWorkerError):
        await worker.start(
            run_id="memory-run-1",
            staged=staged,  # type: ignore[arg-type]
            question=None,
            timeout_seconds=60,
        )

    assert run_dir.is_dir()
    if with_marker:
        assert (run_dir / "marker").read_text() == "replacement-marker"


@pytest.mark.asyncio
async def test_natural_real_process_terminal_write_failure_retries_exact_pending_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def real_process_factory(*argv: object, **_kwargs: object) -> asyncio.subprocess.Process:
        rendered = tuple(str(value) for value in argv)
        output_path = Path(rendered[rendered.index("--output") + 1])
        output_path.write_bytes(b'{"context_type":"android-memory-ai-context"}')
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    worker = _local(tmp_path, real_process_factory)  # type: ignore[arg-type]
    staged = _staged(tmp_path)
    original_atomic = worker._atomic_state
    failed_once = False

    def fail_first_terminal(run_id: str, state: object) -> None:
        nonlocal failed_once
        if state.state != "running" and not failed_once:  # type: ignore[attr-defined]
            failed_once = True
            raise OSError("state-write-private-marker")
        original_atomic(run_id, state)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "_atomic_state", fail_first_terminal)
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    active = worker._active["memory-run-1"]
    assert active.task is not None
    await active.task

    assert active.state == "running"
    assert getattr(active, "process_confirmed", False) is True
    pending = getattr(active, "pending_terminal", None)
    assert pending is not None and pending.state == "completed"
    assert staged.cleanup_calls == 0
    assert json.loads((tmp_path / "runs/memory-run-1/state.json").read_text())["state"] == (
        "running"
    )

    await worker.shutdown()

    assert await worker.status("memory-run-1") == "completed"
    assert (await worker.result("memory-run-1")).payload is not None
    assert staged.cleanup_calls == 1
    assert worker._active == {}


@pytest.mark.asyncio
async def test_cancel_during_terminal_cleanup_waits_then_pops_and_repropagates(
    tmp_path: Path,
) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class BlockingStaged(FakeStaged):
        async def cleanup(self) -> None:
            self.cleanup_calls += 1
            cleanup_started.set()
            await cleanup_release.wait()

    input_dir = tmp_path / "staged-input"
    input_dir.mkdir()
    staged = BlockingStaged(input_dir)
    worker = _local(tmp_path, FakeProcessFactory())
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    active = worker._active["memory-run-1"]
    assert active.task is not None
    await cleanup_started.wait()

    active.task.cancel()
    cleanup_release.set()
    outcome = (await asyncio.gather(active.task, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert staged.cleanup_calls == 1
    assert worker._active == {}
    assert await worker.status("memory-run-1") == "completed"


@pytest.mark.asyncio
async def test_old_terminal_task_never_pops_replacement_active_owner(tmp_path: Path) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class BlockingStaged(FakeStaged):
        async def cleanup(self) -> None:
            self.cleanup_calls += 1
            cleanup_started.set()
            await cleanup_release.wait()

    input_dir = tmp_path / "staged-input"
    input_dir.mkdir()
    staged = BlockingStaged(input_dir)
    worker = _local(tmp_path, FakeProcessFactory())
    await worker.start(
        run_id="memory-run-1",
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=60,
    )
    old_active = worker._active["memory-run-1"]
    assert old_active.task is not None
    await cleanup_started.wait()
    replacement_owner = object()
    worker._active["memory-run-1"] = replacement_owner  # type: ignore[assignment]

    cleanup_release.set()
    await old_active.task

    assert worker._active["memory-run-1"] is replacement_owner


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [-255, 255])
async def test_failed_state_accepts_bounded_worker_exit_codes(
    tmp_path: Path,
    exit_code: int,
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "failed", "exit_code": exit_code})
    )
    assert await _local(tmp_path, FakeProcessFactory()).status("memory-run-1") == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [-256, 256])
async def test_failed_state_rejects_out_of_range_exit_codes(
    tmp_path: Path,
    exit_code: int,
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps({"schema_version": "1.0", "state": "failed", "exit_code": exit_code})
    )
    with pytest.raises(AndroidMemoryWorkerError):
        await _local(tmp_path, FakeProcessFactory()).status("memory-run-1")


@pytest.mark.asyncio
async def test_local_process_lookup_error_confirms_already_exited_process(tmp_path: Path) -> None:
    process = FakeProcess(exit_code=0)

    def already_gone() -> None:
        raise ProcessLookupError

    process.kill = already_gone  # type: ignore[method-assign]
    result = await _local(tmp_path, FakeProcessFactory())._terminate_backend(
        "memory-run-1",
        process,
    )

    assert result.confirmed is True
    assert result.exit_code == 0
