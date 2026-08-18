"""Private persistent repositories for the loopback Agent control plane."""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from perfpilot_api.services.agents import (
    AgentNameConflictError,
    AgentPlatform,
    AgentRecord,
    AgentState,
)
from perfpilot_api.services.device_directory import (
    AgentHeartbeat,
    DeviceTaskTarget,
    DeviceHeartbeatRejected,
    SanitizedDeviceObservation,
    StoredDevice,
    _execution_slot_is_idle,
    _heartbeat_capabilities,
    _state_from_observation,
    _validate_heartbeat,
)
from perfpilot_api.services.source_workspaces import SourceAgentCapabilityRecord


_SCHEMA_VERSION = 1
_DOCUMENT_NAME = "agents.json"
_LOCK_NAME = ".agents.lock"
_RUNTIME_SECRET_NAME = ".agent-runtime.key"
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_AGENTS = 256
_MAX_DEVICES = 1024
_AGENT_KEYS = {
    "id",
    "team_id",
    "owner_user_id",
    "name",
    "state",
    "registration_code_digest",
    "registration_code_expires_at",
    "registration_code_used_at",
    "access_token_digest",
    "access_token_expires_at",
    "refresh_token_digest",
    "refresh_token_expires_at",
    "token_version",
    "public_key_b64",
    "platform",
    "agent_version",
    "hostname",
    "os_version",
    "last_heartbeat_at",
    "created_at",
    "updated_at",
    "capabilities",
}
_DEVICE_KEYS = {
    "device_id",
    "team_id",
    "agent_id",
    "agent_name",
    "serial_digest",
    "serial_suffix",
    "manufacturer",
    "model",
    "android_release",
    "api_level",
    "connection_type",
    "adb_state",
    "state",
    "battery_percent",
    "temperature_c",
    "storage_available_bytes",
    "property_error_code",
    "last_seen_at",
    "launch_targets",
}
_LEGACY_DEVICE_KEYS = _DEVICE_KEYS - {"launch_targets"}


class LocalAgentStoreError(RuntimeError):
    """A deliberately redacted persistence error."""


def _reject_constant(_value: str) -> object:
    raise ValueError


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise ValueError
    return parsed.astimezone(UTC)


def _parse_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value or parsed.version not in range(1, 6):
        raise ValueError
    return parsed


