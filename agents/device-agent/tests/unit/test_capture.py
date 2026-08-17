from __future__ import annotations

import tarfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_agent.adb import ProcessResult
from perfpilot_agent.capture import (
    CaptureAdbDevice,
    CaptureError,
    CaptureTaskRunner,
    ThermalReading,
    write_memory_archive,
)
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.security import TaskSnapshot
from perfpilot_agent.state import AgentRuntimeState
from perfpilot_agent.uploads import UploadedArtifact


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, argv, *, timeout_seconds, maximum_output_bytes):
        self.calls.append(argv)
        return ProcessResult(returncode=0, stdout=b"ok", stderr=b"")


@pytest.mark.asyncio
async def test_capture_adb_is_serial_bound_and_rejects_paths_outside_workspace(tmp_path) -> None:
    adb = tmp_path / "adb"
    adb.write_bytes(b"binary")
    workspace = tmp_path / "execution"
    workspace.mkdir()
    apk = workspace / "input.apk"
    apk.write_bytes(b"apk")
    runner = RecordingRunner()
    device = CaptureAdbDevice(
        binary=adb,
        serial="device-under-test",
        workspace=workspace,
        runner=runner,
    )

    await device.install(apk)
    await device.force_stop("dev.perfpilot.demo")
    await device.start_activity("dev.perfpilot.demo/dev.perfpilot.demo.MainActivity")

    assert all(call[:3] == [str(adb), "-s", "device-under-test"] for call in runner.calls)
    assert runner.calls[0][3:] == ["install", "-r", "-t", str(apk)]
    with pytest.raises(ValueError):
        await device.install(tmp_path / "outside.apk")


@pytest.mark.asyncio
async def test_perfetto_detached_session_does_not_mix_background_mode(tmp_path) -> None:
    adb = tmp_path / "adb"
    adb.write_bytes(b"binary")
    workspace = tmp_path / "execution"
    workspace.mkdir()
    runner = RecordingRunner()
    device = CaptureAdbDevice(
        binary=adb,
        serial="device-under-test",
        workspace=workspace,
        runner=runner,
    )

    await device._start_perfetto(
        config="/data/local/tmp/perfpilot-test.pbtxt",
        trace="/data/misc/perfetto-traces/perfpilot-test.perfetto-trace",
        session="perfpilot-test-startup",
    )

    assert runner.calls == [
        [
            str(adb),
            "-s",
            "device-under-test",
            "shell",
            "perfetto",
            "--txt",
            "-c",
            "/data/local/tmp/perfpilot-test.pbtxt",
            "-o",
            "/data/misc/perfetto-traces/perfpilot-test.perfetto-trace",
            "--detach=perfpilot-test-startup",
        ]
    ]


@pytest.mark.parametrize("scenario_type", ["startup", "scroll"])
def test_detached_perfetto_config_streams_trace_into_output_file(scenario_type) -> None:
    rendered = CaptureAdbDevice._render_config(
        scenario_type=scenario_type,
        package_name="dev.perfpilot.demo",
        duration_seconds=30,
    )

    assert "write_into_file: true" in rendered
    assert "file_write_period_ms: 1000" in rendered


class FakeDownloader:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def download(self, *, target: Path, **kwargs) -> Path:
        self.events.append("download")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"verified-apk")
        return target

    async def aclose(self) -> None:
        return None


class FakeDevice:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def adb_version(self) -> str:
        return "Android Debug Bridge version 1.0.41"

    async def thermal_reading(self) -> ThermalReading:
        return ThermalReading(temperature_c=31.5, thermal_status=0)

    async def install(self, apk: Path) -> None:
        assert apk.read_bytes() == b"verified-apk"
        self.events.append("install")

    async def capture_trace(self, *, scenario_type: str, output: Path, **kwargs) -> None:
        self.events.append(f"capture:{scenario_type}")
        output.write_bytes(b"perfetto-trace")

    async def collect_memory_samples(self, **kwargs):
        return ()

    async def cleanup(self) -> None:
        self.events.append("cleanup")

    async def uninstall(self, package_name: str) -> None:
        self.events.append("uninstall")


class HotAfterCaptureDevice(FakeDevice):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.readings = 0

    async def thermal_reading(self) -> ThermalReading:
        self.readings += 1
        if self.readings == 1:
            return ThermalReading(temperature_c=31.5, thermal_status=0)
        return ThermalReading(temperature_c=43.0, thermal_status=2)


class InstallFailureDevice(FakeDevice):
    async def install(self, apk: Path) -> None:
        assert apk.read_bytes() == b"verified-apk"
        self.events.append("install")
        raise CaptureError("apk_install_failed")


