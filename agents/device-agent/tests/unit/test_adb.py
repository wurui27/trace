from __future__ import annotations

from pathlib import Path

import pytest

from perfpilot_agent.adb import (
    AdbClient,
    AdbCommandFailed,
    AdbUnavailable,
    ProcessResult,
    parse_devices,
    resolve_adb,
)


class FakeRunner:
    def __init__(self, result: ProcessResult | None = None) -> None:
        self.calls: list[tuple[list[str], float, int]] = []
        self.result = result or ProcessResult(
            returncode=0,
            stdout=b"Android Debug Bridge version 1.0.41\n",
            stderr=b"",
        )

    async def __call__(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
    ) -> ProcessResult:
        self.calls.append((argv, timeout_seconds, maximum_output_bytes))
        return self.result


@pytest.mark.asyncio
async def test_every_device_command_is_serial_bound(tmp_path: Path) -> None:
    adb_binary = tmp_path / "adb"
    adb_binary.write_bytes(b"fake")
    adb_binary.chmod(0o755)
    runner = FakeRunner(ProcessResult(returncode=0, stdout=b"demo\n", stderr=b""))
    adb = AdbClient(binary=adb_binary, serial="R3CN30SECRET", runner=runner)

    await adb.run("shell", "getprop", "ro.product.model")

    assert runner.calls == [
        (
            [
                str(adb_binary),
                "-s",
                "R3CN30SECRET",
                "shell",
                "getprop",
                "ro.product.model",
            ],
            5.0,
            256 * 1024,
        )
    ]


@pytest.mark.asyncio
async def test_device_client_rejects_arbitrary_shell_commands(tmp_path: Path) -> None:
    adb_binary = tmp_path / "adb"
    adb_binary.write_bytes(b"fake")
    adb_binary.chmod(0o755)
    runner = FakeRunner()
    adb = AdbClient(binary=adb_binary, serial="R3CN30SECRET", runner=runner)

    with pytest.raises(AdbCommandFailed):
        await adb.run("shell", "sh", "-c", "rm -rf /data")

    assert runner.calls == []


def test_parse_devices_keeps_independent_adb_states() -> None:
    fixture = (Path(__file__).parents[1] / "fixtures" / "adb" / "devices-l.txt").read_bytes()

    devices = parse_devices(fixture)

    assert [(item.serial, item.adb_state) for item in devices] == [
        ("R3CN30SECRET", "device"),
        ("192.168.50.8:5555", "unauthorized"),
        ("emulator-5554", "offline"),
    ]
    assert devices[0].transport_id == "1"
    assert devices[0].usb == "1-2"


@pytest.mark.asyncio
async def test_resolve_adb_prefers_configured_binary_and_rejects_workspace_binary(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    configured = tmp_path / "sdk" / "platform-tools" / "adb"
    configured.parent.mkdir(parents=True)
    configured.write_bytes(b"fake")
    configured.chmod(0o755)
    runner = FakeRunner()

    resolved = await resolve_adb(
        configured=configured,
        workspace_root=workspace,
        runner=runner,
        environ={},
        path_lookup=lambda _name: None,
        installer_candidates=(),
    )

    assert resolved == configured.resolve()
    assert runner.calls[0][0] == [str(configured.resolve()), "version"]

    unsafe = workspace / "adb"
    unsafe.write_bytes(b"fake")
    unsafe.chmod(0o755)
    with pytest.raises(AdbUnavailable):
        await resolve_adb(
            configured=unsafe,
            workspace_root=workspace,
            runner=runner,
            environ={},
            path_lookup=lambda _name: None,
            installer_candidates=(),
        )
