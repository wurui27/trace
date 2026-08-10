from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import (
    Agent as StoredAgent,
    AgentLease,
    Device as StoredDeviceModel,
)
from perfpilot_api.services.agents import AgentPlatform, AgentRepository
from perfpilot_api.services.source_workspaces import (
    SourceAgentCapabilityRecord,
    is_public_source_display_name,
)

ConnectionType = Literal["usb", "wifi", "unknown"]
AdbState = Literal["device", "unauthorized", "offline", "booting"]
DeviceState = Literal[
    "ready",
    "busy",
    "unauthorized",
    "booting",
    "quarantined",
    "offline",
]
ExecutionState = Literal["idle", "busy"]

_STALE_AFTER = timedelta(seconds=30)
_MAXIMUM_DEVICES_PER_HEARTBEAT = 32
_MAXIMUM_BROWSER_DEVICES = 256
_PROPERTY_ERROR_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_AGENT_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_STATE_ORDER = {
    "ready": 0,
    "busy": 1,
    "unauthorized": 2,
    "booting": 3,
    "quarantined": 4,
    "offline": 5,
}


class DeviceHeartbeatRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Device heartbeat was rejected")


@dataclass(frozen=True, slots=True)
class AgentHeartbeat:
    agent_version: str
    platform: AgentPlatform
    hostname: str
    observed_at: datetime
    clock_skew_ms: int
    disk_available_bytes: int
    execution_state: ExecutionState
    execution_id: UUID | None
    source_workspaces: tuple[dict[str, object], ...] | None = None


@dataclass(frozen=True, slots=True)
class SanitizedDeviceObservation:
    client_ref: UUID
    serial_digest: str = dataclass_field(repr=False)
    serial_suffix: str
    manufacturer: str | None
    model: str | None
    android_release: str | None
    api_level: int | None
    connection_type: ConnectionType
    adb_state: AdbState
    battery_percent: int | None
    temperature_c: Decimal | None
    storage_available_bytes: int | None
    property_error_code: str | None


@dataclass(frozen=True, slots=True)
class StoredDevice:
    device_id: UUID
    team_id: UUID
    agent_id: UUID
    agent_name: str
    serial_digest: str = dataclass_field(repr=False)
    serial_suffix: str
    manufacturer: str | None
    model: str | None
    android_release: str | None
    api_level: int | None
    connection_type: ConnectionType
    adb_state: AdbState
    state: DeviceState
    battery_percent: int | None
    temperature_c: Decimal | None
    storage_available_bytes: int | None
    property_error_code: str | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class HeartbeatDeviceReceipt:
    client_ref: UUID
    device_id: UUID
    device_digest: str = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class HeartbeatReceipt:
    accepted_at: datetime
    next_heartbeat_seconds: int
    devices: tuple[HeartbeatDeviceReceipt, ...]


@dataclass(frozen=True, slots=True)
class DeviceView:
    device_id: UUID
    agent_id: UUID
    agent_name: str
    serial_suffix: str
    manufacturer: str | None
    model: str | None
    android_release: str | None
    api_level: int | None
    connection_type: ConnectionType
    adb_state: AdbState
    state: DeviceState
    last_seen_at: datetime | None


class DeviceDirectoryRepository(Protocol):
    async def replace_snapshot(
        self,
        *,
        agent_id: UUID,
        heartbeat: AgentHeartbeat,
        devices: tuple[SanitizedDeviceObservation, ...],
        now: datetime,
    ) -> tuple[StoredDevice, ...]: ...

    async def list_team(self, team_id: UUID) -> tuple[StoredDevice, ...]: ...

    async def expire_stale(
        self,
        *,
        cutoff: datetime,
        now: datetime,
    ) -> int: ...

    async def list_source_agents(
        self, team_id: UUID
    ) -> tuple[SourceAgentCapabilityRecord, ...]: ...

    async def get_source_agent(
        self, agent_id: UUID
    ) -> SourceAgentCapabilityRecord | None: ...


