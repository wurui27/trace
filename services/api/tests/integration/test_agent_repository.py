from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import sql
from sqlalchemy import text
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
    AgentNameConflictError,
    AgentRegistration,
    AgentRegistrationRejected,
    AgentService,
    SQLAlchemyAgentRepository,
    TaskSigningKey,
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
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL Agent tests")
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
def agent_database_url() -> Iterator[URL]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_agent_repository_{uuid4().hex}"
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
class AgentDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    repository: SQLAlchemyAgentRepository
    service: AgentService
    private_key: Ed25519PrivateKey


class CountingEntropy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, size: int) -> bytes:
        self.calls += 1
        return self.calls.to_bytes(4, "big") * (size // 4)


@pytest.fixture
async def agent_database(agent_database_url: URL) -> AsyncIterator[AgentDatabase]:
    engine = create_async_engine(agent_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        await session.execute(text("TRUNCATE TABLE users, teams CASCADE"))
        session.add_all(
            (
                User(
                    id=USER_ID,
                    username="agent-owner",
                    password_hash="unused",
                    state="active",
                ),
                Team(id=TEAM_A_ID, name="Agent Team A", state="active"),
                Team(id=TEAM_B_ID, name="Agent Team B", state="active"),
            )
        )
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    task_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    repository = SQLAlchemyAgentRepository(sessions)
    service = AgentService(
        repository=repository,
        credentials=AgentCredentialCodec(b"c" * 32, entropy=CountingEntropy()),
        nonce_store=InMemoryAgentNonceStore(
            key_secret=b"n" * 32,
            clock=lambda: NOW.timestamp(),
        ),
        task_signing_key=TaskSigningKey(
            kid="repository-test",
            public_key_b64=encode_ed25519_public_key(task_key.public_key()),
        ),
        clock=lambda: NOW,
    )
    try:
        yield AgentDatabase(
            engine=engine,
            sessions=sessions,
            repository=repository,
            service=service,
            private_key=private_key,
        )
    finally:
        await engine.dispose()


def _registration(code: str, private_key: Ed25519PrivateKey) -> AgentRegistration:
    return AgentRegistration(
        registration_code=code,
        public_key_b64=encode_ed25519_public_key(private_key.public_key()),
        platform="linux",
        agent_version="1.2.3",
        hostname="Ubuntu Agent",
        os_version="Ubuntu 24.04",
    )


async def _register(database: AgentDatabase):
    issued = await database.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name="Ubuntu Agent",
    )
    registered = await database.service.register(
        _registration(issued.registration_code, database.private_key)
    )
    return issued, registered


@pytest.mark.asyncio
async def test_sql_repository_consumes_registration_once_under_concurrency(
    agent_database: AgentDatabase,
) -> None:
    issued = await agent_database.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name="Ubuntu Agent",
    )
    request = _registration(issued.registration_code, agent_database.private_key)

    results = await asyncio.gather(
        agent_database.service.register(request),
        agent_database.service.register(request),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, AgentRegistrationRejected) for result in results) == 1


@pytest.mark.asyncio
async def test_sql_repository_translates_only_team_name_conflict(
    agent_database: AgentDatabase,
) -> None:
    await agent_database.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name="Ubuntu Agent",
    )

    with pytest.raises(AgentNameConflictError):
        await agent_database.service.create_registration_code(
            team_id=TEAM_A_ID,
            owner_user_id=USER_ID,
            name="Ubuntu Agent",
        )


@pytest.mark.asyncio
async def test_sql_repository_never_lists_another_team_agent(
    agent_database: AgentDatabase,
) -> None:
    await _register(agent_database)

    assert len(await agent_database.service.list_agents(team_id=TEAM_A_ID)) == 1
    assert await agent_database.service.list_agents(team_id=TEAM_B_ID) == ()


@pytest.mark.asyncio
async def test_sql_revoke_clears_credentials_and_revokes_active_lease(
    agent_database: AgentDatabase,
) -> None:
    _, registered = await _register(agent_database)
    device = Device(
        team_id=TEAM_A_ID,
        agent_id=registered.agent_id,
        serial_digest="a" * 64,
        serial_suffix="7K2A",
        connection_type="usb",
        adb_state="device",
        state="busy",
        last_seen_at=NOW,
    )
    async with agent_database.sessions.begin() as session:
        session.add(device)
        await session.flush()
        job = GlobalJob(
            team_id=TEAM_A_ID,
            idempotency_key="agent-revoke-lease",
            analysis_mode="device",
            state="running",
            selected_device_id=device.id,
        )
        session.add(job)
        await session.flush()
        lease = AgentLease(
            device_id=device.id,
            agent_id=registered.agent_id,
            global_job_id=job.id,
            execution_id=uuid4(),
            lease_token_digest="b" * 64,
            state="active",
            acquired_at=NOW,
            renewed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        session.add(lease)
        await session.flush()
        lease_id = lease.id

    await agent_database.service.revoke(
        team_id=TEAM_A_ID,
        agent_id=registered.agent_id,
    )

    async with agent_database.sessions() as session:
        stored_agent = await session.get(Agent, registered.agent_id)
        stored_lease = await session.get(AgentLease, lease_id)
        stored_device = await session.get(Device, device.id)
    assert stored_agent is not None
    assert stored_agent.token_digest is None
    assert stored_agent.refresh_token_digest is None
    assert stored_agent.public_key_b64 is None
    assert stored_lease is not None
    assert stored_lease.state == "revoked"
    assert stored_lease.released_at == NOW
    assert stored_device is not None
    assert stored_device.state == "offline"