class LocalAgentStore:
    """Implements AgentRepository and DeviceDirectoryRepository on one locked file."""

    def __init__(
        self,
        state_root: Path,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        if state_root.is_symlink():
            raise LocalAgentStoreError("unsafe local agent path")
        self._root_fd = -1
        try:
            state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._root = state_root.absolute()
            self._root_fd = os.open(
                self._root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_status = os.fstat(self._root_fd)
            self._root_identity = (root_status.st_dev, root_status.st_ino)
            self._verify_root()
            os.fchmod(self._root_fd, 0o700)
            for name in (_DOCUMENT_NAME, _LOCK_NAME):
                if self._entry_is_symlink(name):
                    raise LocalAgentStoreError("unsafe local agent path")
            self._secure_existing_document()
            with self._exclusive_lock():
                self._read_document()
        except LocalAgentStoreError:
            self.close()
            raise
        except OSError:
            self.close()
            raise LocalAgentStoreError("unsafe local agent path") from None
        self._uuid_factory = uuid_factory
        self._capture_leases: dict[UUID, tuple[UUID, UUID, UUID, datetime]] = {}

    def close(self) -> None:
        if self._root_fd >= 0:
            descriptor, self._root_fd = self._root_fd, -1
            os.close(descriptor)

    def runtime_secret(self) -> bytes:
        """Return a stable, private local-only key for agent service primitives."""
        with self._exclusive_lock():
            try:
                descriptor = os.open(
                    _RUNTIME_SECRET_NAME,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
            except FileNotFoundError:
                secret = secrets.token_bytes(32)
                try:
                    descriptor = os.open(
                        _RUNTIME_SECRET_NAME,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=self._root_fd,
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        descriptor = -1
                        stream.write(secret)
                        stream.flush()
                        os.fsync(stream.fileno())
                    return secret
                except OSError:
                    raise LocalAgentStoreError("local agent persistence failed") from None
                finally:
                    if "descriptor" in locals() and descriptor >= 0:
                        os.close(descriptor)
            except OSError:
                raise LocalAgentStoreError("local agent persistence failed") from None
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_size != 32:
                    raise ValueError
                secret = os.read(descriptor, 33)
                if len(secret) != 32:
                    raise ValueError
                os.fchmod(descriptor, 0o600)
                return secret
            except (OSError, ValueError):
                raise LocalAgentStoreError("local agent persistence failed") from None
            finally:
                os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    async def create_pending(
        self,
        *,
        team_id: UUID,
        owner_user_id: UUID,
        name: str,
        registration_code_digest: str,
        registration_code_expires_at: datetime,
        now: datetime,
    ) -> AgentRecord:
        with self._exclusive_lock():
            document = self._read_document()
            records = [self._agent_from_document(item) for item in document["agents"]]
            if len(records) >= _MAX_AGENTS:
                raise LocalAgentStoreError("local agent persistence failed")
            if any(record.team_id == team_id and record.name == name for record in records):
                raise AgentNameConflictError
            record = AgentRecord(
                id=self._new_uuid(),
                team_id=team_id,
                owner_user_id=owner_user_id,
                name=name,
                state="pending",
                registration_code_digest=registration_code_digest,
                registration_code_expires_at=registration_code_expires_at,
                registration_code_used_at=None,
                access_token_digest=None,
                access_token_expires_at=None,
                refresh_token_digest=None,
                refresh_token_expires_at=None,
                token_version=1,
                public_key_b64=None,
                platform=None,
                agent_version=None,
                hostname=None,
                os_version=None,
                last_heartbeat_at=None,
                created_at=now,
                updated_at=now,
            )
            document["agents"].append(self._agent_document(record, capabilities={}))
            self._write_document(document)
            return record

    async def consume_registration(
        self,
        *,
        registration_code_digest: str,
        now: datetime,
        public_key_b64: str,
        platform: AgentPlatform,
        agent_version: str,
        hostname: str,
        os_version: str,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None:
        with self._exclusive_lock():
            document = self._read_document()
            for index, item in enumerate(document["agents"]):
                record = self._agent_from_document(item)
                if record.registration_code_digest is None or not hmac.compare_digest(
                    record.registration_code_digest, registration_code_digest
                ):
                    continue
                if (
                    record.state != "pending"
                    or record.registration_code_used_at is not None
                    or record.registration_code_expires_at is None
                    or record.registration_code_expires_at <= now
                ):
                    return None
                registered = replace(
                    record,
                    state="offline",
                    registration_code_digest=None,
                    registration_code_used_at=now,
                    access_token_digest=access_token_digest,
                    access_token_expires_at=access_token_expires_at,
                    refresh_token_digest=refresh_token_digest,
                    refresh_token_expires_at=refresh_token_expires_at,
                    public_key_b64=public_key_b64,
                    platform=platform,
                    agent_version=agent_version,
                    hostname=hostname,
                    os_version=os_version,
                    updated_at=now,
                )
                document["agents"][index] = self._agent_document(
                    registered, capabilities=cast(dict[str, object], item["capabilities"])
                )
                self._write_document(document)
                return registered
            return None

    async def get_refresh_candidate(self, agent_id: UUID) -> AgentRecord | None:
        with self._exclusive_lock():
            document = self._read_document()
            item = self._find_agent(document, agent_id)
            return None if item is None else self._agent_from_document(item)

    async def rotate_credentials(
        self,
        *,
        agent_id: UUID,
        expected_refresh_token_digest: str,
        expected_token_version: int,
        now: datetime,
        access_token_digest: str,
        access_token_expires_at: datetime,
        refresh_token_digest: str,
        refresh_token_expires_at: datetime,
    ) -> AgentRecord | None:
        with self._exclusive_lock():
            document = self._read_document()
            item = self._find_agent(document, agent_id)
            if item is None:
                return None
            record = self._agent_from_document(item)
            if (
                record.state == "revoked"
                or record.refresh_token_digest is None
                or not hmac.compare_digest(
                    record.refresh_token_digest, expected_refresh_token_digest
                )
                or record.refresh_token_expires_at is None
                or record.refresh_token_expires_at <= now
                or record.token_version != expected_token_version
            ):
                return None
            rotated = replace(
                record,
                access_token_digest=access_token_digest,
                access_token_expires_at=access_token_expires_at,
                refresh_token_digest=refresh_token_digest,
                refresh_token_expires_at=refresh_token_expires_at,
                token_version=record.token_version + 1,
                updated_at=now,
            )
            capabilities = dict(cast(dict[str, object], item["capabilities"]))
            item.clear()
            item.update(self._agent_document(rotated, capabilities=capabilities))
            self._write_document(document)
            return rotated

    async def find_access(
        self, *, access_token_digest: str, now: datetime
    ) -> AgentRecord | None:
        with self._exclusive_lock():
            for item in self._read_document()["agents"]:
                record = self._agent_from_document(item)
                if (
                    record.access_token_digest is not None
                    and hmac.compare_digest(record.access_token_digest, access_token_digest)
                    and record.access_token_expires_at is not None
                    and record.access_token_expires_at > now
                    and record.state != "revoked"
                ):
                    return record
            return None

    async def list_team(self, team_id: UUID) -> tuple[Any, ...]:
        """Return AgentRecords to AgentService, or StoredDevices to DeviceDirectory.

        Both repository protocols unfortunately use the same method name.  DeviceDirectory
        calls this store through ``list_devices_team`` below; local composition installs its
        narrow adapter so runtime dispatch remains explicit.
        """
        with self._exclusive_lock():
            return tuple(
                sorted(
                    (
                        self._agent_from_document(item)
                        for item in self._read_document()["agents"]
                        if item["team_id"] == str(team_id)
                    ),
                    key=lambda record: (record.name.casefold(), str(record.id)),
                )
            )

    async def list_devices_team(self, team_id: UUID) -> tuple[StoredDevice, ...]:
        with self._exclusive_lock():
            devices = [
                self._device_from_document(item)
                for item in self._read_document()["devices"]
                if item["team_id"] == str(team_id)
            ]
            return tuple(sorted(devices, key=lambda item: (item.agent_name.casefold(), str(item.device_id))))

    async def project_capture_lease(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        execution_id: UUID,
        expires_at: datetime,
    ) -> bool:
        if expires_at.tzinfo is None:
            raise ValueError("Capture lease expiry must be timezone-aware")
        with self._exclusive_lock():
            device = next(
                (
                    item
                    for item in self._read_document()["devices"]
                    if item["device_id"] == str(device_id)
                ),
                None,
            )
            if (
                device is None
                or device["team_id"] != str(team_id)
                or device["agent_id"] != str(agent_id)
            ):
                return False
        self._capture_leases[device_id] = (
            team_id,
            agent_id,
            execution_id,
            expires_at.astimezone(UTC),
        )
        return True

    async def release_capture_lease(
        self, *, device_id: UUID, execution_id: UUID
    ) -> None:
        lease = self._capture_leases.get(device_id)
        if lease is not None and lease[2] == execution_id:
            self._capture_leases.pop(device_id, None)

    async def active_capture_device_ids(
        self, *, team_id: UUID, now: datetime
    ) -> frozenset[UUID]:
        if now.tzinfo is None:
            raise ValueError("Capture lease clock must be timezone-aware")
        current = now.astimezone(UTC)
        for device_id, (_, _, _, expires_at) in tuple(self._capture_leases.items()):
            if expires_at <= current:
                self._capture_leases.pop(device_id, None)
        return frozenset(
            device_id
            for device_id, (lease_team_id, _, _, _) in self._capture_leases.items()
            if lease_team_id == team_id
        )

    async def get_task_target(
        self,
        *,
        team_id: UUID,
        agent_id: UUID,
        device_id: UUID,
    ) -> DeviceTaskTarget | None:
        with self._exclusive_lock():
            document = self._read_document()
            agent = self._find_agent(document, agent_id)
            device_document = next(
                (
                    item
                    for item in document["devices"]
                    if item["device_id"] == str(device_id)
                ),
                None,
            )
            if (
                agent is None
                or agent["team_id"] != str(team_id)
                or agent["state"] != "online"
                or not _execution_slot_is_idle(agent["capabilities"])
                or device_document is None
            ):
                return None
            device = self._device_from_document(device_document)
            if (
                device.team_id != team_id
                or device.agent_id != agent_id
                or device.state not in {"ready", "busy"}
                or device.adb_state != "device"
            ):
                return None
            return DeviceTaskTarget(
                team_id=team_id,
                agent_id=agent_id,
                device_id=device_id,
                device_digest=device.serial_digest,
            )

    async def rename(
        self, *, team_id: UUID, agent_id: UUID, name: str, now: datetime
    ) -> AgentRecord | None:
        with self._exclusive_lock():
            document = self._read_document()
            item = self._find_agent(document, agent_id)
            if item is None or item["team_id"] != str(team_id):
                return None
            if any(
                other is not item
                and other["team_id"] == str(team_id)
                and other["name"] == name
                for other in document["agents"]
            ):
                raise AgentNameConflictError
            item["name"] = name
            item["updated_at"] = _timestamp(now)
            for device in document["devices"]:
                if device["agent_id"] == str(agent_id):
                    device["agent_name"] = name
            self._write_document(document)
            return self._agent_from_document(item)

    async def revoke(
        self, *, team_id: UUID, agent_id: UUID, now: datetime
    ) -> AgentRecord | None:
        with self._exclusive_lock():
            document = self._read_document()
            item = self._find_agent(document, agent_id)
            if item is None or item["team_id"] != str(team_id):
                return None
            item.update(
                {
                    "state": "revoked",
                    "registration_code_digest": None,
                    "access_token_digest": None,
                    "access_token_expires_at": None,
                    "refresh_token_digest": None,
                    "refresh_token_expires_at": None,
                    "public_key_b64": None,
                    "token_version": int(item["token_version"]) + 1,
                    "updated_at": _timestamp(now),
                    "capabilities": {},
                }
            )
            for device in document["devices"]:
                if device["agent_id"] == str(agent_id):
                    device.update({"adb_state": "offline", "state": "offline"})
            self._write_document(document)
            return self._agent_from_document(item)

    async def replace_snapshot(
        self,
        *,
        agent_id: UUID,
        heartbeat: AgentHeartbeat,
        devices: tuple[SanitizedDeviceObservation, ...],
        now: datetime,
    ) -> tuple[StoredDevice, ...]:
        try:
            _validate_heartbeat(heartbeat)
        except DeviceHeartbeatRejected:
            raise
        with self._exclusive_lock():
            document = self._read_document()
            agent = self._find_agent(document, agent_id)
            if agent is None or agent["state"] in {"pending", "revoked"}:
                raise DeviceHeartbeatRejected
            team_id = _parse_uuid(agent["team_id"])
            by_digest = {item["serial_digest"]: item for item in document["devices"]}
            submitted = {item.serial_digest for item in devices}
            accepted: list[StoredDevice] = []
            for observation in devices:
                current = by_digest.get(observation.serial_digest)
                if current is not None and current["team_id"] != str(team_id):
                    raise DeviceHeartbeatRejected
                device_id = (
                    self._new_uuid()
                    if current is None
                    else _parse_uuid(current["device_id"])
                )
                stored = StoredDevice(
                    device_id=device_id,
                    team_id=team_id,
                    agent_id=agent_id,
                    agent_name=str(agent["name"]),
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
                    launch_targets=observation.launch_targets,
                )
                encoded = self._device_document(stored)
                if current is None:
                    document["devices"].append(encoded)
                else:
                    current.clear()
                    current.update(encoded)
                accepted.append(stored)
            for item in document["devices"]:
                if item["agent_id"] == str(agent_id) and item["serial_digest"] not in submitted:
                    item.update({"adb_state": "offline", "state": "offline"})
            agent.update(
                {
                    "state": "online",
                    "platform": heartbeat.platform,
                    "agent_version": heartbeat.agent_version,
                    "hostname": heartbeat.hostname,
                    "last_heartbeat_at": _timestamp(now),
                    "updated_at": _timestamp(now),
                    "capabilities": _heartbeat_capabilities(heartbeat),
                }
            )
            self._write_document(document)
            return tuple(accepted)

    async def expire_stale(self, *, cutoff: datetime, now: datetime) -> int:
        with self._exclusive_lock():
            document = self._read_document()
            stale: set[str] = set()
            for agent in document["agents"]:
                last_seen = _parse_timestamp(agent["last_heartbeat_at"], optional=True)
                if agent["state"] != "revoked" and last_seen is not None and last_seen <= cutoff:
                    agent.update(
                        {"state": "offline", "updated_at": _timestamp(now), "capabilities": {}}
                    )
                    stale.add(str(agent["id"]))
            for device in document["devices"]:
                if device["agent_id"] in stale:
                    device.update({"adb_state": "offline", "state": "offline"})
            if stale:
                self._write_document(document)
            return len(stale)

    async def list_source_agents(
        self, team_id: UUID
    ) -> tuple[SourceAgentCapabilityRecord, ...]:
        with self._exclusive_lock():
            return tuple(
                SourceAgentCapabilityRecord(
                    agent_id=_parse_uuid(item["id"]),
                    team_id=_parse_uuid(item["team_id"]),
                    name=str(item["name"]),
                    state=str(item["state"]),
                    capabilities=dict(cast(dict[str, object], item["capabilities"])),
                )
                for item in self._read_document()["agents"]
                if item["team_id"] == str(team_id)
            )

    async def get_source_agent(self, agent_id: UUID) -> SourceAgentCapabilityRecord | None:
        with self._exclusive_lock():
            item = self._find_agent(self._read_document(), agent_id)
            if item is None:
                return None
            return SourceAgentCapabilityRecord(
                agent_id=agent_id,
                team_id=_parse_uuid(item["team_id"]),
                name=str(item["name"]),
                state=str(item["state"]),
                capabilities=dict(cast(dict[str, object], item["capabilities"])),
            )

    def _new_uuid(self) -> UUID:
        value = self._uuid_factory()
        if not isinstance(value, UUID) or value.version not in range(1, 6):
            raise LocalAgentStoreError("local agent persistence failed")
        return value

    @staticmethod
    def _empty_document() -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, "agents": [], "devices": []}

    def _verify_root(self) -> None:
        try:
            current = os.lstat(self._root)
            held = os.fstat(self._root_fd)
            if (
                self._root_fd < 0
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self._root_identity
                or (held.st_dev, held.st_ino) != self._root_identity
            ):
                raise ValueError
        except (OSError, ValueError):
            raise LocalAgentStoreError("unsafe local agent path") from None

    def _entry_is_symlink(self, name: str) -> bool:
        try:
            return stat.S_ISLNK(os.stat(name, dir_fd=self._root_fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False

    def _secure_existing_document(self) -> None:
        try:
            status = os.stat(_DOCUMENT_NAME, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(status.st_mode):
            raise LocalAgentStoreError("unsafe local agent path")
        os.chmod(_DOCUMENT_NAME, 0o600, dir_fd=self._root_fd, follow_symlinks=False)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self._verify_root()
        try:
            descriptor = os.open(
                _LOCK_NAME,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._verify_root()
            yield
        except LocalAgentStoreError:
            raise
        except OSError:
            raise LocalAgentStoreError("local agent persistence failed") from None
        finally:
            if "descriptor" in locals():
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _read_document(self) -> dict[str, Any]:
        self._verify_root()
        try:
            descriptor = os.open(
                _DOCUMENT_NAME,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return self._empty_document()
        except OSError:
            raise LocalAgentStoreError("invalid local agent state") from None
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_DOCUMENT_BYTES:
                raise ValueError
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                payload = stream.read(_MAX_DOCUMENT_BYTES + 1)
            if not payload or len(payload) > _MAX_DOCUMENT_BYTES:
                raise ValueError
            document = json.loads(
                payload,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
            self._validate_document(document)
            return cast(dict[str, Any], document)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise LocalAgentStoreError("invalid local agent state") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_document(self, document: dict[str, Any]) -> None:
        try:
            self._validate_document(document)
            payload = json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if not payload or len(payload) > _MAX_DOCUMENT_BYTES:
                raise ValueError
            temporary = f".{_DOCUMENT_NAME}.{uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, _DOCUMENT_NAME, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except (OSError, UnboundLocalError):
                pass
            raise LocalAgentStoreError("local agent persistence failed") from None

    def _validate_document(self, document: object) -> None:
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "agents", "devices"}
            or document["schema_version"] != _SCHEMA_VERSION
            or not isinstance(document["agents"], list)
            or not isinstance(document["devices"], list)
            or len(document["agents"]) > _MAX_AGENTS
            or len(document["devices"]) > _MAX_DEVICES
        ):
            raise ValueError
        agent_ids: set[UUID] = set()
        for item in document["agents"]:
            record = self._agent_from_document(item)
            if record.id in agent_ids:
                raise ValueError
            agent_ids.add(record.id)
            capabilities = item["capabilities"]
            if not isinstance(capabilities, dict):
                raise ValueError
            # Reuse the public parser/validator shape by validating through heartbeat.
            workspaces = capabilities.get("source_workspaces")
            if workspaces is not None and not isinstance(workspaces, list):
                raise ValueError
        device_ids: set[UUID] = set()
        for item in document["devices"]:
            device = self._device_from_document(item)
            if device.device_id in device_ids or device.agent_id not in agent_ids:
                raise ValueError
            device_ids.add(device.device_id)

    @staticmethod
    def _find_agent(document: dict[str, Any], agent_id: UUID) -> dict[str, Any] | None:
        return next((item for item in document["agents"] if item["id"] == str(agent_id)), None)

    @staticmethod
    def _capabilities_for(document: dict[str, Any], agent_id: UUID) -> dict[str, object]:
        item = LocalAgentStore._find_agent(document, agent_id)
        return {} if item is None else dict(cast(dict[str, object], item["capabilities"]))

    @staticmethod
    def _agent_document(record: AgentRecord, *, capabilities: object) -> dict[str, object]:
        return {
            "id": str(record.id),
            "team_id": str(record.team_id),
            "owner_user_id": str(record.owner_user_id),
            "name": record.name,
            "state": record.state,
            "registration_code_digest": record.registration_code_digest,
            "registration_code_expires_at": _timestamp(record.registration_code_expires_at),
            "registration_code_used_at": _timestamp(record.registration_code_used_at),
            "access_token_digest": record.access_token_digest,
            "access_token_expires_at": _timestamp(record.access_token_expires_at),
            "refresh_token_digest": record.refresh_token_digest,
            "refresh_token_expires_at": _timestamp(record.refresh_token_expires_at),
            "token_version": record.token_version,
            "public_key_b64": record.public_key_b64,
            "platform": record.platform,
            "agent_version": record.agent_version,
            "hostname": record.hostname,
            "os_version": record.os_version,
            "last_heartbeat_at": _timestamp(record.last_heartbeat_at),
            "created_at": _timestamp(record.created_at),
            "updated_at": _timestamp(record.updated_at),
            "capabilities": dict(cast(dict[str, object], capabilities)),
        }

    @staticmethod
    def _agent_from_document(item: object) -> AgentRecord:
        if not isinstance(item, dict) or set(item) != _AGENT_KEYS:
            raise ValueError
        state = item["state"]
        platform = item["platform"]
        if state not in {"pending", "online", "offline", "revoked"} or platform not in {
            None, "macos", "windows", "linux"
        }:
            raise ValueError
        string_fields = (
            "name", "registration_code_digest", "access_token_digest",
            "refresh_token_digest", "public_key_b64", "agent_version", "hostname", "os_version",
        )
        if not isinstance(item["name"], str) or not item["name"] or any(
            item[key] is not None and not isinstance(item[key], str) for key in string_fields[1:]
        ):
            raise ValueError
        if type(item["token_version"]) is not int or item["token_version"] < 1:
            raise ValueError
        return AgentRecord(
            id=_parse_uuid(item["id"]),
            team_id=_parse_uuid(item["team_id"]),
            owner_user_id=_parse_uuid(item["owner_user_id"]),
            name=item["name"],
            state=cast(AgentState, state),
            registration_code_digest=item["registration_code_digest"],
            registration_code_expires_at=_parse_timestamp(item["registration_code_expires_at"], optional=True),
            registration_code_used_at=_parse_timestamp(item["registration_code_used_at"], optional=True),
            access_token_digest=item["access_token_digest"],
            access_token_expires_at=_parse_timestamp(item["access_token_expires_at"], optional=True),
            refresh_token_digest=item["refresh_token_digest"],
            refresh_token_expires_at=_parse_timestamp(item["refresh_token_expires_at"], optional=True),
            token_version=item["token_version"],
            public_key_b64=item["public_key_b64"],
            platform=cast(AgentPlatform | None, platform),
            agent_version=item["agent_version"],
            hostname=item["hostname"],
            os_version=item["os_version"],
            last_heartbeat_at=_parse_timestamp(item["last_heartbeat_at"], optional=True),
            created_at=cast(datetime, _parse_timestamp(item["created_at"])),
            updated_at=cast(datetime, _parse_timestamp(item["updated_at"])),
        )

    @staticmethod
    def _device_document(device: StoredDevice) -> dict[str, object]:
        document = asdict(device)
        document.update(
            {
                "device_id": str(device.device_id),
                "team_id": str(device.team_id),
                "agent_id": str(device.agent_id),
                "temperature_c": None if device.temperature_c is None else str(device.temperature_c),
                "last_seen_at": _timestamp(device.last_seen_at),
            }
        )
        return document

    @staticmethod
    def _device_from_document(item: object) -> StoredDevice:
        if not isinstance(item, dict) or set(item) not in {
            frozenset(_DEVICE_KEYS),
            frozenset(_LEGACY_DEVICE_KEYS),
        }:
            raise ValueError
        if any(item[key] is not None and not isinstance(item[key], str) for key in (
            "agent_name", "serial_digest", "serial_suffix", "manufacturer", "model",
            "android_release", "connection_type", "adb_state", "state", "property_error_code"
        )):
            raise ValueError
        launch_targets = item.get("launch_targets", [])
        if (
            not isinstance(launch_targets, (list, tuple))
            or len(launch_targets) > 128
            or any(
                not isinstance(target, (list, tuple))
                or len(target) != 2
                or not all(isinstance(value, str) for value in target)
                for target in launch_targets
            )
        ):
            raise ValueError
        return StoredDevice(
            device_id=_parse_uuid(item["device_id"]),
            team_id=_parse_uuid(item["team_id"]),
            agent_id=_parse_uuid(item["agent_id"]),
            agent_name=item["agent_name"],
            serial_digest=item["serial_digest"],
            serial_suffix=item["serial_suffix"],
            manufacturer=item["manufacturer"],
            model=item["model"],
            android_release=item["android_release"],
            api_level=item["api_level"],
            connection_type=item["connection_type"],
            adb_state=item["adb_state"],
            state=item["state"],
            battery_percent=item["battery_percent"],
            temperature_c=None if item["temperature_c"] is None else Decimal(item["temperature_c"]),
            storage_available_bytes=item["storage_available_bytes"],
            property_error_code=item["property_error_code"],
            last_seen_at=_parse_timestamp(item["last_seen_at"], optional=True),
            launch_targets=tuple((target[0], target[1]) for target in launch_targets),
        )


class LocalDeviceDirectoryRepository:
    """Narrow adapter resolving the protocol's colliding ``list_team`` method."""

    def __init__(self, store: LocalAgentStore) -> None:
        self._store = store

    async def replace_snapshot(self, **kwargs: Any) -> tuple[StoredDevice, ...]:
        return await self._store.replace_snapshot(**kwargs)

    async def list_team(self, team_id: UUID) -> tuple[StoredDevice, ...]:
        return await self._store.list_devices_team(team_id)

    async def active_capture_device_ids(
        self, *, team_id: UUID, now: datetime
    ) -> frozenset[UUID]:
        return await self._store.active_capture_device_ids(team_id=team_id, now=now)

    async def get_task_target(self, **kwargs: Any) -> DeviceTaskTarget | None:
        return await self._store.get_task_target(**kwargs)

    async def expire_stale(self, **kwargs: Any) -> int:
        return await self._store.expire_stale(**kwargs)

    async def list_source_agents(self, team_id: UUID) -> tuple[SourceAgentCapabilityRecord, ...]:
        return await self._store.list_source_agents(team_id)

    async def get_source_agent(self, agent_id: UUID) -> SourceAgentCapabilityRecord | None:
        return await self._store.get_source_agent(agent_id)


__all__ = ["LocalAgentStore", "LocalAgentStoreError", "LocalDeviceDirectoryRepository"]
