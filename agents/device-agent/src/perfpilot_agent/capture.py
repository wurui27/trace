from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import tarfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Protocol
from perfpilot_agent import __version__
from perfpilot_agent.adb import ProcessResult, ProcessRunner, run_process
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.executor import ExecutionOutcome, StopReason
from perfpilot_agent.logging import SecretRedactor
from perfpilot_agent.security import TaskScenario, TaskSnapshot
from perfpilot_agent.state import AgentRuntimeState
from perfpilot_agent.uploads import (
    ArtifactDescriptor,
    InputDownloader,
    MultipartUploader,
    UploadedArtifact,
    describe_artifact,
)

_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_COMPONENT = re.compile(r"^[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+$")
_SERIAL = re.compile(r"^[!-~]{1,255}$")
_REMOTE_FILE = re.compile(r"^/data/(?:local/tmp|misc/perfetto-traces)/[A-Za-z0-9._-]{1,200}$")
_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_BATTERY_TEMPERATURE = re.compile(r"^\s*temperature:\s*(-?\d+)\s*$", re.MULTILINE)
_THERMAL_STATUS = re.compile(r"(?:mStatus\s*=|Thermal Status:\s*)(\d+)", re.IGNORECASE)
_TOTAL_PSS = re.compile(r"TOTAL PSS:\s*(\d+)", re.IGNORECASE)
_TOTAL_ROW = re.compile(r"^\s*TOTAL\s+(\d+)(?:\s+(\d+))?", re.MULTILINE)
_MAXIMUM_COMMAND_OUTPUT = 4 * 1024 * 1024
_MAXIMUM_AGENT_LOG = 64 * 1024


class CaptureError(RuntimeError):
    def __init__(self, code: str = "capture_failed") -> None:
        super().__init__("PerfPilot Agent capture failed")
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) else "capture_failed"


@dataclass(frozen=True, slots=True)
class ThermalReading:
    temperature_c: float | None
    thermal_status: int | None

    @property
    def acceptable(self) -> bool:
        return (
            self.temperature_c is not None
            and self.temperature_c <= 42
            and self.thermal_status is not None
            and self.thermal_status <= 1
        )


def _safe_text(payload: bytes, *, maximum: int = _MAXIMUM_COMMAND_OUTPUT) -> str:
    if len(payload) > maximum:
        raise CaptureError("adb_output_too_large")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeError:
        raise CaptureError("adb_output_invalid") from None


