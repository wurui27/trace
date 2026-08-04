"""Loopback ADB device discovery for the local PerfPilot runtime."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol


LocalDeviceState = Literal[
    "connected",
    "disconnected",
    "multiple",
    "unauthorized",
    "unavailable",
]

_SERIAL = re.compile(r"^[^\s\x00-\x1f\x7f]{1,255}$")
_MAX_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class LocalDevice:
    serial: str
    manufacturer: str
    model: str
    android_version: str
    api_level: int | None


@dataclass(frozen=True, slots=True)
class LocalDeviceStatus:
    state: LocalDeviceState
    device: LocalDevice | None


class LocalDeviceProbe(Protocol):
    async def inspect(self) -> LocalDeviceStatus: ...


@dataclass(frozen=True, slots=True)
class _AdbEntry:
    serial: str
    state: str
    model: str


def _clean(value: str, *, maximum: int = 128) -> str:
    return "".join(character for character in value.strip() if character.isprintable())[
        :maximum
    ]


def _entries(output: str) -> list[_AdbEntry]:
    entries: list[_AdbEntry] = []
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) < 2 or fields[0] == "List" or _SERIAL.fullmatch(fields[0]) is None:
            continue
        metadata = {
            key: value
            for field in fields[2:]
            if ":" in field
            for key, value in [field.split(":", 1)]
        }
        entries.append(
            _AdbEntry(
                serial=fields[0],
                state=fields[1],
                model=_clean(metadata.get("model", "")),
            )
        )
    return entries


class AdbDeviceProbe:
    def __init__(
        self,
        *,
        adb_path: str | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("ADB timeout must be positive")
        self.adb_path = adb_path or os.getenv("PERFPILOT_LOCAL_ADB", "adb")
        self.timeout_seconds = timeout_seconds

    async def _run(self, *arguments: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.adb_path,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return None
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return None
        if process.returncode != 0 or len(stdout) > _MAX_OUTPUT_BYTES:
            return None
        try:
            return stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None

    async def _property(self, serial: str, name: str) -> str:
        value = await self._run("-s", serial, "shell", "getprop", name)
        return _clean(value or "")

    async def inspect(self) -> LocalDeviceStatus:
        raw = await self._run("devices", "-l")
        if raw is None:
            return LocalDeviceStatus(state="unavailable", device=None)
        entries = _entries(raw)
        connected = [entry for entry in entries if entry.state == "device"]
        if len(connected) > 1:
            return LocalDeviceStatus(state="multiple", device=None)
        if not connected:
            state: LocalDeviceState = (
                "unauthorized"
                if any(entry.state == "unauthorized" for entry in entries)
                else "disconnected"
            )
            return LocalDeviceStatus(state=state, device=None)
        entry = connected[0]
        manufacturer, model, release, api_level = await asyncio.gather(
            self._property(entry.serial, "ro.product.manufacturer"),
            self._property(entry.serial, "ro.product.model"),
            self._property(entry.serial, "ro.build.version.release"),
            self._property(entry.serial, "ro.build.version.sdk"),
        )
        try:
            parsed_api_level = int(api_level) if api_level else None
        except ValueError:
            parsed_api_level = None
        return LocalDeviceStatus(
            state="connected",
            device=LocalDevice(
                serial=entry.serial,
                manufacturer=manufacturer,
                model=model or entry.model or entry.serial,
                android_version=release,
                api_level=parsed_api_level,
            ),
        )


__all__ = [
    "AdbDeviceProbe",
    "LocalDevice",
    "LocalDeviceProbe",
    "LocalDeviceState",
    "LocalDeviceStatus",
]
