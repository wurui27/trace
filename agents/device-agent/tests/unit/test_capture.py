from __future__ import annotations

import tarfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import perfpilot_agent.capture as capture_module

from perfpilot_agent.adb import ProcessResult
from perfpilot_agent.capture import (
    CaptureAdbDevice,
    CaptureError,
    CaptureScriptRunner,
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


def test_capture_runner_passes_configured_ca_to_default_transfers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_bundle = tmp_path / "private-ca.crt"
    ca_bundle.write_text("private test CA", encoding="utf-8")
    workspace = tmp_path / "work"
    observed: list[dict[str, object]] = []

    def observe(**kwargs: object) -> object:
        observed.append(kwargs)
        return object()

    monkeypatch.setattr(capture_module, "InputDownloader", observe)
    monkeypatch.setattr(capture_module, "MultipartUploader", observe)
    runner = CaptureTaskRunner(
        config=AgentConfig(
            server_url="https://control.example.test",
            ca_bundle=ca_bundle,
            workspace_root=workspace,
        ),
        adb_binary=tmp_path / "adb",
        control=object(),
        state=AgentRuntimeState(),
        redactor=None,
    )

    runner._downloader_factory(workspace_root=workspace)
    runner._uploader_factory(checkpoint_path=workspace / "upload-state.json")

    assert [item["ca_bundle"] for item in observed] == [ca_bundle, ca_bundle]


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


class FakeScriptCapture:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def capture(self, *, task, serial: str, workspace: Path) -> Path:
        self.events.append(f"script:{task.test_type}:{task.launch_mode}:{serial}")
        target = workspace / "script.trace"
        target.write_bytes(b"script-perfetto-trace")
        return target


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


@pytest.mark.asyncio
async def test_script_capture_never_downloads_or_installs_apk(
    tmp_path: Path,
    task_claims,
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("trace_app.sh", "trace_manual.sh", "scroll_test.sh"):
        (scripts / name).write_text("#!/bin/bash\n", encoding="utf-8")
    claims = {
        **task_claims,
        "schema_version": "1.2",
        "team_id": "81000000-0000-4000-8000-000000000001",
        "test_type": "cold_start",
        "launch_mode": "automatic",
        "package_name": "com.rivotek.mediacenter",
        "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        "cleanup_policy": "keep_installed",
        "input_artifacts": [],
        "scenarios": [
            {
                **task_claims["scenarios"][0],
                "duration_seconds": 15,
                "memory_rounds": 0,
                "swipe_count": 0,
            }
        ],
        "allowed_uploads": ["startup_trace", "agent_log"],
    }
    task = TaskSnapshot.model_validate(claims)
    events: list[str] = []
    runner = CaptureTaskRunner(
        config=AgentConfig(
            server_url="https://control.example.test",
            ca_bundle=ca,
            workspace_root=workspace,
            capture_script_root=scripts,
        ),
        adb_binary=tmp_path / "adb",
        control=object(),
        state=AgentRuntimeState(),
        redactor=None,
        device_factory=lambda **kwargs: FakeDevice(events),
        downloader_factory=lambda **kwargs: FakeDownloader(events),
        uploader_factory=lambda **kwargs: FakeUploader(events),
        script_runner_factory=lambda **kwargs: FakeScriptCapture(events),
        sleep=lambda _: _done(),
        clock=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
    )
    execution = await runner.start(task, serial="device-under-test")
    outcome = await execution.wait()

    assert outcome.manifest["state"] == "completed"
    assert [item["kind"] for item in outcome.manifest["artifacts"]] == [
        "startup_trace",
        "agent_log",
    ]
    assert "download" not in events
    assert "install" not in events
    assert events[0] == "script:cold_start:automatic:device-under-test"

    execution_workspace = workspace / str(task.execution_id)
    assert execution_workspace.is_dir()
    await execution.finalize()
    assert not execution_workspace.exists()


async def _done() -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("test_type", "launch_mode", "scenario_type", "expected_script", "expected_mode"),
    [
        ("cold_start", "automatic", "startup", "trace_app.sh", "cold"),
        ("hot_start", "automatic", "startup", "trace_app.sh", "hot"),
        ("cold_start", "manual", "startup", "trace_manual.sh", "manual"),
        ("hot_start", "manual", "startup", "trace_manual.sh", "manual"),
        ("scroll", "manual", "scroll", "scroll_test.sh", "manual"),
    ],
)
async def test_capture_script_runner_uses_existing_scripts_without_leaking_environment(
    tmp_path: Path,
    task_claims,
    monkeypatch: pytest.MonkeyPatch,
    test_type: str,
    launch_mode: str,
    scenario_type: str,
    expected_script: str,
    expected_mode: str,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    trace_script = scripts / "trace_app.sh"
    trace_script.write_text(
        '#!/bin/bash\nset -eu\nprintf "%s\\n" "$0" "$@" > "$TRACE_OUTDIR/args.txt"\n'
        'printf "%s\\n" "${PERFPILOT_TEST_SECRET-unset}" > "$TRACE_OUTDIR/secret.txt"\n'
        'printf trace > "$TRACE_OUTDIR/trace.pb"\n',
        encoding="utf-8",
    )
    manual_script = scripts / "trace_manual.sh"
    manual_script.write_text(
        '#!/bin/bash\nset -eu\nout="$TRACE_OUTPUT_ROOT/manual"\nmkdir -p "$out"\n'
        'printf "%s\\n" "$0" "$@" > "$out/args.txt"\n'
        'printf "%s\\n" "${PERFPILOT_TEST_SECRET-unset}" > "$out/secret.txt"\n'
        'printf trace > "$out/trace.pb"\n',
        encoding="utf-8",
    )
    scroll_script = scripts / "scroll_test.sh"
    scroll_script.write_text(
        '#!/bin/bash\nset -eu\nread -r _\nout="$SCROLL_OUTPUT_ROOT/scroll"\nmkdir -p "$out"\n'
        'printf "%s\\n" "$0" "$@" > "$out/args.txt"\n'
        'printf "%s\\n" "${PERFPILOT_TEST_SECRET-unset}" > "$out/secret.txt"\n'
        'printf trace > "$out/scroll_r1.pb"\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PERFPILOT_TEST_SECRET", "must-not-leak")
    has_target = launch_mode == "automatic" or test_type == "scroll"
    claims = {
        **task_claims,
        "schema_version": "1.2",
        "team_id": "81000000-0000-4000-8000-000000000001",
        "test_type": test_type,
        "launch_mode": launch_mode,
        "package_name": "com.rivotek.mediacenter" if has_target else None,
        "launch_activity": (
            "com.rivotek.mediacenter/.shell.MediaCenterActivity"
            if has_target
            else None
        ),
        "cleanup_policy": "keep_installed",
        "input_artifacts": [],
        "scenarios": [
            {
                **task_claims["scenarios"][0],
                "scenario_type": scenario_type,
                "duration_seconds": 15,
                "memory_rounds": 0,
                "swipe_count": 0,
            }
        ],
        "allowed_uploads": [f"{scenario_type}_trace", "agent_log"],
    }
    task = TaskSnapshot.model_validate(claims)

    target = await CaptureScriptRunner(
        script_root=scripts,
        adb_binary=tmp_path / "adb",
    ).capture(task=task, serial="device-under-test", workspace=workspace)

    assert target.read_bytes() == b"trace"
    output_root = target.parent
    assert (output_root / "secret.txt").read_text(encoding="utf-8").strip() == "unset"
    arguments = (output_root / "args.txt").read_text(encoding="utf-8").splitlines()
    assert Path(arguments[0]).name == expected_script
    assert (
        expected_mode in arguments
        or expected_script in {"trace_manual.sh", "scroll_test.sh"}
    )
    assert "15" in arguments


@pytest.mark.asyncio
async def test_capture_script_runner_uses_a_fresh_private_directory_for_retry(
    tmp_path: Path,
    task_claims,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "trace_app.sh").write_text(
        '#!/bin/bash\nset -eu\nprintf trace > "$TRACE_OUTDIR/trace.pb"\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claims = {
        **task_claims,
        "schema_version": "1.2",
        "team_id": "81000000-0000-4000-8000-000000000001",
        "test_type": "cold_start",
        "launch_mode": "automatic",
        "package_name": "com.rivotek.mediacenter",
        "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        "cleanup_policy": "keep_installed",
        "input_artifacts": [],
        "scenarios": [
            {
                **task_claims["scenarios"][0],
                "duration_seconds": 15,
                "memory_rounds": 0,
                "swipe_count": 0,
            }
        ],
        "allowed_uploads": ["startup_trace", "agent_log"],
    }
    task = TaskSnapshot.model_validate(claims)
    runner = CaptureScriptRunner(
        script_root=scripts,
        adb_binary=tmp_path / "adb",
    )

    first = await runner.capture(task=task, serial="device-under-test", workspace=workspace)
    second = await runner.capture(task=task, serial="device-under-test", workspace=workspace)

    assert first != second
    assert first.read_bytes() == second.read_bytes() == b"trace"
    assert first.parent.parent == workspace
    assert second.parent.parent == workspace


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
