from __future__ import annotations

import asyncio
import os
import re
import signal
import shutil
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

AdbState = Literal["device", "unauthorized", "offline"]
_SERIAL_PATTERN = re.compile(r"^[!-~]{1,255}$")
_PROPERTY_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_MAXIMUM_DEVICES = 32
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_OUTPUT_LIMIT = 256 * 1024
_ALLOWED_SHELL_COMMANDS = {"getprop", "dumpsys", "df", "which"}


class AdbError(RuntimeError):
    def __init__(self, message: str = "ADB operation failed") -> None:
        super().__init__(message)


class AdbUnavailable(AdbError):
    def __init__(self) -> None:
        super().__init__("ADB is unavailable")


class AdbCommandFailed(AdbError):
    pass


class AdbCommandTimedOut(AdbError):
    pass


class AdbOutputTooLarge(AdbError):
    pass


class AdbProtocolError(AdbError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    async def __call__(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
    ) -> ProcessResult: ...


def process_group_options(platform_name: str | None = None) -> dict[str, bool | int]:
    current = sys.platform if platform_name is None else platform_name
    if current == "win32":
        return {"creationflags": 0x00000200}
    return {"start_new_session": True}


async def run_process(
    argv: list[str],
    *,
    timeout_seconds: float,
    maximum_output_bytes: int,
) -> ProcessResult:
    if (
        not argv
        or timeout_seconds <= 0
        or maximum_output_bytes < 1
        or any(not isinstance(item, str) or "\x00" in item for item in argv)
    ):
        raise AdbCommandFailed
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_options(),
        )
    except OSError:
        raise AdbUnavailable from None

    total = [0]

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        output = bytearray()
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return bytes(output)
            total[0] += len(chunk)
            if total[0] > maximum_output_bytes:
                raise AdbOutputTooLarge
            output.extend(chunk)

    stdout_task = asyncio.create_task(read(process.stdout))
    stderr_task = asyncio.create_task(read(process.stderr))

    async def stop_process() -> None:
        if process.returncode is None:
            try:
                if sys.platform == "win32":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        await process.wait()

    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            returncode = await process.wait()
    except TimeoutError:
        await stop_process()
        raise AdbCommandTimedOut from None
    except AdbOutputTooLarge:
        await stop_process()
        raise
    except BaseException:
        await stop_process()
        raise
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    return ProcessResult(returncode=returncode, stdout=stdout, stderr=stderr)


@dataclass(frozen=True, slots=True)
class AdbDeviceListing:
    serial: str
    adb_state: AdbState
    transport_id: str | None
    usb: str | None


def _decode_output(payload: bytes) -> str:
    if not payload or len(payload) > _DEFAULT_OUTPUT_LIMIT:
        raise AdbProtocolError
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeError:
        raise AdbProtocolError from None


def parse_devices(payload: bytes) -> tuple[AdbDeviceListing, ...]:
    lines = _decode_output(payload).splitlines()
    if not lines or lines[0].strip() != "List of devices attached":
        raise AdbProtocolError
    devices: list[AdbDeviceListing] = []
    seen: set[str] = set()
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            raise AdbProtocolError
        serial, raw_state = fields[:2]
        if _SERIAL_PATTERN.fullmatch(serial) is None or serial in seen:
            raise AdbProtocolError
        seen.add(serial)
        state: AdbState
        if raw_state == "device":
            state = "device"
        elif raw_state == "unauthorized":
            state = "unauthorized"
        else:
            state = "offline"
        attributes: dict[str, str] = {}
        for field in fields[2:]:
            key, separator, value = field.partition(":")
            if separator and key and value:
                attributes[key] = value
        devices.append(
            AdbDeviceListing(
                serial=serial,
                adb_state=state,
                transport_id=attributes.get("transport_id"),
                usb=attributes.get("usb"),
            )
        )
        if len(devices) > _MAXIMUM_DEVICES:
            raise AdbProtocolError
    return tuple(devices)


def _validate_bound_arguments(arguments: tuple[str, ...]) -> None:
    if not arguments or arguments[0] != "shell" or len(arguments) < 2:
        raise AdbCommandFailed
    if arguments[1] not in _ALLOWED_SHELL_COMMANDS:
        raise AdbCommandFailed
    if any(
        not item
        or len(item) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in item)
        for item in arguments
    ):
        raise AdbCommandFailed
    command = arguments[1]
    tail = arguments[2:]
    valid = False
    if command == "getprop":
        valid = len(tail) == 1 and _PROPERTY_PATTERN.fullmatch(tail[0]) is not None
    elif command == "dumpsys":
        valid = tail == ("battery",)
    elif command == "df":
        valid = tail == ("-k", "/data")
    elif command == "which":
        valid = tail == ("perfetto",)
    if not valid:
        raise AdbCommandFailed


