from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from perfpilot_api.db.control.models import Team, TeamEngineWorkspace
from perfpilot_api.services.engine_workspaces import (
    EngineWorkspaceNotFoundError,
    SQLAlchemyEngineWorkspaceRepository,
    StaleEngineWorkspaceVersionError,
)


TEAM_ID = UUID("a1000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("a1000000-0000-4000-8000-000000000002")
_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "control"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL workspace tests")
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


@dataclass
class WorkspaceDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    repository: SQLAlchemyEngineWorkspaceRepository


@pytest.fixture
async def workspace_database() -> AsyncIterator[WorkspaceDatabase]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_engine_workspace_{uuid4().hex}"
    with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name))
        )
    database_url = admin_url.set(database=database_name)
    engine: AsyncEngine | None = None
    try:
        command.upgrade(_migration_config(database_url), "head")
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add_all(
                (
                    Team(id=TEAM_ID, name="Workspace Team", state="active"),
                    Team(id=OTHER_TEAM_ID, name="Other Workspace Team", state="active"),
                )
            )
        yield WorkspaceDatabase(
            engine=engine,
            sessions=sessions,
            repository=SQLAlchemyEngineWorkspaceRepository(sessions),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


@pytest.mark.asyncio
async def test_team_and_engine_claim_one_workspace_row(
    workspace_database: WorkspaceDatabase,
) -> None:
    first = await workspace_database.repository.claim(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
    )
    second = await workspace_database.repository.claim(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
    )

    assert first.is_owner is True
    assert second.is_owner is False
    assert first.record == second.record
    assert first.record.state == "provisioning"
    assert first.record.version == 1


@pytest.mark.asyncio
async def test_concurrent_claimers_converge_on_one_row_and_one_owner(
    workspace_database: WorkspaceDatabase,
) -> None:
    claims = await asyncio.gather(
        workspace_database.repository.claim(team_id=TEAM_ID, engine_id="smartperfetto"),
        workspace_database.repository.claim(team_id=TEAM_ID, engine_id="smartperfetto"),
    )

    assert len({claim.record.id for claim in claims}) == 1
    assert sorted(claim.is_owner for claim in claims) == [False, True]
    async with workspace_database.sessions() as session:
        rows = list((await session.scalars(select(TeamEngineWorkspace))).all())
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_activation_is_expected_version_and_state_protected(
    workspace_database: WorkspaceDatabase,
) -> None:
    claim = await workspace_database.repository.claim(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
    )
    active = await workspace_database.repository.activate(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
        expected_version=claim.record.version,
        external_workspace_id="pp-team-workspace",
    )

    assert active.state == "active"
    assert active.version == 2
    assert active.external_workspace_id == "pp-team-workspace"
    with pytest.raises(StaleEngineWorkspaceVersionError):
        await workspace_database.repository.activate(
            team_id=TEAM_ID,
            engine_id="smartperfetto",
            expected_version=claim.record.version,
            external_workspace_id="pp-overwrite-attempt",
        )
    assert (
        await workspace_database.repository.get(team_id=TEAM_ID, engine_id="smartperfetto")
    ) == active


@pytest.mark.asyncio
async def test_repository_never_resolves_another_team_by_external_workspace_id(
    workspace_database: WorkspaceDatabase,
) -> None:
    claim = await workspace_database.repository.claim(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
    )
    await workspace_database.repository.activate(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
        expected_version=claim.record.version,
        external_workspace_id="pp-private-workspace",
    )

    with pytest.raises(EngineWorkspaceNotFoundError):
        await workspace_database.repository.get(
            team_id=OTHER_TEAM_ID,
            engine_id="smartperfetto",
        )
    assert not hasattr(workspace_database.repository, "get_by_external_workspace_id")


@pytest.mark.asyncio
async def test_failure_accepts_only_stable_code_and_persists_no_raw_body(
    workspace_database: WorkspaceDatabase,
) -> None:
    claim = await workspace_database.repository.claim(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
    )
    failed = await workspace_database.repository.fail(
        team_id=TEAM_ID,
        engine_id="smartperfetto",
        expected_version=claim.record.version,
        stable_error_code="engine_unavailable",
    )

    assert failed.state == "failed"
    assert "response-secret-marker" not in repr(failed)
    with pytest.raises(ValueError):
        await workspace_database.repository.fail(
            team_id=TEAM_ID,
            engine_id="smartperfetto",
            expected_version=failed.version,
            stable_error_code="response-secret-marker: raw body",
        )
