from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import inspect, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.control.models import EngineExecution, GlobalJob, Team
from perfpilot_api.engines.contracts import EngineRunRef
from perfpilot_api.services.engine_executions import (
    EngineExecutionNotFoundError,
    EngineExecutionOwnershipError,
    EngineExecutionSeed,
    SQLAlchemyEngineExecutionRepository,
    StaleEngineExecutionVersionError,
)


TEAM_ID = UUID("d1000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("d1000000-0000-4000-8000-000000000002")
ANALYSIS_ID = UUID("d2000000-0000-4000-8000-000000000001")
MEMORY_ANALYSIS_ID = UUID("d2000000-0000-4000-8000-000000000002")
DEVICE_ANALYSIS_ID = UUID("d2000000-0000-4000-8000-000000000003")
EXECUTION_NAMESPACE_RESULT = UUID("a1c50ce0-6144-553e-8721-18f466991f32")
NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "control"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL execution tests")
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
class ExecutionDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    repository: SQLAlchemyEngineExecutionRepository


@pytest.fixture
async def execution_database() -> AsyncIterator[ExecutionDatabase]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_engine_execution_{uuid4().hex}"
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
                    Team(id=TEAM_ID, name="Execution Team", state="active"),
                    Team(id=OTHER_TEAM_ID, name="Other Team", state="active"),
                )
            )
            await session.flush()
            session.add_all(
                (
                    GlobalJob(
                        id=ANALYSIS_ID,
                        team_id=TEAM_ID,
                        idempotency_key="trace-analysis",
                        analysis_mode="trace_upload",
                        state="analyzing",
                        retry_count=0,
                        max_retries=2,
                        started_at=NOW,
                        version=1,
                    ),
                    GlobalJob(
                        id=MEMORY_ANALYSIS_ID,
                        team_id=TEAM_ID,
                        idempotency_key="memory-analysis",
                        analysis_mode="memory_upload",
                        state="analyzing",
                        retry_count=0,
                        max_retries=2,
                        started_at=NOW,
                        version=1,
                    ),
                    GlobalJob(
                        id=DEVICE_ANALYSIS_ID,
                        team_id=TEAM_ID,
                        idempotency_key="device-analysis",
                        analysis_mode="device",
                        state="analyzing",
                        retry_count=0,
                        max_retries=2,
                        started_at=NOW,
                        version=1,
                    ),
                )
            )
        yield ExecutionDatabase(
            engine=engine,
            sessions=sessions,
            repository=SQLAlchemyEngineExecutionRepository(sessions),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def _seed(*, engine_id: str = "smartperfetto") -> EngineExecutionSeed:
    return EngineExecutionSeed(
        engine_id=engine_id,
        adapter_version="1.0.0",
        engine_commit_sha="a" * 40,
        engine_image_digest="sha256:" + "b" * 64,
        input_manifest_hash="c" * 64,
        config_hash="d" * 64,
    )


def _run_ref(*, run_id: str = "run-1", cursor: str | None = None) -> EngineRunRef:
    return EngineRunRef(
        "smartperfetto",
        "session-1",
        run_id,
        cursor,
        "workspace-1",
    )


def _memory_run_ref(*, run_id: str, cursor: str | None = None) -> EngineRunRef:
    return EngineRunRef("android_memory", None, run_id, cursor, None)


async def _running(database: ExecutionDatabase):
    pending = await database.repository.allocate_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        seed=_seed(),
        now=NOW,
    )
    return await database.repository.mark_submitted(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=pending.id,
        expected_version=pending.version,
        run_ref=_run_ref(),
        now=NOW,
    )