class CaptureAdbDevice:
    def __init__(
        self,
        *,
        binary: Path,
        serial: str,
        workspace: Path,
        runner: ProcessRunner = run_process,
    ) -> None:
        if (
            not binary.is_absolute()
            or _SERIAL.fullmatch(serial) is None
            or not workspace.is_absolute()
        ):
            raise ValueError("capture ADB configuration is invalid")
        self._binary = binary
        self._serial = serial
        self._workspace = workspace.resolve(strict=False)
        self._runner = runner
        self._remote_files: set[str] = set()
        self._sessions: set[str] = set()

    def _local(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self._workspace):
            raise ValueError("capture path is outside the execution workspace")
        return resolved

    @staticmethod
    def _package(value: str) -> str:
        if _PACKAGE.fullmatch(value) is None:
            raise ValueError("package name is invalid")
        return value

    @staticmethod
    def _component(value: str) -> str:
        if _COMPONENT.fullmatch(value) is None:
            raise ValueError("launch component is invalid")
        return value

    async def _run(
        self,
        *arguments: str,
        timeout_seconds: float = 30,
        maximum_output_bytes: int = _MAXIMUM_COMMAND_OUTPUT,
    ) -> ProcessResult:
        if any(
            not argument
            or len(argument) > 1_024
            or "\x00" in argument
            or any(ord(character) < 32 or ord(character) == 127 for character in argument)
            for argument in arguments
        ):
            raise ValueError("ADB arguments are invalid")
        result = await self._runner(
            [str(self._binary), "-s", self._serial, *arguments],
            timeout_seconds=timeout_seconds,
            maximum_output_bytes=maximum_output_bytes,
        )
        if result.returncode != 0:
            raise CaptureError("adb_command_failed")
        return result

    async def adb_version(self) -> str:
        result = await self._runner(
            [str(self._binary), "version"],
            timeout_seconds=5,
            maximum_output_bytes=64 * 1024,
        )
        if result.returncode != 0:
            raise CaptureError("adb_unavailable")
        line = _safe_text(result.stdout, maximum=64 * 1024).splitlines()
        if not line or not line[0].startswith("Android Debug Bridge version "):
            raise CaptureError("adb_version_invalid")
        return line[0][:128]

    async def install(self, apk: Path) -> None:
        source = self._local(apk)
        if not source.is_file() or source.is_symlink():
            raise ValueError("APK path is invalid")
        await self._run("install", "-r", "-t", str(source), timeout_seconds=180)

    async def uninstall(self, package_name: str) -> None:
        await self._run("uninstall", self._package(package_name), timeout_seconds=60)

    async def force_stop(self, package_name: str) -> None:
        await self._run("shell", "am", "force-stop", self._package(package_name))

    async def start_activity(self, component: str) -> None:
        await self._run("shell", "am", "start", "-W", "-n", self._component(component))

    async def swipe(self) -> None:
        await self._run("shell", "input", "swipe", "540", "1600", "540", "400", "300")

    async def thermal_reading(self) -> ThermalReading:
        battery = _safe_text((await self._run("shell", "dumpsys", "battery")).stdout)
        thermal = _safe_text((await self._run("shell", "dumpsys", "thermalservice")).stdout)
        temperature_match = _BATTERY_TEMPERATURE.search(battery)
        status_match = _THERMAL_STATUS.search(thermal)
        temperature = (
            None if temperature_match is None else int(temperature_match.group(1), 10) / 10
        )
        status = None if status_match is None else int(status_match.group(1), 10)
        return ThermalReading(temperature_c=temperature, thermal_status=status)

    async def _push(self, source: Path, remote: str) -> None:
        local = self._local(source)
        if not local.is_file() or _REMOTE_FILE.fullmatch(remote) is None:
            raise ValueError("Perfetto configuration path is invalid")
        await self._run("push", str(local), remote, timeout_seconds=60)
        self._remote_files.add(remote)

    async def _pull(self, remote: str, target: Path) -> None:
        local = self._local(target)
        if _REMOTE_FILE.fullmatch(remote) is None:
            raise ValueError("Perfetto trace path is invalid")
        await self._run("pull", remote, str(local), timeout_seconds=180)

    async def _start_perfetto(self, *, config: str, trace: str, session: str) -> None:
        if (
            _REMOTE_FILE.fullmatch(config) is None
            or _REMOTE_FILE.fullmatch(trace) is None
            or _SESSION.fullmatch(session) is None
        ):
            raise ValueError("Perfetto session is invalid")
        await self._run(
            "shell",
            "perfetto",
            "--txt",
            "-c",
            config,
            "-o",
            trace,
            f"--detach={session}",
        )
        self._sessions.add(session)
        self._remote_files.add(trace)

    async def _stop_perfetto(self, session: str) -> None:
        if _SESSION.fullmatch(session) is None:
            raise ValueError("Perfetto session is invalid")
        await self._run("shell", "perfetto", f"--attach={session}", "--stop")
        self._sessions.discard(session)

    @staticmethod
    def _render_config(
        *,
        scenario_type: str,
        package_name: str,
        duration_seconds: int,
    ) -> str:
        resource_name = "startup.pbtxt" if scenario_type == "startup" else "scroll.pbtxt"
        source = (
            files("perfpilot_agent.resources.perfetto")
            .joinpath(resource_name)
            .read_text(encoding="utf-8")
        )
        duration_ms = max(30_000, (duration_seconds + 30) * 1_000)
        return source.replace("__PACKAGE__", package_name).replace(
            "__DURATION_MS__", str(duration_ms)
        )

    async def capture_trace(
        self,
        *,
        scenario_type: str,
        package_name: str,
        launch_activity: str,
        output: Path,
        duration_seconds: int,
        swipe_count: int,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        if scenario_type not in {"startup", "scroll"}:
            raise ValueError("trace scenario is invalid")
        package = self._package(package_name)
        component = self._component(launch_activity)
        target = self._local(output)
        prefix = f"perfpilot-{self._workspace.name[:36]}-{scenario_type}"
        config_path = self._workspace / f"{scenario_type}.pbtxt"
        remote_config = f"/data/local/tmp/{prefix}.pbtxt"
        remote_trace = f"/data/misc/perfetto-traces/{prefix}.perfetto-trace"
        session = prefix[:96]
        config_path.write_text(
            self._render_config(
                scenario_type=scenario_type,
                package_name=package,
                duration_seconds=duration_seconds,
            ),
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        await self._push(config_path, remote_config)
        if scenario_type == "startup":
            await self.force_stop(package)
        else:
            await self.start_activity(component)
        await self._start_perfetto(config=remote_config, trace=remote_trace, session=session)
        try:
            if scenario_type == "startup":
                await self.start_activity(component)
                await sleep(duration_seconds)
            else:
                count = max(1, swipe_count)
                interval = duration_seconds / count if duration_seconds else 0
                for _ in range(count):
                    await self.swipe()
                    await sleep(interval)
        finally:
            await self._stop_perfetto(session)
        await self._pull(remote_trace, target)
        if not target.is_file() or not 1 <= target.stat().st_size <= 512 * 1024 * 1024:
            raise CaptureError("trace_capture_invalid")

    async def collect_memory_samples(
        self,
        *,
        package_name: str,
        launch_activity: str,
        rounds: int,
        duration_seconds: int,
        sleep: Callable[[float], Awaitable[None]],
    ) -> tuple[str, ...]:
        package = self._package(package_name)
        await self.start_activity(self._component(launch_activity))
        sample_count = max(1, rounds) + 1
        interval = duration_seconds / max(1, sample_count - 1) if duration_seconds else 0
        samples: list[str] = []
        for index in range(sample_count):
            result = await self._run("shell", "dumpsys", "meminfo", "-d", package)
            sample = _safe_text(result.stdout)
            if not sample.strip():
                raise CaptureError("memory_evidence_empty")
            samples.append(sample)
            if index + 1 < sample_count:
                await self.swipe()
                await sleep(interval)
        return tuple(samples)

    async def cleanup(self) -> None:
        for session in tuple(self._sessions):
            try:
                await self._stop_perfetto(session)
            except (CaptureError, OSError, ValueError):
                pass
        for remote in tuple(self._remote_files):
            try:
                await self._run("shell", "rm", "-f", remote)
            except (CaptureError, OSError, ValueError):
                pass
            self._remote_files.discard(remote)


def _memory_totals(sample: str) -> tuple[int | None, int | None]:
    pss_match = _TOTAL_PSS.search(sample)
    row_match = _TOTAL_ROW.search(sample)
    pss = int(pss_match.group(1), 10) if pss_match is not None else None
    if pss is None and row_match is not None:
        pss = int(row_match.group(1), 10)
    rss = (
        int(row_match.group(2), 10)
        if row_match is not None and row_match.group(2) is not None
        else None
    )
    return pss, rss


def write_memory_archive(
    *,
    directory: Path,
    package_name: str,
    samples: Sequence[str],
    started_at: datetime,
    completed_at: datetime,
) -> Path:
    if (
        not directory.is_absolute()
        or _PACKAGE.fullmatch(package_name) is None
        or not samples
        or any(not isinstance(sample, str) or not sample.strip() for sample in samples)
        or started_at.tzinfo is None
        or completed_at.tzinfo is None
        or completed_at < started_at
    ):
        raise CaptureError("memory_evidence_invalid")
    evidence = directory / "memory-evidence"
    meminfo = evidence / "meminfo"
    meminfo.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows: list[tuple[int, int | None, int | None]] = []
    for index, sample in enumerate(samples):
        target = meminfo / f"meminfo-{index:03d}.txt"
        target.write_text(sample, encoding="utf-8")
        rows.append((index, *_memory_totals(sample)))
    metadata = {
        "schema_version": "1.0",
        "package_name": package_name,
        "scenario": "memory_cycle",
        "sample_count": len(samples),
        "started_at": started_at.astimezone(UTC).isoformat(),
        "completed_at": completed_at.astimezone(UTC).isoformat(),
    }
    (evidence / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    pss_values = [pss for _, pss, _ in rows if pss is not None]
    summary = {
        "schema_version": "1.0",
        "sample_count": len(samples),
        "total_pss_kb": pss_values,
        "delta_pss_kb": None if len(pss_values) < 2 else pss_values[-1] - pss_values[0],
    }
    (evidence / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    with (evidence / "memory_cycles.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("round", "total_pss_kb", "total_rss_kb"))
        writer.writerows(rows)
    archive_path = directory / "memory-evidence.tar"
    with tarfile.open(archive_path, "w") as archive:
        for source, arcname in (
            *(
                (meminfo / f"meminfo-{index:03d}.txt", f"meminfo/meminfo-{index:03d}.txt")
                for index in range(len(samples))
            ),
            (evidence / "metadata.json", "metadata.json"),
            (evidence / "summary.json", "summary.json"),
            (evidence / "memory_cycles.csv", "memory_cycles.csv"),
        ):
            archive.add(source, arcname=arcname, recursive=False)
    return archive_path


class CaptureDevice(Protocol):
    async def adb_version(self) -> str: ...

    async def thermal_reading(self) -> ThermalReading: ...

    async def install(self, apk: Path) -> None: ...

    async def capture_trace(self, **kwargs: object) -> None: ...

    async def collect_memory_samples(self, **kwargs: object) -> tuple[str, ...]: ...

    async def cleanup(self) -> None: ...

    async def uninstall(self, package_name: str) -> None: ...


@dataclass(slots=True)
class _ScenarioResult:
    scenario_type: str
    state: str
    started_at: datetime
    completed_at: datetime
    temperature_start_c: float | None
    temperature_end_c: float | None
    artifact_kind: str | None
    diagnostic_code: str | None


class CaptureExecution:
    def __init__(
        self,
        *,
        task: TaskSnapshot,
        workspace: Path,
        device: CaptureDevice,
        downloader: InputDownloader,
        uploader: MultipartUploader,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
        redactor: SecretRedactor | None,
    ) -> None:
        self._signed_task = task
        self._workspace = workspace
        self._device = device
        self._downloader = downloader
        self._uploader = uploader
        self._clock = clock
        self._sleep = sleep
        self._redactor = redactor
        self._stop_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._execute())

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CaptureError("agent_clock_invalid")
        return value.astimezone(UTC)

    async def _thermal_gate(self) -> ThermalReading:
        reading = await self._device.thermal_reading()
        if reading.acceptable:
            return reading
        recovery: list[ThermalReading] = []
        for _ in range(3):
            await self._sleep(10)
            recovery.append(await self._device.thermal_reading())
        if not all(item.acceptable for item in recovery):
            raise CaptureError("thermal_gate_failed")
        return recovery[-1]

    def _log(self, lines: list[str], value: str) -> None:
        rendered = value if self._redactor is None else self._redactor.redact(value)
        current = sum(len(line.encode("utf-8")) + 1 for line in lines)
        if current + len(rendered.encode("utf-8")) + 1 <= _MAXIMUM_AGENT_LOG:
            lines.append(rendered)

    async def _capture_scenario(
        self,
        scenario: TaskScenario,
        log_lines: list[str],
    ) -> tuple[_ScenarioResult, ArtifactDescriptor | None]:
        started = self._now()
        start_thermal: ThermalReading | None = None
        end_thermal: ThermalReading | None = None
        try:
            start_thermal = await self._thermal_gate()
            descriptor: ArtifactDescriptor
            if scenario.scenario_type in {"startup", "scroll"}:
                target = self._workspace / f"{scenario.scenario_type}.perfetto-trace"
                await self._device.capture_trace(
                    scenario_type=scenario.scenario_type,
                    package_name=self._signed_task.package_name,
                    launch_activity=self._signed_task.launch_activity,
                    output=target,
                    duration_seconds=scenario.duration_seconds,
                    swipe_count=scenario.swipe_count,
                    sleep=self._sleep,
                )
                descriptor = describe_artifact(
                    kind=(
                        "startup_trace" if scenario.scenario_type == "startup" else "scroll_trace"
                    ),
                    mime="application/x-perfetto-trace",
                    path=target,
                )
            else:
                samples = await self._device.collect_memory_samples(
                    package_name=self._signed_task.package_name,
                    launch_activity=self._signed_task.launch_activity,
                    rounds=scenario.memory_rounds,
                    duration_seconds=scenario.duration_seconds,
                    sleep=self._sleep,
                )
                archive = write_memory_archive(
                    directory=self._workspace,
                    package_name=self._signed_task.package_name,
                    samples=samples,
                    started_at=started,
                    completed_at=self._now(),
                )
                descriptor = describe_artifact(
                    kind="memory_evidence",
                    mime="application/x-tar",
                    path=archive,
                )
            end_thermal = await self._device.thermal_reading()
            if not end_thermal.acceptable:
                raise CaptureError("thermal_gate_failed")
            completed = self._now()
            self._log(log_lines, f"scenario={scenario.scenario_type} state=completed")
            return (
                _ScenarioResult(
                    scenario_type=scenario.scenario_type,
                    state="completed",
                    started_at=started,
                    completed_at=completed,
                    temperature_start_c=start_thermal.temperature_c,
                    temperature_end_c=end_thermal.temperature_c,
                    artifact_kind=descriptor.kind,
                    diagnostic_code=None,
                ),
                descriptor,
            )
        except CaptureError as error:
            completed = self._now()
            self._log(
                log_lines,
                f"scenario={scenario.scenario_type} state=failed code={error.code}",
            )
            return (
                _ScenarioResult(
                    scenario_type=scenario.scenario_type,
                    state="failed",
                    started_at=started,
                    completed_at=completed,
                    temperature_start_c=(
                        None if start_thermal is None else start_thermal.temperature_c
                    ),
                    temperature_end_c=(None if end_thermal is None else end_thermal.temperature_c),
                    artifact_kind=None,
                    diagnostic_code=error.code,
                ),
                None,
            )

    async def _execute(self) -> ExecutionOutcome:
        started_at = self._now()
        log_lines = ["schema_version=1.0", f"execution_id={self._signed_task.execution_id}"]
        descriptors: list[ArtifactDescriptor] = []
        scenarios: list[_ScenarioResult] = []
        try:
            apk_inputs = tuple(
                item for item in self._signed_task.input_artifacts if item.kind == "apk"
            )
            if len(apk_inputs) != 1 or len(self._signed_task.input_artifacts) != 1:
                raise CaptureError("task_input_invalid")
            apk = await self._downloader.download(
                execution_id=self._signed_task.execution_id,
                lease_version=self._signed_task.lease_version,
                artifact=apk_inputs[0],
                target=self._workspace / "input.apk",
            )
            adb_version = "unavailable"
            try:
                adb_version = await self._device.adb_version()
                await self._device.install(apk)
            except CaptureError as error:
                completed_at = self._now()
                self._log(log_lines, f"preparation state=failed code={error.code}")
                scenarios.extend(
                    _ScenarioResult(
                        scenario_type=scenario.scenario_type,
                        state="failed",
                        started_at=started_at,
                        completed_at=completed_at,
                        temperature_start_c=None,
                        temperature_end_c=None,
                        artifact_kind=None,
                        diagnostic_code=error.code,
                    )
                    for scenario in self._signed_task.scenarios
                )
            else:
                for scenario in self._signed_task.scenarios:
                    result, descriptor = await self._capture_scenario(scenario, log_lines)
                    scenarios.append(result)
                    if descriptor is not None:
                        if descriptor.kind not in self._signed_task.allowed_uploads:
                            raise CaptureError("task_upload_not_allowed")
                        descriptors.append(descriptor)
            log_path = self._workspace / "agent.log"
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            os.chmod(log_path, 0o600)
            if "agent_log" not in self._signed_task.allowed_uploads:
                raise CaptureError("task_upload_not_allowed")
            descriptors.append(
                describe_artifact(kind="agent_log", mime="text/plain", path=log_path)
            )
            uploaded: list[UploadedArtifact] = []
            for descriptor in descriptors:
                uploaded.append(
                    await self._uploader.upload(
                        execution_id=self._signed_task.execution_id,
                        lease_version=self._signed_task.lease_version,
                        descriptor=descriptor,
                    )
                )
            uploaded_by_kind = {item.kind: item for item in uploaded}
            completed_at = self._now()
            any_completed = any(item.state == "completed" for item in scenarios)
            manifest = {
                "schema_version": "1.0",
                "execution_id": str(self._signed_task.execution_id),
                "lease_version": self._signed_task.lease_version,
                "state": "completed" if any_completed else "failed",
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "agent_version": __version__,
                "adb_version": adb_version,
                "artifacts": [
                    {
                        "artifact_id": str(item.artifact_id),
                        "kind": item.kind,
                        "mime": item.mime,
                        "size": item.size,
                        "sha256_b64": item.sha256_b64,
                    }
                    for item in uploaded
                ],
                "scenarios": [
                    {
                        "scenario_type": item.scenario_type,
                        "state": item.state,
                        "started_at": item.started_at.isoformat(),
                        "completed_at": item.completed_at.isoformat(),
                        "temperature_start_c": item.temperature_start_c,
                        "temperature_end_c": item.temperature_end_c,
                        "artifact_ids": (
                            []
                            if item.artifact_kind is None
                            else [str(uploaded_by_kind[item.artifact_kind].artifact_id)]
                        ),
                        "diagnostic_code": item.diagnostic_code,
                    }
                    for item in scenarios
                ],
                "diagnostic_code": None if any_completed else "agent_execution_failed",
            }
            return ExecutionOutcome(manifest=manifest)
        finally:
            await self._device.cleanup()
            if self._signed_task.cleanup_policy == "uninstall":
                try:
                    await self._device.uninstall(self._signed_task.package_name)
                except (CaptureError, OSError, ValueError):
                    pass
            await self._downloader.aclose()
            await self._uploader.aclose()

    async def wait(self) -> ExecutionOutcome:
        return await self._task

    def _delete_incomplete(self, *, include_log: bool) -> None:
        targets = (
            self._workspace / "input.apk",
            self._workspace / "startup.perfetto-trace",
            self._workspace / "scroll.perfetto-trace",
            self._workspace / "memory-evidence.tar",
            self._workspace / "upload-state.json",
            self._workspace / "startup.pbtxt",
            self._workspace / "scroll.pbtxt",
        )
        for target in targets:
            target.unlink(missing_ok=True)
        evidence = self._workspace / "memory-evidence"
        if evidence.is_dir() and not evidence.is_symlink():
            shutil.rmtree(evidence)
        if include_log:
            (self._workspace / "agent.log").unlink(missing_ok=True)

    async def stop(self, reason: StopReason) -> None:
        del reason
        async with self._stop_lock:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            await self._device.cleanup()
            self._delete_incomplete(include_log=False)

    async def force_stop(self) -> None:
        async with self._stop_lock:
            await self._device.cleanup()
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._delete_incomplete(include_log=False)

    async def finalize(self) -> None:
        self._delete_incomplete(include_log=True)
        try:
            self._workspace.rmdir()
        except OSError:
            pass


class CaptureTaskRunner:
    def __init__(
        self,
        *,
        config: AgentConfig,
        adb_binary: Path,
        control: object,
        state: AgentRuntimeState,
        redactor: SecretRedactor | None,
        device_factory: Callable[..., CaptureDevice] | None = None,
        downloader_factory: Callable[..., InputDownloader] | None = None,
        uploader_factory: Callable[..., MultipartUploader] | None = None,
        runner: ProcessRunner = run_process,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        del state
        self._config = config
        self._adb_binary = adb_binary
        self._control = control
        self._redactor = redactor
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._device_factory = device_factory or (
            lambda **kwargs: CaptureAdbDevice(runner=self._runner, **kwargs)
        )
        self._downloader_factory = downloader_factory or (
            lambda **kwargs: InputDownloader(control=self._control, **kwargs)
        )
        self._uploader_factory = uploader_factory or (
            lambda **kwargs: MultipartUploader(control=self._control, **kwargs)
        )

    async def start(self, task: TaskSnapshot, *, serial: str) -> CaptureExecution:
        root = self._config.workspace_root.resolve(strict=False)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise CaptureError("workspace_invalid")
        workspace = (root / str(task.execution_id)).resolve(strict=False)
        if not workspace.is_relative_to(root):
            raise CaptureError("workspace_invalid")
        workspace.mkdir(mode=0o700, exist_ok=True)
        os.chmod(workspace, 0o700)
        device = self._device_factory(
            binary=self._adb_binary,
            serial=serial,
            workspace=workspace,
        )
        downloader = self._downloader_factory(workspace_root=root)
        uploader = self._uploader_factory(checkpoint_path=workspace / "upload-state.json")
        return CaptureExecution(
            task=task,
            workspace=workspace,
            device=device,
            downloader=downloader,
            uploader=uploader,
            clock=self._clock,
            sleep=self._sleep,
            redactor=self._redactor,
        )


__all__ = [
    "CaptureAdbDevice",
    "CaptureError",
    "CaptureExecution",
    "CaptureTaskRunner",
    "ThermalReading",
    "write_memory_archive",
]
