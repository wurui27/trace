"""Local-only Android capture using the shared Device Agent primitives."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from perfpilot_agent.adb import ProcessRunner, run_process
from perfpilot_agent.capture import CaptureAdbDevice, CaptureDevice, write_memory_archive


_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_SERIAL = re.compile(r"^[!-~]{1,255}$")
_PACKAGE_LINE = re.compile(
    r"^package: name='([^'\r\n]+)' versionCode='([0-9]+)'(?: versionName='([^'\r\n]*)')?",
    re.MULTILINE,
)
_LAUNCH_LINE = re.compile(r"^launchable-activity: name='([^'\r\n]+)'", re.MULTILINE)
_MIN_SDK_LINE = re.compile(r"^sdkVersion:'([0-9]+)'", re.MULTILINE)
_TARGET_SDK_LINE = re.compile(r"^targetSdkVersion:'([0-9]+)'", re.MULTILINE)
_NATIVE_CODE_LINE = re.compile(r"^native-code:\s*(.+)$", re.MULTILINE)
_QUOTED_VALUE = re.compile(r"'([A-Za-z0-9_.-]{1,64})'")
_MAX_BADGING_BYTES = 256 * 1024


class LocalDeviceCaptureError(RuntimeError):
    def __init__(self, code: str = "local_device_capture_failed") -> None:
        super().__init__("Local Android device capture failed")
        self.code = code


@dataclass(frozen=True, slots=True)
class LocalAndroidToolchain:
    adb_binary: Path
    aapt2_binary: Path


@dataclass(frozen=True, slots=True)
class LocalApkMetadata:
    package_name: str
    version_name: str | None
    version_code: int
    launch_activity: str
    min_sdk: int | None
    target_sdk: int | None
    supported_abis: tuple[str, ...]
    has_native_libraries: bool


@dataclass(frozen=True, slots=True)
class LocalDeviceCapture:
    metadata: LocalApkMetadata
    startup_trace: Path
    scroll_trace: Path
    memory_evidence: Path


class LocalDeviceCaptureGateway(Protocol):
    async def capture(
        self,
        *,
        apk_path: Path,
        serial: str,
        workspace: Path,
    ) -> LocalDeviceCapture: ...


class LocalApkInspector(Protocol):
    async def inspect(self, apk_path: Path) -> LocalApkMetadata: ...


def _executable(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _sdk_roots(
    *,
    environ: Mapping[str, str],
    home: Path,
    platform_name: str,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        value = environ.get(name)
        if value:
            candidates.append(Path(value))
    if platform_name == "darwin":
        candidates.append(home / "Library" / "Android" / "sdk")
    elif platform_name == "win32":
        local_app_data = environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Android" / "Sdk")
        candidates.append(home / "AppData" / "Local" / "Android" / "Sdk")
    else:
        candidates.append(home / "Android" / "Sdk")
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser().absolute())
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _build_tools_key(path: Path) -> tuple[tuple[int, ...], int, str]:
    numbers = tuple(int(value) for value in re.findall(r"\d+", path.name))[:4]
    padded = numbers + (0,) * (4 - len(numbers))
    stable = int(re.fullmatch(r"\d+(?:\.\d+)*", path.name) is not None)
    return padded, stable, path.name


def _sdk_aapt2(root: Path, executable_name: str) -> Path | None:
    build_tools = root / "build-tools"
    try:
        versions = sorted(
            (item for item in build_tools.iterdir() if item.is_dir()),
            key=_build_tools_key,
            reverse=True,
        )
    except OSError:
        return None
    for version in versions:
        resolved = _executable(version / executable_name)
        if resolved is not None:
            return resolved
    return None


def resolve_local_adb(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
    platform_name: str = sys.platform,
) -> Path:
    values = os.environ if environ is None else environ
    home_directory = Path.home() if home is None else Path(home)
    windows = platform_name == "win32"
    adb_name = "adb.exe" if windows else "adb"
    roots = _sdk_roots(
        environ=values,
        home=home_directory,
        platform_name=platform_name,
    )
    explicit_adb = values.get("PERFPILOT_LOCAL_ADB")
    if explicit_adb is not None:
        adb = _executable(Path(explicit_adb))
        if adb is None:
            raise LocalDeviceCaptureError("android_toolchain_unavailable")
    else:
        adb = next(
            (
                candidate
                for root in roots
                if (candidate := _executable(root / "platform-tools" / adb_name))
                is not None
            ),
            None,
        )
        if adb is None:
            located = which(adb_name)
            adb = _executable(Path(located)) if located else None
    if adb is None:
        raise LocalDeviceCaptureError("android_toolchain_unavailable")
    return adb


def resolve_local_android_toolchain(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
    platform_name: str = sys.platform,
) -> LocalAndroidToolchain:
    values = os.environ if environ is None else environ
    home_directory = Path.home() if home is None else Path(home)
    windows = platform_name == "win32"
    aapt2_name = "aapt2.exe" if windows else "aapt2"
    roots = _sdk_roots(
        environ=values,
        home=home_directory,
        platform_name=platform_name,
    )
    adb = resolve_local_adb(
        environ=values,
        which=which,
        home=home_directory,
        platform_name=platform_name,
    )

    explicit_aapt2 = values.get("PERFPILOT_LOCAL_AAPT2")
    if explicit_aapt2 is not None:
        aapt2 = _executable(Path(explicit_aapt2))
        if aapt2 is None:
            raise LocalDeviceCaptureError("android_toolchain_unavailable")
    else:
        aapt2 = next(
            (
                candidate
                for root in roots
                if (candidate := _sdk_aapt2(root, aapt2_name)) is not None
            ),
            None,
        )
        if aapt2 is None:
            located = which(aapt2_name)
            aapt2 = _executable(Path(located)) if located else None

    if aapt2 is None:
        raise LocalDeviceCaptureError("android_toolchain_unavailable")
    return LocalAndroidToolchain(adb_binary=adb, aapt2_binary=aapt2)


def _sdk_value(pattern: re.Pattern[str], output: str) -> int | None:
    matched = pattern.search(output)
    if matched is None:
        return None
    value = int(matched.group(1), 10)
    return value if 1 <= value <= 100 else None


def _launch_component(package_name: str, activity: str) -> str:
    if activity.startswith("."):
        normalized = f"{package_name}{activity}"
    elif "." not in activity:
        normalized = f"{package_name}.{activity}"
    else:
        normalized = activity
    if len(normalized) > 510 or _PACKAGE.fullmatch(normalized) is None:
        raise LocalDeviceCaptureError("apk_metadata_invalid")
    return f"{package_name}/{normalized}"


def _parse_badging(payload: bytes) -> LocalApkMetadata:
    if not payload or len(payload) > _MAX_BADGING_BYTES:
        raise LocalDeviceCaptureError("apk_metadata_invalid")
    try:
        output = payload.decode("utf-8", errors="strict")
    except UnicodeError:
        raise LocalDeviceCaptureError("apk_metadata_invalid") from None
    package = _PACKAGE_LINE.search(output)
    launcher = _LAUNCH_LINE.search(output)
    if (
        package is None
        or launcher is None
        or len(package.group(1)) > 255
        or _PACKAGE.fullmatch(package.group(1)) is None
    ):
        raise LocalDeviceCaptureError("apk_metadata_invalid")
    package_name = package.group(1)
    version_code = int(package.group(2), 10)
    if version_code > 9_223_372_036_854_775_807:
        raise LocalDeviceCaptureError("apk_metadata_invalid")
    version_name = package.group(3) or None
    if version_name is not None and len(version_name) > 255:
        raise LocalDeviceCaptureError("apk_metadata_invalid")
    native_line = _NATIVE_CODE_LINE.search(output)
    supported_abis = (
        tuple(dict.fromkeys(_QUOTED_VALUE.findall(native_line.group(1))))
        if native_line is not None
        else ()
    )
    if len(supported_abis) > 32:
        raise LocalDeviceCaptureError("apk_metadata_invalid")
    return LocalApkMetadata(
        package_name=package_name,
        version_name=version_name,
        version_code=version_code,
        launch_activity=_launch_component(package_name, launcher.group(1)),
        min_sdk=_sdk_value(_MIN_SDK_LINE, output),
        target_sdk=_sdk_value(_TARGET_SDK_LINE, output),
        supported_abis=supported_abis,
        has_native_libraries=bool(supported_abis),
    )


class Aapt2LocalApkInspector:
    def __init__(
        self,
        *,
        binary: Path,
        runner: ProcessRunner = run_process,
    ) -> None:
        resolved = Path(binary).resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError("AAPT2 binary is unavailable")
        self._binary = resolved
        self._runner = runner

    async def inspect(self, apk_path: Path) -> LocalApkMetadata:
        source = Path(apk_path).resolve(strict=True)
        if not source.is_file() or source.is_symlink():
            raise LocalDeviceCaptureError("apk_metadata_invalid")
        result = await self._runner(
            [str(self._binary), "dump", "badging", str(source)],
            timeout_seconds=30,
            maximum_output_bytes=_MAX_BADGING_BYTES,
        )
        if result.returncode != 0 or result.stderr and not result.stdout:
            raise LocalDeviceCaptureError("apk_metadata_invalid")
        return _parse_badging(result.stdout)


class AdbLocalDeviceCaptureGateway:
    def __init__(
        self,
        *,
        adb_binary: Path,
        inspector: LocalApkInspector,
        device_factory: Callable[..., CaptureDevice] = CaptureAdbDevice,
        sleep: Callable[[float], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        startup_duration_seconds: int = 3,
        scroll_duration_seconds: int = 6,
        memory_duration_seconds: int = 6,
        memory_rounds: int = 3,
        scroll_swipe_count: int = 3,
    ) -> None:
        resolved_adb = Path(adb_binary).resolve(strict=True)
        if (
            not resolved_adb.is_file()
            or not os.access(resolved_adb, os.X_OK)
            or min(
                startup_duration_seconds,
                scroll_duration_seconds,
                memory_duration_seconds,
                memory_rounds,
                scroll_swipe_count,
            )
            < 1
        ):
            raise ValueError("local capture configuration is invalid")
        self._adb_binary = resolved_adb
        self._inspector = inspector
        self._device_factory = device_factory
        self._sleep = sleep
        self._clock = clock
        self._startup_duration_seconds = startup_duration_seconds
        self._scroll_duration_seconds = scroll_duration_seconds
        self._memory_duration_seconds = memory_duration_seconds
        self._memory_rounds = memory_rounds
        self._scroll_swipe_count = scroll_swipe_count

    async def capture(
        self,
        *,
        apk_path: Path,
        serial: str,
        workspace: Path,
    ) -> LocalDeviceCapture:
        source = Path(apk_path).resolve(strict=True)
        root = Path(workspace).resolve(strict=True)
        if (
            not source.is_file()
            or source.is_symlink()
            or not root.is_dir()
            or root.is_symlink()
            or _SERIAL.fullmatch(serial) is None
        ):
            raise LocalDeviceCaptureError("local_capture_input_invalid")
        metadata = await self._inspector.inspect(source)
        device = self._device_factory(
            binary=self._adb_binary,
            serial=serial,
            workspace=root,
        )
        startup_trace = root / "startup.perfetto-trace"
        scroll_trace = root / "scroll.perfetto-trace"
        try:
            await device.install(source)
            await device.capture_trace(
                scenario_type="startup",
                package_name=metadata.package_name,
                launch_activity=metadata.launch_activity,
                output=startup_trace,
                duration_seconds=self._startup_duration_seconds,
                swipe_count=1,
                sleep=self._sleep,
            )
            await device.capture_trace(
                scenario_type="scroll",
                package_name=metadata.package_name,
                launch_activity=metadata.launch_activity,
                output=scroll_trace,
                duration_seconds=self._scroll_duration_seconds,
                swipe_count=self._scroll_swipe_count,
                sleep=self._sleep,
            )
            memory_started_at = self._clock()
            samples = await device.collect_memory_samples(
                package_name=metadata.package_name,
                launch_activity=metadata.launch_activity,
                rounds=self._memory_rounds,
                duration_seconds=self._memory_duration_seconds,
                sleep=self._sleep,
            )
            memory_evidence = write_memory_archive(
                directory=root,
                package_name=metadata.package_name,
                samples=samples,
                started_at=memory_started_at,
                completed_at=self._clock(),
            )
        finally:
            await device.cleanup()
        return LocalDeviceCapture(
            metadata=metadata,
            startup_trace=startup_trace,
            scroll_trace=scroll_trace,
            memory_evidence=memory_evidence,
        )


def build_local_device_capture_gateway(
    *,
    toolchain: LocalAndroidToolchain | None = None,
) -> AdbLocalDeviceCaptureGateway:
    tools = toolchain or resolve_local_android_toolchain()
    return AdbLocalDeviceCaptureGateway(
        adb_binary=tools.adb_binary,
        inspector=Aapt2LocalApkInspector(binary=tools.aapt2_binary),
        sleep=asyncio.sleep,
    )


__all__ = [
    "Aapt2LocalApkInspector",
    "AdbLocalDeviceCaptureGateway",
    "LocalAndroidToolchain",
    "LocalApkInspector",
    "LocalApkMetadata",
    "LocalDeviceCapture",
    "LocalDeviceCaptureGateway",
    "LocalDeviceCaptureError",
    "build_local_device_capture_gateway",
    "resolve_local_adb",
    "resolve_local_android_toolchain",
]
