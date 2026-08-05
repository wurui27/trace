from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import sql
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from perfpilot_api.db.control.models import Agent, AgentLease, Device, GlobalJob, Team, User
from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    InMemoryAgentNonceStore,
    encode_ed25519_public_key,
)
from perfpilot_api.services.agents import (
    AgentRegistration,
    AgentService,
    SQLAlchemyAgentRepository,
    TaskSigningKey,
)
from perfpilot_api.services.device_directory import (
    AgentHeartbeat,
    DeviceDirectory,
    DeviceHeartbeatRejected,
    SQLAlchemyDeviceDirectoryRepository,
)

TEAM_A_ID = UUID("20000000-0000-4000-8000-000000000001")
TEAM_B_ID = UUID("20000000-0000-4000-8000-000000000002")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "control"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL device tests")
    url = make_url(raw_url)
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        pytest.fail(f"{_POSTGRES_URL_ENV} must be a PostgreSQL psycopg URL")
    return url


def _conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _migration_config(url: URL) -> Config:
    config = Config(str(_MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_MIGRATIONS_ROOT))
    config.set_main_option(
        "sqlalchemy.url",
        url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


@pytest.fixture(scope="module")
def device_database_url() -> Iterator[URL]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_device_directory_{uuid4().hex}"
    with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name))
        )
    database_url = admin_url.set(database=database_name)
    try:
        command.upgrade(_migration_config(database_url), "head")
        yield database_url
    finally:
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


@dataclass
class DeviceDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    agents: AgentService
    directory: DeviceDirectory
    private_key: Ed25519PrivateKey
    clock: MutableClock


