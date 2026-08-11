from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from perfpilot_agent import __version__
from perfpilot_agent.adb import (
    AdbClient,
    AdbDeviceListing,
    AdbError,
    AdbHostClient,
    AdbProtocolError,
    ProcessRunner,
    run_process,
)
from perfpilot_agent.control_client import (
    ControlClientError,
    ExecutionSlot,
    HeartbeatDevice,
    HeartbeatRequest,
    HeartbeatResponse,
    HeartbeatWorkspace,
)
from perfpilot_agent.credentials import AgentCredentials
from perfpilot_agent.logging import SecretRedactor
from perfpilot_agent.platform.base import PlatformMetadata
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding, RuntimeStateError

_BATTERY_LEVEL = re.compile(r"^\s*level:\s*(\d+)\s*$", re.MULTILINE)
_BATTERY_TEMPERATURE = re.compile(r"^\s*temperature:\s*(-?\d+)\s*$", re.MULTILINE)
_WIFI_SERIAL = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}$")


class DeviceObservation(HeartbeatDevice):
    pass


@dataclass(frozen=True, slots=True)
class DeviceProbeResult:
    manufacturer: str | None
    model: str | None
    android_release: str | None
    api_level: int | None
    boot_completed: bool
    battery_percent: int | None
    temperature_c: float | None
    storage_available_bytes: int | None
    abi: str | None
    fingerprint: str | None
    perfetto_available: bool
    property_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceInventoryItem:
    serial: str = field(repr=False)
    observation: DeviceObservation
    transport_id: str | None
    abi: str | None
    fingerprint: str | None = field(repr=False)
    perfetto_available: bool
    diagnostic_code: str | None = None


class DeviceHost(Protocol):
    async def devices(self) -> tuple[AdbDeviceListing, ...]: ...


class DeviceProbe(Protocol):
    async def __call__(self, device: AdbDeviceListing) -> DeviceProbeResult: ...


def _bounded_display(value: str, maximum: int) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise AdbProtocolError
    return normalized


def _parse_integer(value: str, *, minimum: int, maximum: int) -> int | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = int(normalized, 10)
    except ValueError:
        raise AdbProtocolError from None
    if not minimum <= parsed <= maximum:
        raise AdbProtocolError
    return parsed


def _parse_battery(payload: str) -> tuple[int | None, float | None]:
    level_match = _BATTERY_LEVEL.search(payload)
    temperature_match = _BATTERY_TEMPERATURE.search(payload)
    level = None if level_match is None else int(level_match.group(1), 10)
    temperature = None if temperature_match is None else int(temperature_match.group(1), 10) / 10
    if level is not None and not 0 <= level <= 100:
        raise AdbProtocolError
    if temperature is not None and not -100 <= temperature <= 200:
        raise AdbProtocolError
    return level, temperature


