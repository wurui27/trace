from __future__ import annotations

import io
import logging
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_agent.adb import AdbDeviceListing, ProcessResult
from perfpilot_agent.devices import AdbDeviceProbe, DeviceInventory, DeviceProbeResult
from perfpilot_agent.logging import RedactingFilter, SecretRedactor


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