@pytest.fixture
async def device_database(device_database_url: URL) -> AsyncIterator[DeviceDatabase]:
    engine = create_async_engine(device_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        await session.execute(text("TRUNCATE TABLE users, teams CASCADE"))
        session.add_all(
            (
                User(
                    id=USER_ID,
                    username="device-owner",
                    password_hash="unused",
                    state="active",
                ),
                Team(id=TEAM_A_ID, name="Device Team A", state="active"),
                Team(id=TEAM_B_ID, name="Device Team B", state="active"),
            )
        )
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    task_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    clock = MutableClock()
    agents = AgentService(
        repository=SQLAlchemyAgentRepository(sessions),
        credentials=AgentCredentialCodec(b"c" * 32),
        nonce_store=InMemoryAgentNonceStore(
            key_secret=b"n" * 32,
            clock=lambda: clock().timestamp(),
        ),
        task_signing_key=TaskSigningKey(
            kid="device-repository-test",
            public_key_b64=encode_ed25519_public_key(task_key.public_key()),
        ),
        clock=clock,
    )
    directory = DeviceDirectory(
        repository=SQLAlchemyDeviceDirectoryRepository(sessions),
        serial_hmac_key=b"s" * 32,
        clock=clock,
    )
    try:
        yield DeviceDatabase(
            engine=engine,
            sessions=sessions,
            agents=agents,
            directory=directory,
            private_key=private_key,
            clock=clock,
        )
    finally:
        await engine.dispose()


async def _register(
    database: DeviceDatabase,
    *,
    team_id: UUID,
    name: str,
) -> UUID:
    issued = await database.agents.create_registration_code(
        team_id=team_id,
        owner_user_id=USER_ID,
        name=name,
    )
    registered = await database.agents.register(
        AgentRegistration(
            registration_code=issued.registration_code,
            public_key_b64=encode_ed25519_public_key(database.private_key.public_key()),
            platform="linux",
            agent_version="1.2.3",
            hostname=name,
            os_version="Ubuntu 24.04",
        )
    )
    return registered.agent_id


def _heartbeat(*, hostname: str) -> AgentHeartbeat:
    return AgentHeartbeat(
        agent_version="1.2.3",
        platform="linux",
        hostname=hostname,
        observed_at=NOW,
        clock_skew_ms=5,
        disk_available_bytes=1024,
        execution_state="idle",
        execution_id=None,
    )


def _observation(
    database: DeviceDatabase,
    *,
    serial: str,
    adb_state: str = "device",
):
    return database.directory.sanitize_observation(
        client_ref=uuid4(),
        serial=serial,
        manufacturer="UNISOC",
        model="ums9620",
        android_release="15",
        api_level=35,
        connection_type="usb",
        adb_state=adb_state,  # type: ignore[arg-type]
        battery_percent=82,
        temperature_c=Decimal("31.5"),
        storage_available_bytes=4096,
        property_error_code=None,
    )


@pytest.mark.asyncio
async def test_sql_heartbeat_stores_only_digest_and_bounded_agent_status(
    device_database: DeviceDatabase,
) -> None:
    agent_id = await _register(
        device_database,
        team_id=TEAM_A_ID,
        name="Ubuntu Agent",
    )
    raw_serial = "R3CN30ABC7K2A"

    await device_database.directory.replace_heartbeat(
        agent_id=agent_id,
        heartbeat=_heartbeat(hostname="Ubuntu Agent"),
        devices=(_observation(device_database, serial=raw_serial),),
    )

    async with device_database.sessions() as session:
        stored_agent = await session.get(Agent, agent_id)
        stored_device = await session.scalar(select(Device))
    assert stored_agent is not None
    assert stored_agent.last_heartbeat_at == NOW
    assert stored_agent.capabilities == {
        "clock_skew_ms": 5,
        "disk_available_bytes": 1024,
        "execution_slot": {"execution_id": None, "state": "idle"},
        "observed_at": "2026-08-05T08:00:00+00:00",
    }
    assert stored_device is not None
    assert stored_device.serial_digest != raw_serial
    assert stored_device.serial_suffix == "7K2A"
    assert raw_serial not in repr((stored_agent.capabilities, stored_device.serial_digest))
    assert await device_database.directory.list_devices(team_id=TEAM_B_ID) == ()


@pytest.mark.asyncio
async def test_sql_snapshot_replacement_and_stale_expiry(
    device_database: DeviceDatabase,
) -> None:
    agent_id = await _register(
        device_database,
        team_id=TEAM_A_ID,
        name="Ubuntu Agent",
    )
    first = await device_database.directory.replace_heartbeat(
        agent_id=agent_id,
        heartbeat=_heartbeat(hostname="Ubuntu Agent"),
        devices=(
            _observation(device_database, serial="DEVICE0001"),
            _observation(device_database, serial="DEVICE0002"),
        ),
    )
    second = await device_database.directory.replace_heartbeat(
        agent_id=agent_id,
        heartbeat=_heartbeat(hostname="Ubuntu Agent"),
        devices=(_observation(device_database, serial="DEVICE0001"),),
    )

    assert second.devices[0].device_id == first.devices[0].device_id
    assert {
        device.serial_suffix: device.state
        for device in await device_database.directory.list_devices(team_id=TEAM_A_ID)
    } == {"0001": "ready", "0002": "offline"}

    device_database.clock.advance(seconds=31)
    assert await device_database.directory.expire_stale() == 1
    assert {
        device.state for device in await device_database.directory.list_devices(team_id=TEAM_A_ID)
    } == {"offline"}


@pytest.mark.asyncio
async def test_device_movement_is_fenced_by_active_lease(
    device_database: DeviceDatabase,
) -> None:
    first_agent_id = await _register(
        device_database,
        team_id=TEAM_A_ID,
        name="First Agent",
    )
    second_agent_id = await _register(
        device_database,
        team_id=TEAM_A_ID,
        name="Second Agent",
    )
    first = await device_database.directory.replace_heartbeat(
        agent_id=first_agent_id,
        heartbeat=_heartbeat(hostname="First Agent"),
        devices=(_observation(device_database, serial="MOVING0001"),),
    )
    device_id = first.devices[0].device_id
    async with device_database.sessions.begin() as session:
        job = GlobalJob(
            team_id=TEAM_A_ID,
            idempotency_key="device-movement-fence",
            analysis_mode="device",
            state="running",
            selected_device_id=device_id,
        )
        session.add(job)
        await session.flush()
        session.add(
            AgentLease(
                device_id=device_id,
                agent_id=first_agent_id,
                global_job_id=job.id,
                execution_id=uuid4(),
                lease_token_digest="a" * 64,
                state="active",
                acquired_at=NOW,
                renewed_at=NOW,
                expires_at=NOW + timedelta(minutes=1),
            )
        )

    await device_database.directory.replace_heartbeat(
        agent_id=first_agent_id,
        heartbeat=_heartbeat(hostname="First Agent"),
        devices=(
            _observation(
                device_database,
                serial="MOVING0001",
                adb_state="unauthorized",
            ),
        ),
    )
    assert (await device_database.directory.list_devices(team_id=TEAM_A_ID))[
        0
    ].state == "unauthorized"

    with pytest.raises(DeviceHeartbeatRejected):
        await device_database.directory.replace_heartbeat(
            agent_id=second_agent_id,
            heartbeat=_heartbeat(hostname="Second Agent"),
            devices=(_observation(device_database, serial="MOVING0001"),),
        )

    async with device_database.sessions.begin() as session:
        await session.execute(
            update(AgentLease)
            .where(AgentLease.device_id == device_id)
            .values(state="released", released_at=NOW)
        )
    moved = await device_database.directory.replace_heartbeat(
        agent_id=second_agent_id,
        heartbeat=_heartbeat(hostname="Second Agent"),
        devices=(_observation(device_database, serial="MOVING0001"),),
    )
    assert moved.devices[0].device_id == device_id
    async with device_database.sessions() as session:
        stored = await session.get(Device, device_id)
    assert stored is not None
    assert stored.agent_id == second_agent_id


@pytest.mark.asyncio
async def test_device_digest_cannot_move_between_teams(
    device_database: DeviceDatabase,
) -> None:
    team_a_agent = await _register(
        device_database,
        team_id=TEAM_A_ID,
        name="Team A Agent",
    )
    team_b_agent = await _register(
        device_database,
        team_id=TEAM_B_ID,
        name="Team B Agent",
    )
    await device_database.directory.replace_heartbeat(
        agent_id=team_a_agent,
        heartbeat=_heartbeat(hostname="Team A Agent"),
        devices=(_observation(device_database, serial="CROSS0001"),),
    )

    with pytest.raises(DeviceHeartbeatRejected):
        await device_database.directory.replace_heartbeat(
            agent_id=team_b_agent,
            heartbeat=_heartbeat(hostname="Team B Agent"),
            devices=(_observation(device_database, serial="CROSS0001"),),
        )

    assert await device_database.directory.list_devices(team_id=TEAM_B_ID) == ()
