from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from perfpilot_agent.adb import ProcessResult
from perfpilot_api.local_device_capture import (
    Aapt2LocalApkInspector,
    AdbLocalDeviceCaptureGateway,
    LocalApkMetadata,
)


class _FakeInspector:
    def __init__(self, metadata: LocalApkMetadata) -> None:
        self.metadata = metadata
        self.paths: list[Path] = []

    async def inspect(self, apk_path: Path) -> LocalApkMetadata:
        self.paths.append(apk_path)
        return self.metadata


class _FakeCaptureDevice:
    def __init__(self) -> None:
        self.installed: list[Path] = []
        self.traces: list[str] = []
        self.memory_calls = 0
        self.cleanup_calls = 0

    async def install(self, apk: Path) -> None:
        self.installed.append(apk)

    async def capture_trace(self, **kwargs: object) -> None:
        scenario = str(kwargs["scenario_type"])
        output = kwargs["output"]
        assert isinstance(output, Path)
        output.write_bytes(f"{scenario}-trace".encode())
        self.traces.append(scenario)

    async def collect_memory_samples(self, **kwargs: object) -> tuple[str, ...]:
        self.memory_calls += 1
        return (
            "Applications Memory Usage\nTOTAL PSS: 100\n",
            "Applications Memory Usage\nTOTAL PSS: 120\n",
        )

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


@pytest.mark.asyncio
async def test_aapt2_inspector_returns_launcher_and_version_metadata(tmp_path: Path) -> None:
    binary = tmp_path / "aapt2"
    binary.write_text("test", encoding="utf-8")
    binary.chmod(0o700)
    apk = tmp_path / "demo.apk"
    apk.write_bytes(b"apk")
    calls: list[list[str]] = []

    async def runner(
        argv: list[str],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
    ) -> ProcessResult:
        calls.append(argv)
        assert timeout_seconds == 30
        assert maximum_output_bytes == 256 * 1024
        return ProcessResult(
            returncode=0,
            stdout=(
                b"package: name='dev.perfpilot.demo' versionCode='42' versionName='2.4.2'\n"
                b"sdkVersion:'23'\n"
                b"targetSdkVersion:'35'\n"
                b"launchable-activity: name='.MainActivity' label='' icon=''\n"
                b"native-code: 'arm64-v8a' 'x86_64'\n"
            ),
            stderr=b"",
        )

    metadata = await Aapt2LocalApkInspector(binary=binary, runner=runner).inspect(apk)

    assert metadata == LocalApkMetadata(
        package_name="dev.perfpilot.demo",
        version_name="2.4.2",
        version_code=42,
        launch_activity="dev.perfpilot.demo/dev.perfpilot.demo.MainActivity",
        min_sdk=23,
        target_sdk=35,
        supported_abis=("arm64-v8a", "x86_64"),
        has_native_libraries=True,
    )
    assert calls == [[str(binary), "dump", "badging", str(apk)]]


@pytest.mark.asyncio
async def test_local_capture_reuses_agent_trace_and_memory_primitives(tmp_path: Path) -> None:
    apk = tmp_path / "input.apk"
    apk.write_bytes(b"apk")
    adb = tmp_path / "adb"
    adb.write_text("test", encoding="utf-8")
    adb.chmod(0o700)
    workspace = tmp_path / "capture"
    workspace.mkdir()
    metadata = LocalApkMetadata(
        package_name="dev.perfpilot.demo",
        version_name="2.4.2",
        version_code=42,
        launch_activity="dev.perfpilot.demo/dev.perfpilot.demo.MainActivity",
        min_sdk=23,
        target_sdk=35,
        supported_abis=("arm64-v8a",),
        has_native_libraries=True,
    )
    inspector = _FakeInspector(metadata)
    device = _FakeCaptureDevice()
    started = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
    times = iter((started, started + timedelta(seconds=6)))

    gateway = AdbLocalDeviceCaptureGateway(
        adb_binary=adb,
        inspector=inspector,
        device_factory=lambda **_kwargs: device,
        sleep=lambda _seconds: _done(),
        clock=lambda: next(times),
        startup_duration_seconds=1,
        scroll_duration_seconds=1,
        memory_duration_seconds=1,
        memory_rounds=1,
        scroll_swipe_count=1,
    )

    result = await gateway.capture(
        apk_path=apk,
        serial="device-under-test",
        workspace=workspace,
    )

    assert result.metadata == metadata
    assert result.startup_trace.read_bytes() == b"startup-trace"
    assert result.scroll_trace.read_bytes() == b"scroll-trace"
    assert result.memory_evidence.name == "memory-evidence.tar"
    assert result.memory_evidence.is_file()
    assert inspector.paths == [apk]
    assert device.installed == [apk]
    assert device.traces == ["startup", "scroll"]
    assert device.memory_calls == 1
    assert device.cleanup_calls == 1


async def _done() -> None:
    return None