class InMemoryDeviceDirectoryRepository:
    def __init__(
        self,
        agent_repository: AgentRepository,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._agent_repository = agent_repository
        self._uuid_factory = uuid_factory
        self._devices: dict[UUID, StoredDevice] = {}
        self._agent_heartbeats: dict[UUID, datetime] = {}
        self._agent_capabilities: dict[UUID, dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def replace_snapshot(
        self,
        *,
        agent_id: UUID,
        heartbeat: AgentHeartbeat,
        devices: tuple[SanitizedDeviceObservation, ...],
        now: datetime,
    ) -> tuple[StoredDevice, ...]:
        agent = await self._agent_repository.get_refresh_candidate(agent_id)
        if agent is None or agent.state in {"pending", "revoked"}:
            raise DeviceHeartbeatRejected
        async with self._lock:
            by_digest = {device.serial_digest: device for device in self._devices.values()}
            accepted: list[StoredDevice] = []
            submitted_digests = {device.serial_digest for device in devices}
            for observation in devices:
                existing = by_digest.get(observation.serial_digest)
                if existing is not None and existing.team_id != agent.team_id:
                    raise DeviceHeartbeatRejected
                stored = StoredDevice(
                    device_id=(
                        existing.device_id if existing is not None else self._uuid_factory()
                    ),
                    team_id=agent.team_id,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    serial_digest=observation.serial_digest,
                    serial_suffix=observation.serial_suffix,
                    manufacturer=observation.manufacturer,
                    model=observation.model,
                    android_release=observation.android_release,
                    api_level=observation.api_level,
                    connection_type=observation.connection_type,
                    adb_state=observation.adb_state,
                    state=_state_from_observation(observation),
                    battery_percent=observation.battery_percent,
                    temperature_c=observation.temperature_c,
                    storage_available_bytes=observation.storage_available_bytes,
                    property_error_code=observation.property_error_code,
                    last_seen_at=now,
                )
                self._devices[stored.device_id] = stored
                accepted.append(stored)
            for device_id, stored in tuple(self._devices.items()):
                if stored.agent_id == agent_id and stored.serial_digest not in submitted_digests:
                    self._devices[device_id] = replace(
                        stored,
                        adb_state="offline",
                        state="offline",
                    )
            self._agent_heartbeats[agent_id] = now
            capabilities = _heartbeat_capabilities(heartbeat)
            self._agent_capabilities[agent_id] = capabilities
            return tuple(accepted)

    async def list_source_agents(
        self, team_id: UUID
    ) -> tuple[SourceAgentCapabilityRecord, ...]:
        agents = await self._agent_repository.list_team(team_id)
        async with self._lock:
            return tuple(
                SourceAgentCapabilityRecord(
                    agent_id=agent.id,
                    team_id=agent.team_id,
                    name=agent.name,
                    state=("online" if agent.id in self._agent_heartbeats else agent.state),
                    capabilities=dict(self._agent_capabilities.get(agent.id, {})),
                )
                for agent in agents
            )

    async def get_source_agent(
        self, agent_id: UUID
    ) -> SourceAgentCapabilityRecord | None:
        agent = await self._agent_repository.get_refresh_candidate(agent_id)
        if agent is None:
            return None
        async with self._lock:
            return SourceAgentCapabilityRecord(
                agent_id=agent.id,
                team_id=agent.team_id,
                name=agent.name,
                state=("online" if agent.id in self._agent_heartbeats else agent.state),
                capabilities=dict(self._agent_capabilities.get(agent.id, {})),
            )

    async def list_team(self, team_id: UUID) -> tuple[StoredDevice, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (device for device in self._devices.values() if device.team_id == team_id),
                    key=lambda device: (
                        _STATE_ORDER[device.state],
                        device.agent_name.casefold(),
                        (device.model or "").casefold(),
                        str(device.device_id),
                    ),
                )[:_MAXIMUM_BROWSER_DEVICES]
            )

    async def expire_stale(
        self,
        *,
        cutoff: datetime,
        now: datetime,
    ) -> int:
        del now
        async with self._lock:
            stale_agent_ids = {
                agent_id
                for agent_id, last_seen_at in self._agent_heartbeats.items()
                if last_seen_at <= cutoff
            }
            for device_id, stored in tuple(self._devices.items()):
                if stored.agent_id in stale_agent_ids:
                    self._devices[device_id] = replace(
                        stored,
                        adb_state="offline",
                        state="offline",
                    )
            for agent_id in stale_agent_ids:
                self._agent_heartbeats.pop(agent_id, None)
            return len(stale_agent_ids)


class SQLAlchemyDeviceDirectoryRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def replace_snapshot(
        self,
        *,
        agent_id: UUID,
        heartbeat: AgentHeartbeat,
        devices: tuple[SanitizedDeviceObservation, ...],
        now: datetime,
    ) -> tuple[StoredDevice, ...]:
        incoming_digests = tuple(device.serial_digest for device in devices)
        try:
            async with self._session_factory() as session, session.begin():
                agent = await session.scalar(
                    select(StoredAgent).where(StoredAgent.id == agent_id).with_for_update()
                )
                if agent is None or agent.state in {"pending", "revoked"}:
                    raise DeviceHeartbeatRejected
                device_filter = StoredDeviceModel.agent_id == agent_id
                if incoming_digests:
                    device_filter = or_(
                        device_filter,
                        StoredDeviceModel.serial_digest.in_(incoming_digests),
                    )
                existing_devices = list(
                    (
                        await session.scalars(
                            select(StoredDeviceModel).where(device_filter).with_for_update()
                        )
                    ).all()
                )
                by_digest = {stored.serial_digest: stored for stored in existing_devices}
                if any(
                    stored.team_id != agent.team_id
                    for digest, stored in by_digest.items()
                    if digest in incoming_digests
                ):
                    raise DeviceHeartbeatRejected

                incoming_existing_ids = tuple(
                    stored.id for digest, stored in by_digest.items() if digest in incoming_digests
                )
                leased_device_ids: set[UUID] = set()
                if incoming_existing_ids:
                    leased_device_ids = set(
                        (
                            await session.scalars(
                                select(AgentLease.device_id)
                                .where(
                                    AgentLease.device_id.in_(incoming_existing_ids),
                                    AgentLease.state.in_(("active", "cancel_requested")),
                                    AgentLease.expires_at > now,
                                )
                                .with_for_update()
                            )
                        ).all()
                    )
                if any(
                    stored.agent_id != agent_id and stored.id in leased_device_ids
                    for digest, stored in by_digest.items()
                    if digest in incoming_digests
                ):
                    raise DeviceHeartbeatRejected

                accepted: list[StoredDeviceModel] = []
                for observation in devices:
                    stored = by_digest.get(observation.serial_digest)
                    if stored is None:
                        stored = StoredDeviceModel(
                            team_id=agent.team_id,
                            agent_id=agent_id,
                            serial_digest=observation.serial_digest,
                            serial_suffix=observation.serial_suffix,
                            connection_type=observation.connection_type,
                            adb_state=observation.adb_state,
                            state=_state_from_observation(observation),
                        )
                        session.add(stored)
                        by_digest[observation.serial_digest] = stored
                    stored.agent_id = agent_id
                    stored.serial_suffix = observation.serial_suffix
                    stored.manufacturer = observation.manufacturer
                    stored.model = observation.model
                    stored.android_release = observation.android_release
                    stored.api_level = observation.api_level
                    stored.connection_type = observation.connection_type
                    stored.adb_state = observation.adb_state
                    stored.state = _state_from_observation(
                        observation,
                        leased=stored.id in leased_device_ids,
                    )
                    stored.battery_percent = observation.battery_percent
                    stored.temperature_c = observation.temperature_c
                    stored.storage_available_bytes = observation.storage_available_bytes
                    stored.last_property_error_code = observation.property_error_code
                    stored.last_seen_at = now
                    stored.updated_at = now
                    accepted.append(stored)

                submitted_digests = set(incoming_digests)
                for stored in existing_devices:
                    if (
                        stored.agent_id == agent_id
                        and stored.serial_digest not in submitted_digests
                    ):
                        stored.adb_state = "offline"
                        stored.state = "offline"
                        stored.updated_at = now

                agent.platform = heartbeat.platform
                agent.agent_version = heartbeat.agent_version
                agent.hostname = heartbeat.hostname
                agent.state = "online"
                agent.last_heartbeat_at = now
                agent.capabilities = _heartbeat_capabilities(heartbeat)
                agent.updated_at = now
                await session.flush()
                return tuple(
                    _stored_device_record(stored, agent_name=agent.name) for stored in accepted
                )
        except IntegrityError:
            raise DeviceHeartbeatRejected from None

    async def list_team(self, team_id: UUID) -> tuple[StoredDevice, ...]:
        state_order = case(
            *(
                (StoredDeviceModel.state == state, position)
                for state, position in _STATE_ORDER.items()
            ),
            else_=len(_STATE_ORDER),
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(StoredDeviceModel, StoredAgent.name)
                    .join(StoredAgent, StoredAgent.id == StoredDeviceModel.agent_id)
                    .where(StoredDeviceModel.team_id == team_id)
                    .order_by(
                        state_order,
                        StoredAgent.name,
                        StoredDeviceModel.model,
                        StoredDeviceModel.id,
                    )
                    .limit(_MAXIMUM_BROWSER_DEVICES)
                )
            ).all()
            return tuple(
                _stored_device_record(stored, agent_name=agent_name) for stored, agent_name in rows
            )

    async def expire_stale(
        self,
        *,
        cutoff: datetime,
        now: datetime,
    ) -> int:
        async with self._session_factory() as session, session.begin():
            stale_agents = list(
                (
                    await session.scalars(
                        select(StoredAgent)
                        .where(
                            StoredAgent.state != "revoked",
                            StoredAgent.last_heartbeat_at.is_not(None),
                            StoredAgent.last_heartbeat_at <= cutoff,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not stale_agents:
                return 0
            agent_ids = tuple(agent.id for agent in stale_agents)
            for agent in stale_agents:
                agent.state = "offline"
                agent.updated_at = now
            await session.execute(
                update(StoredDeviceModel)
                .where(StoredDeviceModel.agent_id.in_(agent_ids))
                .values(adb_state="offline", state="offline", updated_at=now)
            )
            return len(stale_agents)

    async def list_source_agents(
        self, team_id: UUID
    ) -> tuple[SourceAgentCapabilityRecord, ...]:
        async with self._session_factory() as session:
            agents = (
                await session.scalars(
                    select(StoredAgent)
                    .where(StoredAgent.team_id == team_id)
                    .order_by(StoredAgent.name, StoredAgent.id)
                )
            ).all()
            return tuple(
                SourceAgentCapabilityRecord(
                    agent_id=agent.id,
                    team_id=agent.team_id,
                    name=agent.name,
                    state=agent.state,
                    capabilities=dict(agent.capabilities),
                )
                for agent in agents
            )

    async def get_source_agent(
        self, agent_id: UUID
    ) -> SourceAgentCapabilityRecord | None:
        async with self._session_factory() as session:
            agent = await session.get(StoredAgent, agent_id)
            if agent is None:
                return None
            return SourceAgentCapabilityRecord(
                agent_id=agent.id,
                team_id=agent.team_id,
                name=agent.name,
                state=agent.state,
                capabilities=dict(agent.capabilities),
            )


class DeviceDirectory:
    def __init__(
        self,
        *,
        repository: DeviceDirectoryRepository,
        serial_hmac_key: bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(serial_hmac_key, bytes) or len(serial_hmac_key) < 32:
            raise ValueError("Device serial key is invalid")
        self._repository = repository
        self._serial_hmac_key = serial_hmac_key
        self._clock = clock

    def __repr__(self) -> str:
        return "DeviceDirectory()"

    def sanitize_observation(
        self,
        *,
        client_ref: UUID,
        serial: str,
        manufacturer: str | None,
        model: str | None,
        android_release: str | None,
        api_level: int | None,
        connection_type: ConnectionType,
        adb_state: AdbState,
        battery_percent: int | None,
        temperature_c: Decimal | None,
        storage_available_bytes: int | None,
        property_error_code: str | None,
    ) -> SanitizedDeviceObservation:
        try:
            if (
                not isinstance(client_ref, UUID)
                or not isinstance(serial, str)
                or not 1 <= len(serial) <= 255
                or any(not 0x21 <= ord(character) <= 0x7E for character in serial)
            ):
                raise DeviceHeartbeatRejected
            _validate_optional_display(manufacturer, maximum=128)
            _validate_optional_display(model, maximum=128)
            _validate_optional_display(android_release, maximum=64)
            if api_level is not None and (
                isinstance(api_level, bool)
                or not isinstance(api_level, int)
                or not 1 <= api_level <= 1000
            ):
                raise DeviceHeartbeatRejected
            if connection_type not in {"usb", "wifi", "unknown"}:
                raise DeviceHeartbeatRejected
            if adb_state not in {"device", "unauthorized", "offline", "booting"}:
                raise DeviceHeartbeatRejected
            if battery_percent is not None and (
                isinstance(battery_percent, bool)
                or not isinstance(battery_percent, int)
                or not 0 <= battery_percent <= 100
            ):
                raise DeviceHeartbeatRejected
            if temperature_c is not None and (
                not isinstance(temperature_c, Decimal)
                or not temperature_c.is_finite()
                or not Decimal("-100") <= temperature_c <= Decimal("200")
            ):
                raise DeviceHeartbeatRejected
            if storage_available_bytes is not None and (
                isinstance(storage_available_bytes, bool)
                or not isinstance(storage_available_bytes, int)
                or not 0 <= storage_available_bytes <= 2**63 - 1
            ):
                raise DeviceHeartbeatRejected
            if property_error_code is not None and (
                _PROPERTY_ERROR_PATTERN.fullmatch(property_error_code) is None
            ):
                raise DeviceHeartbeatRejected
            digest = hmac.new(
                self._serial_hmac_key,
                serial.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            return SanitizedDeviceObservation(
                client_ref=client_ref,
                serial_digest=digest,
                serial_suffix=serial[-4:],
                manufacturer=manufacturer,
                model=model,
                android_release=android_release,
                api_level=api_level,
                connection_type=connection_type,
                adb_state=adb_state,
                battery_percent=battery_percent,
                temperature_c=temperature_c,
                storage_available_bytes=storage_available_bytes,
                property_error_code=property_error_code,
            )
        except (ArithmeticError, UnicodeError, ValueError):
            raise DeviceHeartbeatRejected from None

    async def replace_heartbeat(
        self,
        *,
        agent_id: UUID,
        heartbeat: AgentHeartbeat,
        devices: Sequence[SanitizedDeviceObservation],
    ) -> HeartbeatReceipt:
        now = _aware_utc(self._clock())
        _validate_heartbeat(heartbeat)
        snapshot = tuple(devices)
        if len(snapshot) > _MAXIMUM_DEVICES_PER_HEARTBEAT:
            raise DeviceHeartbeatRejected
        client_refs = {device.client_ref for device in snapshot}
        digests = {device.serial_digest for device in snapshot}
        if len(client_refs) != len(snapshot) or len(digests) != len(snapshot):
            raise DeviceHeartbeatRejected
        accepted = await self._repository.replace_snapshot(
            agent_id=agent_id,
            heartbeat=heartbeat,
            devices=snapshot,
            now=now,
        )
        accepted_by_digest = {device.serial_digest: device for device in accepted}
        if set(accepted_by_digest) != digests:
            raise RuntimeError("Device repository returned an invalid snapshot")
        return HeartbeatReceipt(
            accepted_at=now,
            next_heartbeat_seconds=10,
            devices=tuple(
                HeartbeatDeviceReceipt(
                    client_ref=observation.client_ref,
                    device_id=accepted_by_digest[observation.serial_digest].device_id,
                    device_digest=observation.serial_digest,
                )
                for observation in snapshot
            ),
        )

    async def list_devices(self, *, team_id: UUID) -> tuple[DeviceView, ...]:
        now = _aware_utc(self._clock())
        await self._repository.expire_stale(
            cutoff=now - _STALE_AFTER,
            now=now,
        )
        return tuple(
            DeviceView(
                device_id=device.device_id,
                agent_id=device.agent_id,
                agent_name=device.agent_name,
                serial_suffix=device.serial_suffix,
                manufacturer=device.manufacturer,
                model=device.model,
                android_release=device.android_release,
                api_level=device.api_level,
                connection_type=device.connection_type,
                adb_state=device.adb_state,
                state=device.state,
                last_seen_at=device.last_seen_at,
            )
            for device in await self._repository.list_team(team_id)
        )

    async def expire_stale(self) -> int:
        now = _aware_utc(self._clock())
        return await self._repository.expire_stale(
            cutoff=now - _STALE_AFTER,
            now=now,
        )

    async def list_source_agents(
        self, team_id: UUID
    ) -> tuple[SourceAgentCapabilityRecord, ...]:
        await self.expire_stale()
        return await self._repository.list_source_agents(team_id)

    async def get_source_agent(
        self, agent_id: UUID
    ) -> SourceAgentCapabilityRecord | None:
        await self.expire_stale()
        return await self._repository.get_source_agent(agent_id)


def _validate_optional_display(value: str | None, *, maximum: int) -> None:
    if value is not None and (
        not isinstance(value, str)
        or len(value) > maximum
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise DeviceHeartbeatRejected


def _validate_heartbeat(heartbeat: AgentHeartbeat) -> None:
    if (
        not isinstance(heartbeat.agent_version, str)
        or not 1 <= len(heartbeat.agent_version) <= 64
        or _AGENT_VERSION_PATTERN.fullmatch(heartbeat.agent_version) is None
        or heartbeat.platform not in {"macos", "windows", "linux"}
        or not isinstance(heartbeat.hostname, str)
        or not 1 <= len(heartbeat.hostname) <= 200
        or any(unicodedata.category(character) == "Cc" for character in heartbeat.hostname)
        or not isinstance(heartbeat.observed_at, datetime)
        or heartbeat.observed_at.tzinfo is None
        or isinstance(heartbeat.clock_skew_ms, bool)
        or not isinstance(heartbeat.clock_skew_ms, int)
        or not -300_000 <= heartbeat.clock_skew_ms <= 300_000
        or isinstance(heartbeat.disk_available_bytes, bool)
        or not isinstance(heartbeat.disk_available_bytes, int)
        or not 0 <= heartbeat.disk_available_bytes <= 2**63 - 1
        or heartbeat.execution_state not in {"idle", "busy"}
        or (heartbeat.execution_id is not None and not isinstance(heartbeat.execution_id, UUID))
        or (heartbeat.execution_state == "idle" and heartbeat.execution_id is not None)
        or (heartbeat.execution_state == "busy" and heartbeat.execution_id is None)
    ):
        raise DeviceHeartbeatRejected
    _validate_source_workspaces(heartbeat.source_workspaces)


def _heartbeat_capabilities(heartbeat: AgentHeartbeat) -> dict[str, object]:
    capabilities: dict[str, object] = {
        "clock_skew_ms": heartbeat.clock_skew_ms,
        "disk_available_bytes": heartbeat.disk_available_bytes,
        "execution_slot": {
            "execution_id": None if heartbeat.execution_id is None else str(heartbeat.execution_id),
            "state": heartbeat.execution_state,
        },
        "observed_at": heartbeat.observed_at.astimezone(UTC).isoformat(),
    }
    if heartbeat.source_workspaces is not None:
        capabilities["source_workspaces"] = [
            dict(workspace) for workspace in heartbeat.source_workspaces
        ]
    return capabilities


def _validate_source_workspaces(
    workspaces: tuple[dict[str, object], ...] | None,
) -> None:
    if workspaces is None:
        return
    if len(workspaces) > 32:
        raise DeviceHeartbeatRejected
    workspace_ids: set[str] = set()
    for workspace in workspaces:
        if not isinstance(workspace, dict) or set(workspace) != {
            "workspace_id",
            "name",
            "state",
            "git_branch",
            "git_head",
            "tracked_dirty_count",
            "snapshot_policy",
            "validation_profiles",
        }:
            raise DeviceHeartbeatRejected
        workspace_id = workspace["workspace_id"]
        name = workspace["name"]
        branch = workspace["git_branch"]
        head = workspace["git_head"]
        dirty = workspace["tracked_dirty_count"]
        profiles = workspace["validation_profiles"]
        if (
            not isinstance(workspace_id, str)
            or workspace_id in workspace_ids
            or not isinstance(name, str)
            or not 1 <= len(name) <= 128
            or any(unicodedata.category(character) == "Cc" for character in name)
            or not is_public_source_display_name(name)
            or workspace["state"] not in {"ready", "invalid"}
            or (branch is not None and (
                not isinstance(branch, str)
                or not 1 <= len(branch) <= 255
                or any(unicodedata.category(character) == "Cc" for character in branch)
            ))
            or not isinstance(head, str)
            or re.fullmatch(r"[0-9a-f]{40}", head) is None
            or type(dirty) is not int
            or dirty < 0
            or workspace["snapshot_policy"] != "tracked_worktree"
            or not isinstance(profiles, list)
            or len(profiles) > 8
        ):
            raise DeviceHeartbeatRejected
        try:
            parsed_workspace_id = UUID(workspace_id)
        except ValueError:
            raise DeviceHeartbeatRejected from None
        if (
            str(parsed_workspace_id) != workspace_id
            or parsed_workspace_id.version not in range(1, 6)
        ):
            raise DeviceHeartbeatRejected
        workspace_ids.add(workspace_id)
        profile_ids: set[str] = set()
        for profile in profiles:
            if not isinstance(profile, dict) or set(profile) != {"profile_id", "name"}:
                raise DeviceHeartbeatRejected
            profile_id = profile["profile_id"]
            profile_name = profile["name"]
            if (
                not isinstance(profile_id, str)
                or profile_id in profile_ids
                or not isinstance(profile_name, str)
                or not 1 <= len(profile_name) <= 128
                or not is_public_source_display_name(profile_name)
                or any(unicodedata.category(character) == "Cc" for character in profile_name)
            ):
                raise DeviceHeartbeatRejected
            try:
                parsed_profile_id = UUID(profile_id)
            except ValueError:
                raise DeviceHeartbeatRejected from None
            if (
                str(parsed_profile_id) != profile_id
                or parsed_profile_id.version not in range(1, 6)
            ):
                raise DeviceHeartbeatRejected
            profile_ids.add(profile_id)


def _state_from_observation(
    observation: SanitizedDeviceObservation,
    *,
    leased: bool = False,
) -> DeviceState:
    if observation.property_error_code is not None:
        return "quarantined"
    if observation.adb_state != "device":
        return {
            "unauthorized": "unauthorized",
            "booting": "booting",
            "offline": "offline",
        }[observation.adb_state]
    return "busy" if leased else "ready"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Device directory clock must return an aware datetime")
    return value.astimezone(UTC)


def _stored_device_record(
    stored: StoredDeviceModel,
    *,
    agent_name: str,
) -> StoredDevice:
    return StoredDevice(
        device_id=stored.id,
        team_id=stored.team_id,
        agent_id=stored.agent_id,
        agent_name=agent_name,
        serial_digest=stored.serial_digest,
        serial_suffix=stored.serial_suffix,
        manufacturer=stored.manufacturer,
        model=stored.model,
        android_release=stored.android_release,
        api_level=stored.api_level,
        connection_type=cast(ConnectionType, stored.connection_type),
        adb_state=cast(AdbState, stored.adb_state),
        state=cast(DeviceState, stored.state),
        battery_percent=stored.battery_percent,
        temperature_c=stored.temperature_c,
        storage_available_bytes=stored.storage_available_bytes,
        property_error_code=stored.last_property_error_code,
        last_seen_at=stored.last_seen_at,
    )


__all__ = [
    "AgentHeartbeat",
    "DeviceDirectory",
    "DeviceDirectoryRepository",
    "DeviceHeartbeatRejected",
    "DeviceView",
    "HeartbeatReceipt",
    "InMemoryDeviceDirectoryRepository",
    "SQLAlchemyDeviceDirectoryRepository",
    "SanitizedDeviceObservation",
    "StoredDevice",
]
