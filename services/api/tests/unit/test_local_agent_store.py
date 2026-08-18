from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.local_agent_store import (
    LocalAgentStore,
    LocalAgentStoreError,
    LocalDeviceDirectoryRepository,
)
from perfpilot_api.services.device_directory import (
    AgentHeartbeat,
    DeviceDirectory,
    SanitizedDeviceObservation,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
TEAM_A = UUID("81000000-0000-4000-8000-000000000001")
TEAM_B = UUID("81000000-0000-4000-8000-000000000002")
USER_A = UUID("80000000-0000-4000-8000-000000000001")
AGENT_A = UUID("71000000-0000-4000-8000-000000000001")
DEVICE_A = UUID("72000000-0000-4000-8000-000000000001")
DEVICE_B = UUID("72000000-0000-4000-8000-000000000002")
WORKSPACE_A = UUID("73000000-0000-4000-8000-000000000001")


async def _registered_store(tmp_path: Path) -> LocalAgentStore:
    identifiers = iter((AGENT_A, DEVICE_A, DEVICE_B))
    store = LocalAgentStore(tmp_path, uuid_factory=lambda: next(identifiers))
    await store.create_pending(
        team_id=TEAM_A,
        owner_user_id=USER_A,
        name="Rivotek Agent",
        registration_code_digest="a" * 64,
        registration_code_expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    consumed = await store.consume_registration(
        registration_code_digest="a" * 64,
        now=NOW,
        public_key_b64="A" * 44,
        platform="macos",
        agent_version="1.2.3",
        hostname="developer-mac",
        os_version="15.0",
        access_token_digest="b" * 64,
        access_token_expires_at=NOW + timedelta(minutes=15),
        refresh_token_digest="c" * 64,
        refresh_token_expires_at=NOW + timedelta(days=30),
    )
    assert consumed is not None
    return store


@pytest.mark.asyncio
async def test_agent_credentials_and_source_capabilities_survive_reopen_without_private_values(
    tmp_path: Path,
) -> None:
    store = await _registered_store(tmp_path)
    await store.replace_snapshot(
        agent_id=AGENT_A,
        heartbeat=AgentHeartbeat(
            agent_version="1.2.3",
            platform="macos",
            hostname="developer-mac",
            observed_at=NOW,
            clock_skew_ms=0,
            disk_available_bytes=1024,
            execution_state="idle",
            execution_id=None,
            source_workspaces=(
                {
                    "workspace_id": str(WORKSPACE_A),
                    "name": "RivotekMedia",
                    "state": "ready",
                    "git_branch": "main",
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 0,
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [],
                },
            ),
        ),
        devices=(
            SanitizedDeviceObservation(
                client_ref=UUID("74000000-0000-4000-8000-000000000001"),
                serial_digest="d" * 64,
                serial_suffix="1234",
                manufacturer="Google",
                model="Pixel",
                android_release="16",
                api_level=36,
                connection_type="usb",
                adb_state="device",
                battery_percent=80,
                temperature_c=None,
                storage_available_bytes=100,
                property_error_code=None,
                launch_targets=(
                    (
                        "com.rivotek.mediacenter",
                        "com.rivotek.mediacenter/.shell.MediaCenterActivity",
                    ),
                ),
            ),
        ),
        now=NOW,
    )

    reopened = LocalAgentStore(tmp_path)
    record = await reopened.find_access(
        access_token_digest="b" * 64,
        now=NOW + timedelta(seconds=1),
    )
    sources = await reopened.list_source_agents(TEAM_A)
    devices = await LocalDeviceDirectoryRepository(reopened).list_team(TEAM_A)

    assert record is not None and record.id == AGENT_A
    assert sources[0].capabilities["source_workspaces"][0]["name"] == "RivotekMedia"
    assert devices[0].device_id == DEVICE_A
    assert devices[0].launch_targets == (
        (
            "com.rivotek.mediacenter",
            "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        ),
    )
    payload = (tmp_path / "agents.json").read_text(encoding="utf-8")
    assert "registration plaintext" not in payload
    assert "access plaintext" not in payload
    assert "refresh plaintext" not in payload
    assert "emulator-5554" not in payload
    assert "/Users/" not in payload
    assert (tmp_path.stat().st_mode & 0o777) == 0o700
    assert ((tmp_path / "agents.json").stat().st_mode & 0o777) == 0o600


@pytest.mark.asyncio
async def test_repository_enforces_team_scope_one_time_codes_rotation_and_stale_expiry(
    tmp_path: Path,
) -> None:
    store = await _registered_store(tmp_path)

    replay = await store.consume_registration(
        registration_code_digest="a" * 64,
        now=NOW,
        public_key_b64="A" * 44,
        platform="macos",
        agent_version="1.2.3",
        hostname="developer-mac",
        os_version="15.0",
        access_token_digest="x" * 64,
        access_token_expires_at=NOW + timedelta(minutes=15),
        refresh_token_digest="y" * 64,
        refresh_token_expires_at=NOW + timedelta(days=30),
    )
    rotated = await store.rotate_credentials(
        agent_id=AGENT_A,
        expected_refresh_token_digest="c" * 64,
        expected_token_version=1,
        now=NOW + timedelta(seconds=1),
        access_token_digest="e" * 64,
        access_token_expires_at=NOW + timedelta(minutes=16),
        refresh_token_digest="f" * 64,
        refresh_token_expires_at=NOW + timedelta(days=31),
    )

    assert replay is None
    assert rotated is not None and rotated.token_version == 2
    assert await store.rename(
        team_id=TEAM_B, agent_id=AGENT_A, name="stolen", now=NOW
    ) is None
    assert await store.revoke(team_id=TEAM_B, agent_id=AGENT_A, now=NOW) is None
    assert await store.list_team(TEAM_B) == ()
    await store.replace_snapshot(
        agent_id=AGENT_A,
        heartbeat=AgentHeartbeat(
            agent_version="1.2.3",
            platform="macos",
            hostname="developer-mac",
            observed_at=NOW,
            clock_skew_ms=0,
            disk_available_bytes=1024,
            execution_state="idle",
            execution_id=None,
            source_workspaces=None,
        ),
        devices=(),
        now=NOW,
    )
    assert await store.expire_stale(cutoff=NOW, now=NOW + timedelta(seconds=31)) == 1
    assert (await store.get_source_agent(AGENT_A)).state == "offline"  # type: ignore[union-attr]


def test_store_rejects_unknown_schema_fifo_and_absolute_path_capabilities(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "agents.json").write_text(
        json.dumps({"schema_version": 1, "agents": [], "devices": [], "unknown": []}),
        encoding="utf-8",
    )
    with pytest.raises(LocalAgentStoreError, match="invalid local agent state"):
        LocalAgentStore(tmp_path)


@pytest.mark.asyncio
async def test_private_task_target_is_exact_team_agent_device_and_ready_only(
    tmp_path: Path,
) -> None:
    store = await _registered_store(tmp_path)
    repository = LocalDeviceDirectoryRepository(store)
    directory = DeviceDirectory(
        repository=repository,
        serial_hmac_key=b"device-directory-test-key".ljust(32, b"!"),
        clock=lambda: NOW,
    )
    observation = SanitizedDeviceObservation(
        client_ref=UUID("74000000-0000-4000-8000-000000000001"),
        serial_digest="d" * 64,
        serial_suffix="1234",
        manufacturer="Google",
        model="Pixel",
        android_release="16",
        api_level=36,
        connection_type="usb",
        adb_state="device",
        battery_percent=80,
        temperature_c=None,
        storage_available_bytes=100,
        property_error_code=None,
    )
    idle = AgentHeartbeat(
        agent_version="1.2.3",
        platform="macos",
        hostname="developer-mac",
        observed_at=NOW,
        clock_skew_ms=0,
        disk_available_bytes=1024,
        execution_state="idle",
        execution_id=None,
        source_workspaces=None,
    )
    await repository.replace_snapshot(
        agent_id=AGENT_A,
        heartbeat=idle,
        devices=(observation,),
        now=NOW,
    )

    target = await directory.get_task_target(
        team_id=TEAM_A,
        agent_id=AGENT_A,
        device_id=DEVICE_A,
    )
    assert target is not None
    assert target.team_id == TEAM_A
    assert target.agent_id == AGENT_A
    assert target.device_id == DEVICE_A
    assert target.device_digest == "d" * 64
    assert "1234" not in repr(target)
    assert await directory.get_task_target(
        team_id=TEAM_B, agent_id=AGENT_A, device_id=DEVICE_A
    ) is None
    assert await directory.get_task_target(
        team_id=TEAM_A,
        agent_id=UUID("71000000-0000-4000-8000-000000000099"),
        device_id=DEVICE_A,
    ) is None
    assert await directory.get_task_target(
        team_id=TEAM_A,
        agent_id=AGENT_A,
        device_id=UUID("72000000-0000-4000-8000-000000000099"),
    ) is None

    busy = AgentHeartbeat(
        **{
            **{field: getattr(idle, field) for field in idle.__dataclass_fields__},
            "execution_state": "busy",
            "execution_id": UUID("73000000-0000-4000-8000-000000000099"),
        }
    )
    await repository.replace_snapshot(
        agent_id=AGENT_A,
        heartbeat=busy,
        devices=(observation,),
        now=NOW,
    )
    assert (await repository.list_team(TEAM_A))[0].state == "ready"
    assert await directory.get_task_target(
        team_id=TEAM_A, agent_id=AGENT_A, device_id=DEVICE_A
    ) is None

    await repository.expire_stale(cutoff=NOW, now=NOW + timedelta(seconds=31))
    assert await directory.get_task_target(
        team_id=TEAM_A, agent_id=AGENT_A, device_id=DEVICE_A
    ) is None


@pytest.mark.asyncio
async def test_capture_lease_projects_only_exact_device_and_is_not_persisted(
    tmp_path: Path,
) -> None:
    store = await _registered_store(tmp_path)
    repository = LocalDeviceDirectoryRepository(store)
    directory = DeviceDirectory(
        repository=repository,
        serial_hmac_key=b"device-directory-test-key".ljust(32, b"!"),
        clock=lambda: NOW,
    )
    heartbeat = AgentHeartbeat(
        agent_version="1.2.3",
        platform="macos",
        hostname="developer-mac",
        observed_at=NOW,
        clock_skew_ms=0,
        disk_available_bytes=1024,
        execution_state="idle",
        execution_id=None,
        source_workspaces=None,
    )
    await repository.replace_snapshot(
        agent_id=AGENT_A,
        heartbeat=heartbeat,
        devices=(
            SanitizedDeviceObservation(
                client_ref=UUID("74000000-0000-4000-8000-000000000001"),
                serial_digest="d" * 64,
                serial_suffix="1234",
                manufacturer="Google",
                model="Pixel",
                android_release="16",
                api_level=36,
                connection_type="usb",
                adb_state="device",
                battery_percent=80,
                temperature_c=None,
                storage_available_bytes=100,
                property_error_code=None,
            ),
            SanitizedDeviceObservation(
                client_ref=UUID("74000000-0000-4000-8000-000000000002"),
                serial_digest="e" * 64,
                serial_suffix="5678",
                manufacturer="Google",
                model="Pixel 2",
                android_release="16",
                api_level=36,
                connection_type="usb",
                adb_state="device",
                battery_percent=70,
                temperature_c=None,
                storage_available_bytes=200,
                property_error_code=None,
            ),
        ),
        now=NOW,
    )

    assert await store.project_capture_lease(
        team_id=TEAM_A,
        agent_id=AGENT_A,
        device_id=DEVICE_A,
        execution_id=UUID("73000000-0000-4000-8000-000000000099"),
        expires_at=NOW + timedelta(minutes=1),
    )
    states = {device.device_id: device.state for device in await directory.list_devices(team_id=TEAM_A)}
    assert states == {DEVICE_A: "busy", DEVICE_B: "ready"}

    reopened = LocalAgentStore(tmp_path)
    reopened_directory = DeviceDirectory(
        repository=LocalDeviceDirectoryRepository(reopened),
        serial_hmac_key=b"device-directory-test-key".ljust(32, b"!"),
        clock=lambda: NOW,
    )
    reopened_states = {
        device.device_id: device.state
        for device in await reopened_directory.list_devices(team_id=TEAM_A)
    }
    assert reopened_states == {DEVICE_A: "ready", DEVICE_B: "ready"}
