from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_agent.adb import AdbDeviceListing, ProcessResult
from perfpilot_agent.control_client import HeartbeatRequest, HeartbeatResponse
from perfpilot_agent.credentials import AgentCredentials
from perfpilot_agent.devices import (
    AdbDeviceProbe,
    DeviceInventory,
    DeviceProbeResult,
    HeartbeatPublisher,
)
from perfpilot_agent.logging import RedactingFilter, SecretRedactor
from perfpilot_agent.platform.base import PlatformMetadata
from perfpilot_agent.state import AgentRuntimeState

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
PROFILE_ID = UUID("94000000-0000-4000-8000-000000000001")


class FakeHost:
    async def devices(self) -> tuple[AdbDeviceListing, ...]:
        return (
            AdbDeviceListing(
                serial="good-device",
                adb_state="device",
                transport_id="1",
                usb="1-2",
            ),
            AdbDeviceListing(
                serial="broken-device",
                adb_state="device",
                transport_id="2",
                usb="1-3",
            ),
            AdbDeviceListing(
                serial="waiting-device",
                adb_state="unauthorized",
                transport_id="3",
                usb="1-4",
            ),
        )


class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, device: AdbDeviceListing) -> DeviceProbeResult:
        self.calls.append(device.serial)
        if device.serial == "broken-device":
            raise TimeoutError
        return DeviceProbeResult(
            manufacturer="Google",
            model="Pixel 7",
            android_release="14",
            api_level=34,
            boot_completed=True,
            battery_percent=82,
            temperature_c=31.5,
            storage_available_bytes=42_949_672_960,
            abi="arm64-v8a",
            fingerprint="google/panther/demo",
            perfetto_available=True,
        )


class EmptyInventory:
    async def read_all(self):
        return ()


class CapturingHeartbeatControl:
    def __init__(self) -> None:
        self.request: HeartbeatRequest | None = None

    async def heartbeat(self, request: HeartbeatRequest, *, access_token: str):
        self.request = request
        assert access_token == "ppat_" + "A" * 43
        return HeartbeatResponse(
            schema_version="1.0",
            accepted_at=NOW.isoformat(),
            next_heartbeat_seconds=10,
            devices=(),
        )


def _heartbeat_credentials(*, source_capable: bool = False) -> AgentCredentials:
    return AgentCredentials.model_construct(
        schema_version="1.1" if source_capable else "1.0",
        team_id=(
            UUID("10000000-0000-4000-8000-000000000001")
            if source_capable
            else None
        ),
        access_token="ppat_" + "A" * 43,
        refresh_token="pprt_" + "B" * 43,
    )


@pytest.mark.asyncio
async def test_one_bad_device_does_not_hide_other_devices() -> None:
    probe = FakeProbe()
    references = iter(
        [
            UUID("74000000-0000-4000-8000-000000000001"),
            UUID("74000000-0000-4000-8000-000000000002"),
            UUID("74000000-0000-4000-8000-000000000003"),
        ]
    )
    inventory = DeviceInventory(
        host=FakeHost(),
        probe=probe,
        client_ref_factory=lambda: next(references),
    )

    snapshot = await inventory.read_all()

    assert {item.observation.adb_state for item in snapshot} == {
        "device",
        "offline",
        "unauthorized",
    }
    good = next(item for item in snapshot if item.serial == "good-device")
    assert good.observation.model == "Pixel 7"
    assert good.abi == "arm64-v8a"
    broken = next(item for item in snapshot if item.serial == "broken-device")
    assert broken.observation.property_error_code is None
    assert broken.diagnostic_code == "adb_query_failed"
    assert probe.calls == ["good-device", "broken-device"]

    second = await inventory.read_all()
    assert [item.observation.client_ref for item in second] == [
        item.observation.client_ref for item in snapshot
    ]
    assert "good-device" not in repr(good)


@pytest.mark.asyncio
async def test_device_probe_reads_bounded_properties_for_one_bound_serial(
    tmp_path: Path,
) -> None:
    responses = {
        ("shell", "getprop", "ro.product.manufacturer"): "Google\n",
        ("shell", "getprop", "ro.product.model"): "Pixel 7\n",
        ("shell", "getprop", "ro.build.version.release"): "14\n",
        ("shell", "getprop", "ro.build.version.sdk"): "34\n",
        ("shell", "getprop", "sys.boot_completed"): "1\n",
        ("shell", "getprop", "ro.product.cpu.abi"): "arm64-v8a\n",
        ("shell", "getprop", "ro.build.fingerprint"): "google/panther/demo\n",
        ("shell", "dumpsys", "battery"): "level: 82\ntemperature: 315\n",
        ("shell", "df", "-k", "/data"): (
            "Filesystem 1K-blocks Used Available Use% Mounted on\n"
            "/dev/block/data 100000 50000 41943040 50% /data\n"
        ),
        ("shell", "which", "perfetto"): "/system/bin/perfetto\n",
    }
    calls: list[list[str]] = []

    async def runner(argv, *, timeout_seconds, maximum_output_bytes):
        calls.append(argv)
        key = tuple(argv[3:])
        return ProcessResult(
            returncode=0,
            stdout=responses[key].encode("utf-8"),
            stderr=b"",
        )

    binary = tmp_path / "adb"
    binary.write_bytes(b"fake")
    binary.chmod(0o755)
    result = await AdbDeviceProbe(binary=binary, runner=runner)(
        AdbDeviceListing(
            serial="R3CN30SECRET",
            adb_state="device",
            transport_id="1",
            usb="1-2",
        )
    )

    assert result.api_level == 34
    assert result.temperature_c == 31.5
    assert result.storage_available_bytes == 42_949_672_960
    assert result.perfetto_available is True
    assert all(call[1:3] == ["-s", "R3CN30SECRET"] for call in calls)


