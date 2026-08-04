from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.tenant.models import Analysis, Application, ApplicationVersion, Artifact
from perfpilot_api.services.synthesis_artifacts import (
    SQLAlchemySynthesisArtifactRepository,
    SynthesisArtifactConflictError,
    projection_artifact_id,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_A = UUID("a1000000-0000-4000-8000-000000000001")
TEAM_B = UUID("a1000000-0000-4000-8000-000000000002")
ANALYSIS_A = UUID("a2000000-0000-4000-8000-000000000001")
ANALYSIS_B = UUID("a2000000-0000-4000-8000-000000000002")
CANONICAL_ID = UUID("a3000000-0000-4000-8000-000000000001")
ARTIFACT_ID = projection_artifact_id(CANONICAL_ID, "smartperfetto-normalizer-1")
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
CHECKSUM = base64.b64encode(b"c" * 32).decode("ascii")
OTHER_CHECKSUM = base64.b64encode(b"d" * 32).decode("ascii")
TENANT_A = TenantBucket(team_id=TEAM_A, bucket="private-a", resource_version=7)
TENANT_B = TenantBucket(team_id=TEAM_B, bucket="private-b", resource_version=11)

_MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "tenant"


def _postgres_url() -> URL:
    raw = os.getenv("PERFPILOT_TEST_POSTGRES_URL")
    if raw is None:
        pytest.skip("set PERFPILOT_TEST_POSTGRES_URL to run synthesis artifact tests")
    url = make_url(raw)
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        pytest.fail("PERFPILOT_TEST_POSTGRES_URL must be a PostgreSQL psycopg URL")
    return url


def _conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _migration_config(url: URL) -> Config:
    config = Config(str(_MIGRATION_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_MIGRATION_ROOT))
    config.attributes["sqlalchemy_url"] = url
    return config


@dataclass
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


class Router:
    def __init__(
        self,
        routes: dict[UUID, async_sessionmaker[AsyncSession]],
        versions: dict[UUID, int],
    ) -> None:
        self.routes = routes
        self.versions = versions

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        async with self.routes[team_id].begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = self.versions[team_id]
            yield session


@dataclass
class Harness:
    databases: dict[UUID, Database]
    repository: SQLAlchemySynthesisArtifactRepository


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
    analysis_id: UUID,
) -> None:
    application_id = uuid4()
    application_version_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            Application(
                id=application_id,
                name=f"Synthesis {suffix}",
                package_name=f"dev.perfpilot.synthesis.{suffix}",
            )
        )
        await session.flush()
        session.add(
            ApplicationVersion(
                id=application_version_id,
                application_id=application_id,
                package_name=f"dev.perfpilot.synthesis.{suffix}",
                version_name="1.0",
                version_code=1,
                supported_abis=[],
            )
        )
        await session.flush()
        session.add(
            Analysis(
                id=analysis_id,
                application_version_id=application_version_id,
                analysis_mode="trace_upload",
                analysis_profile="auto",
                input_manifest=[],
                state="created",
                version=1,
            )
        )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    admin_url = _postgres_url()
    names = {
        TEAM_A: f"perfpilot_synthesis_a_{uuid4().hex}",
        TEAM_B: f"perfpilot_synthesis_b_{uuid4().hex}",
    }
    databases: dict[UUID, Database] = {}
    created: list[str] = []
    try:
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            for name in names.values():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                        sql.Identifier(name)
                    )
                )
                created.append(name)
        for team_id, name in names.items():
            url = admin_url.set(database=name)
            command.upgrade(_migration_config(url), "head")
            engine = create_async_engine(url)
            databases[team_id] = Database(
                engine=engine,
                sessions=async_sessionmaker(engine, expire_on_commit=False),
            )
        await _seed(databases[TEAM_A].sessions, suffix="a", analysis_id=ANALYSIS_A)
        await _seed(databases[TEAM_B].sessions, suffix="b", analysis_id=ANALYSIS_B)
        router = Router(
            {team: database.sessions for team, database in databases.items()},
            {TEAM_A: 7, TEAM_B: 11},
        )
        yield Harness(
            databases=databases,
            repository=SQLAlchemySynthesisArtifactRepository(
                tenant_router=router,  # type: ignore[arg-type]
            ),
        )
    finally:
        for database in databases.values():
            await database.engine.dispose()
        if created:
            with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
                for name in reversed(created):
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )


async def _reserve(
    harness: Harness,
    *,
    tenant: TenantBucket = TENANT_A,
    analysis_id: UUID = ANALYSIS_A,
    request_hash: str = "a" * 64,
    checksum: str = CHECKSUM,
) -> object:
    return await harness.repository.reserve(
        tenant=tenant,
        analysis_id=analysis_id,
        artifact_id=ARTIFACT_ID,
        kind="ai_projection",
        request_hash=request_hash,
        size_bytes=321,
        sha256_b64=checksum,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_repository_reserve_finalize_reload_and_cross_team_isolation(
    harness: Harness,
) -> None:
    pending = await _reserve(harness)
    assert pending.state == "pending"  # type: ignore[union-attr]
    finalized = await harness.repository.finalize(
        tenant=TENANT_A,
        analysis_id=ANALYSIS_A,
        artifact_id=ARTIFACT_ID,
        expected_version=1,
        storage_version_id="immutable-v1",
        now=NOW,
        expires_at=pending.expires_at,  # type: ignore[union-attr]
    )
    assert finalized is not None and finalized.state == "finalized"
    reloaded = await harness.repository.reload(
        tenant=TENANT_A,
        analysis_id=ANALYSIS_A,
        artifact_id=ARTIFACT_ID,
    )
    assert reloaded.version_id == "immutable-v1"
    with pytest.raises(SynthesisArtifactConflictError):
        await _reserve(harness, tenant=TENANT_B, analysis_id=ANALYSIS_A)


@pytest.mark.asyncio
async def test_concurrent_identical_reservations_converge_and_different_bytes_conflict(
    harness: Harness,
) -> None:
    first, second = await asyncio.gather(_reserve(harness), _reserve(harness))
    assert first == second
    with pytest.raises(SynthesisArtifactConflictError):
        await _reserve(harness, request_hash="b" * 64, checksum=OTHER_CHECKSUM)

    results = await asyncio.gather(
        harness.repository.finalize(
            tenant=TENANT_A,
            analysis_id=ANALYSIS_A,
            artifact_id=ARTIFACT_ID,
            expected_version=1,
            storage_version_id="winner-a",
            now=NOW,
            expires_at=first.expires_at,
        ),
        harness.repository.finalize(
            tenant=TENANT_A,
            analysis_id=ANALYSIS_A,
            artifact_id=ARTIFACT_ID,
            expected_version=1,
            storage_version_id="winner-b",
            now=NOW,
            expires_at=first.expires_at,
        ),
    )
    assert sum(result is not None for result in results) == 1
    async with harness.databases[TEAM_A].sessions() as session:
        rows = list((await session.scalars(select(Artifact))).all())
    assert len(rows) == 1
    assert rows[0].state == "finalized"