@pytest.mark.asyncio
async def test_attempt_allocation_serializes_and_increments(
    execution_database: ExecutionDatabase,
) -> None:
    attempts = await asyncio.gather(
        execution_database.repository.allocate_attempt(
            team_id=TEAM_ID, analysis_id=ANALYSIS_ID, seed=_seed(), now=NOW
        ),
        execution_database.repository.allocate_attempt(
            team_id=TEAM_ID, analysis_id=ANALYSIS_ID, seed=_seed(), now=NOW
        ),
    )

    assert sorted(record.attempt_number for record in attempts) == [1, 2]
    assert all(record.state == "pending" for record in attempts)


@pytest.mark.asyncio
async def test_allocation_accepts_only_the_analysis_engine_pair(
    execution_database: ExecutionDatabase,
) -> None:
    memory = await execution_database.repository.allocate_attempt(
        team_id=TEAM_ID,
        analysis_id=MEMORY_ANALYSIS_ID,
        seed=_seed(engine_id="android_memory"),
        now=NOW,
    )

    assert memory.engine_id == "android_memory"
    assert memory.engine_image_digest == "sha256:" + "b" * 64
    for analysis_id, engine_id in (
        (ANALYSIS_ID, "android_memory"),
        (MEMORY_ANALYSIS_ID, "smartperfetto"),
        (DEVICE_ANALYSIS_ID, "smartperfetto"),
        (DEVICE_ANALYSIS_ID, "android_memory"),
    ):
        with pytest.raises(EngineExecutionNotFoundError):
            await execution_database.repository.allocate_attempt(
                team_id=TEAM_ID,
                analysis_id=analysis_id,
                seed=_seed(engine_id=engine_id),
                now=NOW,
            )


@pytest.mark.asyncio
async def test_submit_reference_is_scoped_and_version_protected(
    execution_database: ExecutionDatabase,
) -> None:
    pending = await execution_database.repository.allocate_attempt(
        team_id=TEAM_ID, analysis_id=ANALYSIS_ID, seed=_seed(), now=NOW
    )
    running = await execution_database.repository.mark_submitted(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=pending.id,
        expected_version=pending.version,
        run_ref=_run_ref(),
        now=NOW,
    )

    assert running.state == "running"
    assert running.external_workspace_id == "workspace-1"
    assert running.external_session_id == "session-1"
    assert running.external_run_id == "run-1"
    with pytest.raises(StaleEngineExecutionVersionError):
        await execution_database.repository.mark_submitted(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=pending.id,
            expected_version=pending.version,
            run_ref=_run_ref(run_id="overwrite"),
            now=NOW,
        )
    with pytest.raises(EngineExecutionNotFoundError):
        await execution_database.repository.get(
            team_id=OTHER_TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=pending.id,
        )