def _parse_storage(payload: str) -> int | None:
    lines = [line.split() for line in payload.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    columns = lines[-1]
    if len(columns) < 4:
        raise AdbProtocolError
    try:
        available_kib = int(columns[3], 10)
    except ValueError:
        raise AdbProtocolError from None
    if available_kib < 0:
        raise AdbProtocolError
    return available_kib * 1024


class AdbDeviceProbe:
    def __init__(
        self,
        *,
        binary: Path,
        runner: ProcessRunner = run_process,
    ) -> None:
        self._binary = binary
        self._runner = runner

    async def _text(self, client: AdbClient, *arguments: str) -> str:
        result = await client.run(*arguments)
        try:
            return result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeError:
            raise AdbProtocolError from None

    async def __call__(self, device: AdbDeviceListing) -> DeviceProbeResult:
        client = AdbClient(
            binary=self._binary,
            serial=device.serial,
            runner=self._runner,
        )
        manufacturer = _bounded_display(
            await self._text(client, "shell", "getprop", "ro.product.manufacturer"),
            128,
        )
        model = _bounded_display(
            await self._text(client, "shell", "getprop", "ro.product.model"),
            128,
        )
        android_release = _bounded_display(
            await self._text(client, "shell", "getprop", "ro.build.version.release"),
            64,
        )
        api_level = _parse_integer(
            await self._text(client, "shell", "getprop", "ro.build.version.sdk"),
            minimum=1,
            maximum=1_000,
        )
        boot_completed = (await self._text(client, "shell", "getprop", "sys.boot_completed")) == "1"
        abi = _bounded_display(
            await self._text(client, "shell", "getprop", "ro.product.cpu.abi"),
            128,
        )
        fingerprint = _bounded_display(
            await self._text(client, "shell", "getprop", "ro.build.fingerprint"),
            512,
        )
        battery = await self._text(client, "shell", "dumpsys", "battery")
        battery_percent, temperature_c = _parse_battery(battery)
        storage_available_bytes = _parse_storage(
            await self._text(client, "shell", "df", "-k", "/data")
        )
        try:
            perfetto_available = bool(await self._text(client, "shell", "which", "perfetto"))
        except AdbError:
            perfetto_available = False
        return DeviceProbeResult(
            manufacturer=manufacturer,
            model=model,
            android_release=android_release,
            api_level=api_level,
            boot_completed=boot_completed,
            battery_percent=battery_percent,
            temperature_c=temperature_c,
            storage_available_bytes=storage_available_bytes,
            abi=abi,
            fingerprint=fingerprint,
            perfetto_available=perfetto_available,
        )


def _connection_type(device: AdbDeviceListing) -> str:
    if device.usb is not None:
        return "usb"
    if _WIFI_SERIAL.fullmatch(device.serial):
        return "wifi"
    return "unknown"


class DeviceInventory:
    def __init__(
        self,
        *,
        host: DeviceHost,
        probe: DeviceProbe,
        client_ref_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._host = host
        self._probe = probe
        self._client_ref_factory = client_ref_factory
        self._client_refs: dict[str, UUID] = {}

    async def _read_one(
        self,
        device: AdbDeviceListing,
        client_ref: UUID,
    ) -> DeviceInventoryItem:
        connection_type = _connection_type(device)
        if device.adb_state != "device":
            return DeviceInventoryItem(
                serial=device.serial,
                observation=DeviceObservation(
                    client_ref=client_ref,
                    serial=device.serial,
                    manufacturer=None,
                    model=None,
                    android_release=None,
                    api_level=None,
                    connection_type=connection_type,
                    adb_state=device.adb_state,
                    battery_percent=None,
                    temperature_c=None,
                    storage_available_bytes=None,
                    property_error_code=None,
                ),
                transport_id=device.transport_id,
                abi=None,
                fingerprint=None,
                perfetto_available=False,
            )
        try:
            details = await self._probe(device)
        except (AdbError, TimeoutError, OSError, ValueError):
            return DeviceInventoryItem(
                serial=device.serial,
                observation=DeviceObservation(
                    client_ref=client_ref,
                    serial=device.serial,
                    manufacturer=None,
                    model=None,
                    android_release=None,
                    api_level=None,
                    connection_type=connection_type,
                    adb_state="offline",
                    battery_percent=None,
                    temperature_c=None,
                    storage_available_bytes=None,
                    property_error_code=None,
                ),
                transport_id=device.transport_id,
                abi=None,
                fingerprint=None,
                perfetto_available=False,
                diagnostic_code="adb_query_failed",
            )
        return DeviceInventoryItem(
            serial=device.serial,
            observation=DeviceObservation(
                client_ref=client_ref,
                serial=device.serial,
                manufacturer=details.manufacturer,
                model=details.model,
                android_release=details.android_release,
                api_level=details.api_level,
                connection_type=connection_type,
                adb_state="device" if details.boot_completed else "booting",
                battery_percent=details.battery_percent,
                temperature_c=details.temperature_c,
                storage_available_bytes=details.storage_available_bytes,
                property_error_code=details.property_error_code,
            ),
            transport_id=device.transport_id,
            abi=details.abi,
            fingerprint=details.fingerprint,
            perfetto_available=details.perfetto_available,
        )

    async def read_all(self) -> tuple[DeviceInventoryItem, ...]:
        discovered = await self._host.devices()
        current_serials = {device.serial for device in discovered}
        self._client_refs = {
            serial: client_ref
            for serial, client_ref in self._client_refs.items()
            if serial in current_serials
        }
        for device in discovered:
            if device.serial not in self._client_refs:
                self._client_refs[device.serial] = self._client_ref_factory()
        return tuple(
            await asyncio.gather(
                *(self._read_one(device, self._client_refs[device.serial]) for device in discovered)
            )
        )


class HeartbeatControl(Protocol):
    async def heartbeat(
        self,
        request: HeartbeatRequest,
        *,
        access_token: str,
    ) -> HeartbeatResponse: ...


class HeartbeatInventory(Protocol):
    async def read_all(self) -> tuple[DeviceInventoryItem, ...]: ...


class HeartbeatSourceRegistry(Protocol):
    def public_workspaces(self) -> tuple[dict[str, object], ...]: ...


class HeartbeatPublisher:
    def __init__(
        self,
        *,
        inventory: HeartbeatInventory,
        control: HeartbeatControl,
        credentials: AgentCredentials,
        metadata: PlatformMetadata,
        state: AgentRuntimeState,
        workspace_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
        redactor: SecretRedactor | None = None,
        source_registry: HeartbeatSourceRegistry | None = None,
    ) -> None:
        self._inventory = inventory
        self._control = control
        self._credentials = credentials
        self._metadata = metadata
        self._state = state
        self._workspace_root = workspace_root
        self._clock = clock
        self._disk_free = disk_free
        self._redactor = redactor
        self._source_registry = source_registry

    def _current_credentials(self) -> AgentCredentials:
        try:
            current = getattr(self._control, "credentials")
        except (AttributeError, ControlClientError):
            return self._credentials
        return current if isinstance(current, AgentCredentials) else self._credentials

    async def publish(self) -> HeartbeatResponse:
        started_at = self._clock()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise RuntimeStateError
        snapshot = await self._inventory.read_all()
        credentials = self._current_credentials()
        if self._redactor is not None:
            self._redactor.replace_live_values(
                serials={item.serial for item in snapshot},
                secrets={
                    credentials.access_token,
                    credentials.refresh_token,
                },
            )
        execution_id = self._state.execution_id
        schema_version = "1.0"
        workspaces: tuple[HeartbeatWorkspace, ...] | None = None
        if (
            self._source_registry is not None
            and getattr(credentials, "schema_version", "1.0") == "1.1"
            and getattr(credentials, "team_id", None) is not None
        ):
            schema_version = "1.1"
            try:
                workspaces = tuple(
                    HeartbeatWorkspace.model_validate(document)
                    for document in self._source_registry.public_workspaces()
                )
            except Exception:
                workspaces = ()
        source_fields: dict[str, object] = {}
        if workspaces is not None:
            source_fields["workspaces"] = workspaces
        request = HeartbeatRequest(
            schema_version=schema_version,
            agent_version=__version__,
            platform=self._metadata.platform,
            hostname=self._metadata.hostname,
            observed_at=started_at,
            clock_skew_ms=self._state.clock_skew_ms,
            disk_available_bytes=self._disk_free(self._workspace_root),
            execution_slot=ExecutionSlot(
                state="idle" if execution_id is None else "busy",
                execution_id=execution_id,
            ),
            devices=tuple(item.observation for item in snapshot),
            **source_fields,
        )
        receipt = await self._control.heartbeat(
            request,
            access_token=credentials.access_token,
        )
        snapshot_by_ref = {item.observation.client_ref: item for item in snapshot}
        receipt_by_ref = {item.client_ref: item for item in receipt.devices}
        if (
            len(snapshot_by_ref) != len(snapshot)
            or len(receipt_by_ref) != len(receipt.devices)
            or set(snapshot_by_ref) != set(receipt_by_ref)
        ):
            raise RuntimeStateError
        self._state.replace_device_bindings(
            tuple(
                DeviceBinding(
                    client_ref=client_ref,
                    device_id=receipt_by_ref[client_ref].device_id,
                    device_digest=receipt_by_ref[client_ref].device_digest,
                    serial=snapshot_by_ref[client_ref].serial,
                )
                for client_ref in snapshot_by_ref
            )
        )
        finished_at = self._clock()
        midpoint = started_at + (finished_at - started_at) / 2
        skew = round(
            (receipt.accepted_at.astimezone(UTC) - midpoint.astimezone(UTC)).total_seconds() * 1_000
        )
        self._state.set_clock_skew(max(-300_000, min(300_000, skew)))
        return receipt


def create_device_inventory(
    *,
    binary: Path,
    runner: ProcessRunner = run_process,
) -> DeviceInventory:
    return DeviceInventory(
        host=AdbHostClient(binary=binary, runner=runner),
        probe=AdbDeviceProbe(binary=binary, runner=runner),
    )


__all__ = [
    "AdbDeviceProbe",
    "DeviceInventory",
    "DeviceInventoryItem",
    "DeviceObservation",
    "DeviceProbeResult",
    "HeartbeatPublisher",
    "create_device_inventory",
]