class FakeUploader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.counter = 0

    async def upload(self, *, descriptor, **kwargs) -> UploadedArtifact:
        self.events.append(f"upload:{descriptor.kind}")
        self.counter += 1
        return UploadedArtifact(
            artifact_id=UUID(f"76000000-0000-4000-8000-{self.counter:012d}"),
            kind=descriptor.kind,
            mime=descriptor.mime,
            size=descriptor.size,
            sha256_b64=descriptor.sha256_b64,
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_capture_runner_downloads_before_install_and_returns_closed_manifest(
    tmp_path,
    task_claims,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )
    task = TaskSnapshot.model_validate(task_claims)
    events: list[str] = []
    device = FakeDevice(events)
    downloader = FakeDownloader(events)
    uploader = FakeUploader(events)
    runner = CaptureTaskRunner(
        config=config,
        adb_binary=tmp_path / "adb",
        control=object(),
        state=AgentRuntimeState(),
        redactor=None,
        device_factory=lambda **kwargs: device,
        downloader_factory=lambda **kwargs: downloader,
        uploader_factory=lambda **kwargs: uploader,
        sleep=lambda _: _done(),
        clock=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
    )

    execution = await runner.start(task, serial="device-under-test")
    outcome = await execution.wait()

    assert events.index("download") < events.index("install")
    assert "capture:startup" in events
    assert events[-2:] == ["cleanup", "uninstall"]
    assert outcome.manifest["execution_id"] == str(task.execution_id)
    assert outcome.manifest["lease_version"] == task.lease_version
    assert outcome.manifest["state"] == "completed"
    assert [item["kind"] for item in outcome.manifest["artifacts"]] == [
        "startup_trace",
        "agent_log",
    ]
    assert outcome.manifest["scenarios"][0]["state"] == "completed"


@pytest.mark.asyncio
async def test_capture_rejects_trace_when_post_measurement_thermal_gate_fails(
    tmp_path,
    task_claims,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca,
        workspace_root=workspace,
    )
    task = TaskSnapshot.model_validate(task_claims)
    events: list[str] = []
    uploader = FakeUploader(events)
    runner = CaptureTaskRunner(
        config=config,
        adb_binary=tmp_path / "adb",
        control=object(),
        state=AgentRuntimeState(),
        redactor=None,
        device_factory=lambda **kwargs: HotAfterCaptureDevice(events),
        downloader_factory=lambda **kwargs: FakeDownloader(events),
        uploader_factory=lambda **kwargs: uploader,
        sleep=lambda _: _done(),
        clock=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
    )

    execution = await runner.start(task, serial="device-under-test")
    outcome = await execution.wait()

    assert outcome.manifest["state"] == "failed"
    assert outcome.manifest["diagnostic_code"] == "agent_execution_failed"
    assert outcome.manifest["scenarios"][0]["diagnostic_code"] == "thermal_gate_failed"
    assert outcome.manifest["scenarios"][0]["temperature_end_c"] == 43.0
    assert [item["kind"] for item in outcome.manifest["artifacts"]] == ["agent_log"]


@pytest.mark.asyncio
async def test_capture_install_failure_returns_closed_failed_manifest(
    tmp_path,
    task_claims,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    claims = {
        **task_claims,
        "schema_version": "1.1",
        "team_id": "81000000-0000-4000-8000-000000000001",
        "scenarios": [
            task_claims["scenarios"][0],
            {
                **task_claims["scenarios"][0],
                "scenario_type": "scroll",
                "swipe_count": 5,
            },
        ],
        "allowed_uploads": ["startup_trace", "scroll_trace", "agent_log"],
    }
    task = TaskSnapshot.model_validate(claims)
    events: list[str] = []
    runner = CaptureTaskRunner(
        config=AgentConfig(
            server_url="https://control.example.test",
            ca_bundle=ca,
            workspace_root=workspace,
        ),
        adb_binary=tmp_path / "adb",
        control=object(),
        state=AgentRuntimeState(),
        redactor=None,
        device_factory=lambda **kwargs: InstallFailureDevice(events),
        downloader_factory=lambda **kwargs: FakeDownloader(events),
        uploader_factory=lambda **kwargs: FakeUploader(events),
        sleep=lambda _: _done(),
        clock=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
    )

    outcome = await (await runner.start(task, serial="device-under-test")).wait()

    assert outcome.manifest["state"] == "failed"
    assert outcome.manifest["diagnostic_code"] == "agent_execution_failed"
    assert [item["kind"] for item in outcome.manifest["artifacts"]] == ["agent_log"]
    assert [item["scenario_type"] for item in outcome.manifest["scenarios"]] == [
        "startup",
        "scroll",
    ]
    assert {
        (item["state"], item["diagnostic_code"])
        for item in outcome.manifest["scenarios"]
    } == {("failed", "apk_install_failed")}
    assert events == ["download", "install", "upload:agent_log", "cleanup", "uninstall"]


async def _done() -> None:
    return None


def test_memory_archive_contains_raw_samples_and_machine_readable_summary(tmp_path) -> None:
    output = write_memory_archive(
        directory=tmp_path,
        package_name="dev.perfpilot.demo",
        samples=(
            "Applications Memory Usage\nTOTAL PSS: 100\n",
            "Applications Memory Usage\nTOTAL PSS: 120\n",
        ),
        started_at=datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 5, 9, 1, tzinfo=UTC),
    )

    with tarfile.open(output, "r") as archive:
        names = set(archive.getnames())

    assert names == {
        "meminfo/meminfo-000.txt",
        "meminfo/meminfo-001.txt",
        "metadata.json",
        "summary.json",
        "memory_cycles.csv",
    }
