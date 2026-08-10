from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    InMemoryAgentNonceStore,
    encode_ed25519_public_key,
)
from perfpilot_api.services.agents import (
    AgentRegistration,
    AgentService,
    InMemoryAgentRepository,
    TaskSigningKey,
)
from perfpilot_api.services.device_directory import (
    AgentHeartbeat,
    DeviceDirectory,
    DeviceHeartbeatRejected,
    InMemoryDeviceDirectoryRepository,
)
from perfpilot_api.services.source_workspaces import (
    SourceBinding,
    SourceBindingInvalid,
    SourceWorkspaceService,
)

TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


@dataclass(frozen=True)
class DirectoryHarness:
    directory: DeviceDirectory
    agent_id: UUID
    clock: MutableClock


@pytest.fixture
async def harness() -> DirectoryHarness:
    clock = MutableClock()
    repository = InMemoryAgentRepository(uuid_factory=uuid4)
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    task_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    agent_service = AgentService(
        repository=repository,
        credentials=AgentCredentialCodec(b"c" * 32),
        nonce_store=InMemoryAgentNonceStore(
            key_secret=b"n" * 32,
            clock=lambda: clock().timestamp(),
        ),
        task_signing_key=TaskSigningKey(
            kid="device-test",
            public_key_b64=encode_ed25519_public_key(task_key.public_key()),
        ),
        clock=clock,
    )
    issued = await agent_service.create_registration_code(
        team_id=TEAM_ID,
        owner_user_id=USER_ID,
        name="Ray Mac",
    )
    registered = await agent_service.register(
        AgentRegistration(
            registration_code=issued.registration_code,
            public_key_b64=encode_ed25519_public_key(private_key.public_key()),
            platform="macos",
            agent_version="1.2.3",
            hostname="Ray Mac",
            os_version="macOS 15.6",
        )
    )
    directory = DeviceDirectory(
        repository=InMemoryDeviceDirectoryRepository(repository),
        serial_hmac_key=b"s" * 32,
        clock=clock,
    )
    return DirectoryHarness(
        directory=directory,
        agent_id=registered.agent_id,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_registered_agent_is_not_online_before_first_heartbeat() -> None:
    clock = MutableClock()
    repository = InMemoryAgentRepository(uuid_factory=uuid4)
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    task_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    service = AgentService(
        repository=repository,
        credentials=AgentCredentialCodec(b"c" * 32),
        nonce_store=InMemoryAgentNonceStore(
            key_secret=b"n" * 32,
            clock=lambda: clock().timestamp(),
        ),
        task_signing_key=TaskSigningKey(
            kid="pre-heartbeat-test",
            public_key_b64=encode_ed25519_public_key(task_key.public_key()),
        ),
        clock=clock,
    )
    issued = await service.create_registration_code(
        team_id=TEAM_ID,
        owner_user_id=USER_ID,
        name="Ray Mac",
    )
    await service.register(
        AgentRegistration(
            registration_code=issued.registration_code,
            public_key_b64=encode_ed25519_public_key(private_key.public_key()),
            platform="macos",
            agent_version="1.2.3",
            hostname="Ray Mac",
            os_version="macOS 15.6",
        )
    )

    assert (await service.list_agents(team_id=TEAM_ID))[0].state == "offline"


def heartbeat(*, execution_state: str = "idle") -> AgentHeartbeat:
    return AgentHeartbeat(
        agent_version="1.2.3",
        platform="macos",
        hostname="Ray Mac",
        observed_at=NOW,
        clock_skew_ms=12,
        disk_available_bytes=100 * 1024 * 1024,
        execution_state=execution_state,
        execution_id=None,
    )


def source_heartbeat() -> AgentHeartbeat:
    return AgentHeartbeat(
        agent_version="1.2.3",
        platform="macos",
        hostname="Ray Mac",
        observed_at=NOW,
        clock_skew_ms=12,
        disk_available_bytes=100 * 1024 * 1024,
        execution_state="idle",
        execution_id=None,
        source_workspaces=(
            {
                "workspace_id": "92000000-0000-4000-8000-000000000001",
                "name": "Demo Android",
                "state": "ready",
                "git_branch": "main",
                "git_head": "1" * 40,
                "tracked_dirty_count": 0,
                "snapshot_policy": "tracked_worktree",
                "validation_profiles": [],
            },
        ),
    )


def observation(
    directory: DeviceDirectory,
    *,
    serial: str,
    client_ref: UUID | None = None,
):
    return directory.sanitize_observation(
        client_ref=client_ref or uuid4(),
        serial=serial,
        manufacturer="UNISOC",
        model="ums9620",
        android_release="15",
        api_level=35,
        connection_type="usb",
        adb_state="device",
        battery_percent=82,
        temperature_c=Decimal("31.5"),
        storage_available_bytes=40 * 1024 * 1024,
        property_error_code=None,
    )


@pytest.mark.asyncio
async def test_heartbeat_masks_serial_and_never_retains_raw_value(
    harness: DirectoryHarness,
) -> None:
    raw_serial = "R3CN30ABC7K2A"
    sanitized = observation(harness.directory, serial=raw_serial)

    receipt = await harness.directory.replace_heartbeat(
        agent_id=harness.agent_id,
        heartbeat=heartbeat(),
        devices=(sanitized,),
    )
    devices = await harness.directory.list_devices(team_id=TEAM_ID)

    assert devices[0].serial_suffix == "7K2A"
    assert receipt.devices[0].device_digest != raw_serial
    assert len(receipt.devices[0].device_digest) == 64
    assert raw_serial not in repr((sanitized, receipt, devices, harness.directory))


@pytest.mark.asyncio
async def test_heartbeat_replaces_snapshot_and_keeps_stable_device_id(
    harness: DirectoryHarness,
) -> None:
    first_ref = uuid4()
    second_ref = uuid4()
    first = await harness.directory.replace_heartbeat(
        agent_id=harness.agent_id,
        heartbeat=heartbeat(),
        devices=(
            observation(harness.directory, serial="DEVICE0001", client_ref=first_ref),
            observation(harness.directory, serial="DEVICE0002", client_ref=second_ref),
        ),
    )

    second = await harness.directory.replace_heartbeat(
        agent_id=harness.agent_id,
        heartbeat=heartbeat(),
        devices=(observation(harness.directory, serial="DEVICE0001", client_ref=first_ref),),
    )
    views = await harness.directory.list_devices(team_id=TEAM_ID)

    assert second.devices[0].device_id == first.devices[0].device_id
    assert {view.serial_suffix: view.state for view in views} == {
        "0001": "ready",
        "0002": "offline",
    }


@pytest.mark.asyncio
async def test_stale_agent_and_devices_are_offline(harness: DirectoryHarness) -> None:
    await harness.directory.replace_heartbeat(
        agent_id=harness.agent_id,
        heartbeat=heartbeat(),
        devices=(observation(harness.directory, serial="DEVICE0001"),),
    )
    harness.clock.advance(seconds=31)

    expired = await harness.directory.expire_stale()
    devices = await harness.directory.list_devices(team_id=TEAM_ID)

    assert expired == 1
    assert devices[0].state == "offline"
    assert devices[0].adb_state == "offline"


@pytest.mark.asyncio
async def test_device_listing_expires_stale_snapshot_without_a_separate_worker(
    harness: DirectoryHarness,
) -> None:
    await harness.directory.replace_heartbeat(
        agent_id=harness.agent_id,
        heartbeat=heartbeat(),
        devices=(observation(harness.directory, serial="DEVICE0001"),),
    )
    harness.clock.advance(seconds=31)

    devices = await harness.directory.list_devices(team_id=TEAM_ID)

    assert devices[0].state == "offline"


@pytest.mark.asyncio
async def test_duplicate_serial_is_rejected_without_echoing_it(
    harness: DirectoryHarness,
) -> None:
    raw_serial = "DUPLICATE0001"
    first = observation(harness.directory, serial=raw_serial)
    second = observation(harness.directory, serial=raw_serial)

    with pytest.raises(DeviceHeartbeatRejected) as captured:
        await harness.directory.replace_heartbeat(
            agent_id=harness.agent_id,
            heartbeat=heartbeat(),
            devices=(first, second),
        )

    assert raw_serial not in str(captured.value)


@pytest.mark.asyncio
async def test_source_workspace_reads_expire_stale_agent_before_listing_or_binding(
    harness: DirectoryHarness,
) -> None:
    workspace_id = UUID("92000000-0000-4000-8000-000000000001")
    await harness.directory.replace_heartbeat(
        agent_id=harness.agent_id,
        heartbeat=source_heartbeat(),
        devices=(),
    )
    service = SourceWorkspaceService(
        repository=harness.directory,
        enabled=True,
    )
    binding = SourceBinding(
        provider_kind="agent_workspace",
        agent_id=harness.agent_id,
        workspace_id=workspace_id,
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
    )
    assert len(await service.list_for_team(team_id=TEAM_ID)) == 1

    harness.clock.advance(seconds=31)

    assert await service.list_for_team(team_id=TEAM_ID) == ()
    with pytest.raises(SourceBindingInvalid):
        await service.require_binding(team_id=TEAM_ID, binding=binding)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "private_name"),
    (
        ("workspace", "/Users/ray/private/demo"),
        ("profile", r"C:\Users\ray\private\demo"),
        ("workspace", r"\\server\share\demo"),
        ("branch", "/Users/ray/private/demo"),
        ("branch", r"C:\Users\ray\private\demo"),
        ("branch", r"\\server\share\demo"),
        ("branch", "../private/demo"),
        ("branch", "file:///Users/ray/private/demo"),
    ),
)
async def test_heartbeat_rejects_path_shaped_public_source_names_at_directory_boundary(
    harness: DirectoryHarness,
    field: str,
    private_name: str,
) -> None:
    base = source_heartbeat()
    workspace = dict(base.source_workspaces[0])  # type: ignore[index]
    if field == "workspace":
        workspace["name"] = private_name
    elif field == "profile":
        workspace["validation_profiles"] = [
            {
                "profile_id": "94000000-0000-4000-8000-000000000001",
                "name": private_name,
            }
        ]
    else:
        workspace["git_branch"] = private_name

    with pytest.raises(DeviceHeartbeatRejected) as captured:
        await harness.directory.replace_heartbeat(
            agent_id=harness.agent_id,
            heartbeat=replace(base, source_workspaces=(workspace,)),
            devices=(),
        )

    assert private_name not in str(captured.value)
    agents = await harness.directory.list_source_agents(TEAM_ID)
    assert len(agents) == 1
    assert agents[0].state == "offline"
    assert agents[0].capabilities == {}


def test_sanitizer_rejects_values_outside_database_bounds(
    harness: DirectoryHarness,
) -> None:
    with pytest.raises(DeviceHeartbeatRejected):
        harness.directory.sanitize_observation(
            client_ref=uuid4(),
            serial="BOUNDED0001",
            manufacturer="UNISOC",
            model="ums9620",
            android_release="15",
            api_level=35,
            connection_type="usb",
            adb_state="device",
            battery_percent=82,
            temperature_c=Decimal("31.5"),
            storage_available_bytes=2**63,
            property_error_code=None,
        )


@pytest.mark.asyncio
async def test_heartbeat_rejects_invalid_runtime_metadata(
    harness: DirectoryHarness,
) -> None:
    invalid = AgentHeartbeat(
        agent_version="not-semver",
        platform="macos",
        hostname="Ray Mac",
        observed_at=NOW,
        clock_skew_ms=0,
        disk_available_bytes=1,
        execution_state="idle",
        execution_id=None,
    )

    with pytest.raises(DeviceHeartbeatRejected):
        await harness.directory.replace_heartbeat(
            agent_id=harness.agent_id,
            heartbeat=invalid,
            devices=(),
        )