@pytest.mark.asyncio
async def test_memory_submit_accepts_null_workspace_and_session_and_fences_run_identity(
    execution_database: ExecutionDatabase,
) -> None:
    pending = await execution_database.repository.allocate_attempt(
        team_id=TEAM_ID,
        analysis_id=MEMORY_ANALYSIS_ID,
        seed=_seed(engine_id="android_memory"),
        now=NOW,
    )
    running = await execution_database.repository.mark_submitted(
        team_id=TEAM_ID,
        analysis_id=MEMORY_ANALYSIS_ID,
        execution_id=pending.id,
        expected_version=pending.version,
        run_ref=_memory_run_ref(run_id=f"memory-{pending.id.hex}"),
        now=NOW,
    )
    observed = await execution_database.repository.persist_observation(
        team_id=TEAM_ID,
        analysis_id=MEMORY_ANALYSIS_ID,
        execution_id=running.id,
        expected_version=running.version,
        run_ref=_memory_run_ref(run_id=f"memory-{pending.id.hex}", cursor="1"),
        target_state="running",
        stable_error_code=None,
        now=NOW,
    )

    assert observed.external_workspace_id is None
    assert observed.external_session_id is None
    assert observed.external_run_id == f"memory-{pending.id.hex}"
    with pytest.raises(EngineExecutionOwnershipError):
        await execution_database.repository.persist_observation(
            team_id=TEAM_ID,
            analysis_id=MEMORY_ANALYSIS_ID,
            execution_id=running.id,
            expected_version=observed.version,
            run_ref=_memory_run_ref(run_id="memory-22222222222222222222222222222222", cursor="2"),
            target_state="running",
            stable_error_code=None,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_cursor_is_monotonic_and_recovered_run_update_is_cas_protected(
    execution_database: ExecutionDatabase,
) -> None:
    running = await _running(execution_database)
    observed = await execution_database.repository.persist_observation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=running.id,
        expected_version=running.version,
        run_ref=_run_ref(run_id="run-recovered", cursor="7"),
        target_state="running",
        stable_error_code=None,
        now=NOW,
    )
    duplicate = await execution_database.repository.persist_observation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=running.id,
        expected_version=observed.version,
        run_ref=_run_ref(run_id="run-recovered", cursor="7"),
        target_state="running",
        stable_error_code=None,
        now=NOW,
    )

    assert observed.external_run_id == "run-recovered"
    assert observed.last_event_cursor == "7"
    assert duplicate.version == observed.version
    with pytest.raises(ValueError):
        await execution_database.repository.persist_observation(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=running.id,
            expected_version=observed.version,
            run_ref=_run_ref(run_id="run-recovered", cursor="6"),
            target_state="running",
            stable_error_code=None,
            now=NOW,
        )
    with pytest.raises(ValueError):
        await execution_database.repository.persist_observation(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=running.id,
            expected_version=observed.version,
            run_ref=_run_ref(run_id="run-recovered", cursor="8"),
            target_state="completed",
            stable_error_code=None,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_finalization_claim_is_deterministic_and_completion_requires_it(
    execution_database: ExecutionDatabase,
) -> None:
    running = await _running(execution_database)
    claims = await asyncio.gather(
        execution_database.repository.claim_finalization(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=running.id,
            now=NOW,
        ),
        execution_database.repository.claim_finalization(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=running.id,
            now=NOW,
        ),
    )

    assert claims[0].record.raw_result_artifact_id == claims[1].record.raw_result_artifact_id
    assert sum(claim.is_owner for claim in claims) == 1
    claimed = claims[0].record
    completed = await execution_database.repository.finalize(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=claimed.id,
        expected_version=claimed.version,
        artifact_id=claimed.raw_result_artifact_id,
        terminal_state="completed",
        now=NOW,
    )
    assert completed.state == "completed"
    assert completed.raw_result_artifact_id == claimed.raw_result_artifact_id
    assert completed.stable_error_code is None


@pytest.mark.asyncio
async def test_concurrent_capacity_retry_reserves_one_next_attempt(
    execution_database: ExecutionDatabase,
) -> None:
    running = await _running(execution_database)
    reservations = await asyncio.gather(
        execution_database.repository.reserve_retry(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=running.id,
            stable_error_code="capacity_exceeded",
            now=NOW,
            deadline_seconds=1800,
        ),
        execution_database.repository.reserve_retry(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=running.id,
            stable_error_code="capacity_exceeded",
            now=NOW,
            deadline_seconds=1800,
        ),
    )

    assert reservations[0].next_attempt == reservations[1].next_attempt
    assert reservations[0].next_attempt is not None
    assert reservations[0].next_attempt.attempt_number == 2
    async with execution_database.sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        rows = list(
            (
                await session.scalars(
                    select(EngineExecution).order_by(EngineExecution.attempt_number)
                )
            ).all()
        )
    assert job is not None and job.retry_count == 1
    assert [(row.attempt_number, row.state) for row in rows] == [(1, "failed"), (2, "pending")]


def test_control_execution_schema_has_no_payload_or_request_material() -> None:
    columns = {column.key for column in inspect(EngineExecution).columns}

    assert columns.isdisjoint(
        {"payload", "report", "query", "url", "headers", "object_key", "evidence"}
    )