def test_logging_filter_redacts_live_serials_tokens_registration_codes_and_queries() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("perfpilot-agent-redaction-test")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    redactor = SecretRedactor()
    redactor.replace_live_values(
        serials={"R3CN30SECRET"},
        secrets={"ppat_" + "A" * 43, "pprt_" + "B" * 43},
    )
    handler.addFilter(RedactingFilter(redactor))
    logger.addHandler(handler)

    logger.info(
        "serial=%s access=%s refresh=%s code=%s url=%s",
        "R3CN30SECRET",
        "ppat_" + "A" * 43,
        "pprt_" + "B" * 43,
        "ppreg_" + "C" * 43,
        "https://objects.example/part?X-Amz-Signature=secret",
    )
    try:
        raise RuntimeError("device R3CN30SECRET failed")
    except RuntimeError:
        logger.exception("probe failed")

    rendered = stream.getvalue()
    assert "R3CN30SECRET" not in rendered
    assert "ppat_" not in rendered
    assert "pprt_" not in rendered
    assert "ppreg_" not in rendered
    assert "X-Amz-Signature" not in rendered
    assert rendered.count("[redacted]") >= 5


@pytest.mark.asyncio
async def test_heartbeat_publishes_only_public_source_workspace_metadata(tmp_path: Path) -> None:
    private_path = tmp_path / "private-source"

    class PublicRegistry:
        def public_workspaces(self):
            return (
                {
                    "workspace_id": str(WORKSPACE_ID),
                    "name": "Demo Android",
                    "state": "ready",
                    "git_branch": "main",
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 1,
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [
                        {"profile_id": str(PROFILE_ID), "name": "Android check"}
                    ],
                },
            )

    control = CapturingHeartbeatControl()
    publisher = HeartbeatPublisher(
        inventory=EmptyInventory(),
        control=control,
        credentials=_heartbeat_credentials(source_capable=True),
        metadata=PlatformMetadata(platform="linux", hostname="test", os_version="test"),
        state=AgentRuntimeState(),
        workspace_root=tmp_path,
        source_registry=PublicRegistry(),
        clock=lambda: NOW,
        disk_free=lambda _path: 1,
    )

    await publisher.publish()

    assert control.request is not None
    assert control.request.schema_version == "1.1"
    assert control.request.model_dump(mode="json")["workspaces"] == [
        {
            "workspace_id": str(WORKSPACE_ID),
            "name": "Demo Android",
            "state": "ready",
            "git_branch": "main",
            "git_head": "a" * 40,
            "tracked_dirty_count": 1,
            "snapshot_policy": "tracked_worktree",
            "validation_profiles": [
                {"profile_id": str(PROFILE_ID), "name": "Android check"}
            ],
        }
    ]
    rendered = repr(control.request)
    assert str(private_path) not in rendered
    assert "gradlew" not in rendered
    assert "remote" not in rendered


@pytest.mark.asyncio
async def test_source_registry_failure_does_not_break_device_heartbeat_or_leak_path(
    tmp_path: Path,
    caplog,
) -> None:
    private_path = tmp_path / "private-source"

    class BrokenRegistry:
        def public_workspaces(self):
            raise RuntimeError(f"cannot inspect {private_path}")

    control = CapturingHeartbeatControl()
    publisher = HeartbeatPublisher(
        inventory=EmptyInventory(),
        control=control,
        credentials=_heartbeat_credentials(source_capable=True),
        metadata=PlatformMetadata(platform="linux", hostname="test", os_version="test"),
        state=AgentRuntimeState(),
        workspace_root=tmp_path,
        source_registry=BrokenRegistry(),
        clock=lambda: NOW,
        disk_free=lambda _path: 1,
    )

    receipt = await publisher.publish()

    assert receipt.devices == ()
    assert control.request is not None
    assert control.request.schema_version == "1.1"
    assert control.request.workspaces == ()
    assert str(private_path) not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_without_source_registry_remains_schema_1_0(tmp_path: Path) -> None:
    control = CapturingHeartbeatControl()
    publisher = HeartbeatPublisher(
        inventory=EmptyInventory(),
        control=control,
        credentials=_heartbeat_credentials(),
        metadata=PlatformMetadata(platform="linux", hostname="test", os_version="test"),
        state=AgentRuntimeState(),
        workspace_root=tmp_path,
        clock=lambda: NOW,
        disk_free=lambda _path: 1,
    )

    await publisher.publish()

    assert control.request is not None
    assert control.request.schema_version == "1.0"
    assert "workspaces" not in control.request.model_fields_set
    assert control.request.model_dump(mode="json", exclude={"workspaces"})[
        "schema_version"
    ] == "1.0"


@pytest.mark.asyncio
async def test_heartbeat_missing_constructed_schema_version_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    class UnexpectedRegistry:
        def public_workspaces(self):
            raise AssertionError("legacy credentials must not publish source workspaces")

    control = CapturingHeartbeatControl()
    publisher = HeartbeatPublisher(
        inventory=EmptyInventory(),
        control=control,
        credentials=AgentCredentials.model_construct(
            access_token="ppat_" + "A" * 43,
            refresh_token="pprt_" + "B" * 43,
        ),
        metadata=PlatformMetadata(platform="linux", hostname="test", os_version="test"),
        state=AgentRuntimeState(),
        workspace_root=tmp_path,
        source_registry=UnexpectedRegistry(),
        clock=lambda: NOW,
        disk_free=lambda _path: 1,
    )

    await publisher.publish()

    assert control.request is not None
    assert control.request.schema_version == "1.0"
    assert "workspaces" not in control.request.model_fields_set