class AdbClient:
    def __init__(
        self,
        *,
        binary: Path,
        serial: str,
        runner: ProcessRunner = run_process,
    ) -> None:
        if not binary.is_absolute() or _SERIAL_PATTERN.fullmatch(serial) is None:
            raise ValueError("ADB client configuration is invalid")
        self._binary = binary
        self._serial = serial
        self._runner = runner

    async def run(self, *arguments: str) -> ProcessResult:
        _validate_bound_arguments(arguments)
        result = await self._runner(
            [str(self._binary), "-s", self._serial, *arguments],
            timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
            maximum_output_bytes=_DEFAULT_OUTPUT_LIMIT,
        )
        if result.returncode != 0:
            raise AdbCommandFailed
        return result


class AdbHostClient:
    def __init__(
        self,
        *,
        binary: Path,
        runner: ProcessRunner = run_process,
    ) -> None:
        if not binary.is_absolute():
            raise ValueError("ADB host configuration is invalid")
        self._binary = binary
        self._runner = runner

    async def devices(self) -> tuple[AdbDeviceListing, ...]:
        result = await self._runner(
            [str(self._binary), "devices", "-l"],
            timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
            maximum_output_bytes=_DEFAULT_OUTPUT_LIMIT,
        )
        if result.returncode != 0:
            raise AdbCommandFailed
        return parse_devices(result.stdout)


def _default_installer_candidates(platform_name: str) -> tuple[Path, ...]:
    if platform_name == "darwin":
        return (Path("/Library/PerfPilot Agent/platform-tools/adb"),)
    if platform_name == "win32":
        program_files = os.environ.get("ProgramFiles")
        if not program_files:
            return ()
        return (Path(program_files) / "PerfPilot Agent" / "platform-tools" / "adb.exe",)
    return (Path("/opt/perfpilot-agent/platform-tools/adb"),)


def _safe_binary(candidate: Path, workspace_root: Path) -> Path | None:
    try:
        resolved = candidate.resolve(strict=True)
        workspace = workspace_root.resolve(strict=False)
        metadata = resolved.stat()
    except OSError:
        return None
    if (
        not resolved.is_absolute()
        or resolved.is_relative_to(workspace)
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
    ):
        return None
    return resolved


async def resolve_adb(
    *,
    configured: Path | None,
    workspace_root: Path,
    runner: ProcessRunner = run_process,
    environ: Mapping[str, str] | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
    installer_candidates: Sequence[Path] | None = None,
    platform_name: str | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    current_platform = platform_name or sys.platform
    executable = "adb.exe" if current_platform == "win32" else "adb"
    candidates: list[Path] = []
    if configured is not None:
        candidates.append(configured)
    else:
        for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            root = environment.get(variable)
            if root:
                candidates.append(Path(root) / "platform-tools" / executable)
        discovered = path_lookup(executable)
        if discovered:
            candidates.append(Path(discovered))
        candidates.extend(
            installer_candidates
            if installer_candidates is not None
            else _default_installer_candidates(current_platform)
        )
    for candidate in candidates:
        safe = _safe_binary(candidate, workspace_root)
        if safe is None:
            if configured is not None:
                raise AdbUnavailable
            continue
        try:
            result = await runner(
                [str(safe), "version"],
                timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
                maximum_output_bytes=64 * 1024,
            )
        except AdbError:
            if configured is not None:
                raise AdbUnavailable from None
            continue
        if result.returncode == 0 and result.stdout.startswith(b"Android Debug Bridge version "):
            return safe
        if configured is not None:
            raise AdbUnavailable
    raise AdbUnavailable


__all__ = [
    "AdbClient",
    "AdbCommandFailed",
    "AdbCommandTimedOut",
    "AdbDeviceListing",
    "AdbError",
    "AdbHostClient",
    "AdbOutputTooLarge",
    "AdbProtocolError",
    "AdbState",
    "AdbUnavailable",
    "ProcessResult",
    "ProcessRunner",
    "parse_devices",
    "process_group_options",
    "resolve_adb",
    "run_process",
]
