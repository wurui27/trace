from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.tenant.models import Analysis, Application, ApplicationVersion, Artifact
from perfpilot_api.engines.canonical_results import (
    EngineResultValidationError as CanonicalError,
    result_artifact_id,
)
from perfpilot_api.services.engine_result_artifacts import (
    EngineResultArtifactRecord,
    EngineResultConflictError,
    SQLAlchemyEngineResultArtifactRepository,
    EngineResultUnavailableError,
    EngineResultValidationError,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_A = UUID("81000000-0000-4000-8000-000000000001")
TEAM_B = UUID("81000000-0000-4000-8000-000000000002")
MEMORY_ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
TRACE_ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000002")
OTHER_ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000003")
DEVICE_ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000004")
TEAM_B_ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000005")
MISSING_ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000099")
EXECUTION_ID = UUID("83000000-0000-4000-8000-000000000001")
ARTIFACT_ID = result_artifact_id(EXECUTION_ID)
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
REQUEST_HASH = "a" * 64
CHECKSUM = base64.b64encode(b"c" * 32).decode("ascii")
OTHER_CHECKSUM = base64.b64encode(b"d" * 32).decode("ascii")
SIZE_BYTES = 321
TENANT_A = TenantBucket(team_id=TEAM_A, bucket="private-team-a", resource_version=7)
TENANT_B = TenantBucket(team_id=TEAM_B, bucket="private-team-b", resource_version=11)

_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "tenant"
_WAITING_ARTIFACT_DML_LOCKS = text(
    """
    SELECT count(*)
    FROM pg_catalog.pg_locks
    WHERE locktype = 'relation'
      AND database = (
          SELECT oid
          FROM pg_catalog.pg_database
          WHERE datname = current_database()
      )
      AND relation = pg_catalog.to_regclass('public.artifacts')
      AND mode = 'RowExclusiveLock'
      AND granted = false
    """
)
_T = TypeVar("_T")


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run engine result artifact tests")
    url = make_url(raw_url)
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        pytest.fail(f"{_POSTGRES_URL_ENV} must be a PostgreSQL psycopg URL")
    return url


def _conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _migration_config(url: URL) -> Config:
    config = Config(str(_MIGRATION_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_MIGRATION_ROOT))
    config.attributes["sqlalchemy_url"] = url
    return config


@dataclass
class TenantDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


class MutableTenantRouter:
    def __init__(
        self,
        routes: dict[UUID, async_sessionmaker[AsyncSession]],
        versions: dict[UUID, object],
    ) -> None:
        self.routes = routes
        self.versions = versions
        self.calls: list[UUID] = []
        self.failure: BaseException | None = None

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        self.calls.append(team_id)
        if self.failure is not None:
            raise self.failure
        async with self.routes[team_id].begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = self.versions[team_id]
            yield session


@dataclass
class ArtifactHarness:
    databases: dict[UUID, TenantDatabase]
    router: MutableTenantRouter
    repository: SQLAlchemyEngineResultArtifactRepository

    async def rows(self, team_id: UUID) -> list[Artifact]:
        async with self.databases[team_id].sessions() as session:
            return list((await session.scalars(select(Artifact).order_by(Artifact.id))).all())


async def _seed_tenant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
    analyses: tuple[tuple[UUID, str], ...],
) -> None:
    application_id = uuid4()
    version_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            Application(
                id=application_id,
                name=f"Engine Result {suffix}",
                package_name=f"dev.perfpilot.engine.{suffix}",
            )
        )
        await session.flush()
        session.add(
            ApplicationVersion(
                id=version_id,
                application_id=application_id,
                package_name=f"dev.perfpilot.engine.{suffix}",
                version_name="1.0",
                version_code=1,
                supported_abis=[],
            )
        )
        await session.flush()
        session.add_all(
            Analysis(
                id=analysis_id,
                application_version_id=version_id,
                analysis_mode=analysis_mode,
                state="created",
                version=1,
                **(
                    {"analysis_profile": "auto", "input_manifest": []}
                    if analysis_mode == "trace_upload"
                    else {}
                ),
            )
            for analysis_id, analysis_mode in analyses
        )


@pytest.fixture
async def artifact_harness() -> AsyncIterator[ArtifactHarness]:
    admin_url = _postgres_url()
    names = {
        TEAM_A: f"perfpilot_engine_result_a_{uuid4().hex}",
        TEAM_B: f"perfpilot_engine_result_b_{uuid4().hex}",
    }
    created: list[str] = []
    databases: dict[UUID, TenantDatabase] = {}
    try:
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            for name in names.values():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
                )
                created.append(name)
        for team_id, name in names.items():
            url = admin_url.set(database=name)
            command.upgrade(_migration_config(url), "head")
            engine = create_async_engine(url)
            databases[team_id] = TenantDatabase(
                engine=engine,
                sessions=async_sessionmaker(engine, expire_on_commit=False),
            )
        await _seed_tenant(
            databases[TEAM_A].sessions,
            suffix="a",
            analyses=(
                (MEMORY_ANALYSIS_ID, "memory_upload"),
                (TRACE_ANALYSIS_ID, "trace_upload"),
                (OTHER_ANALYSIS_ID, "memory_upload"),
                (DEVICE_ANALYSIS_ID, "device"),
            ),
        )
        await _seed_tenant(
            databases[TEAM_B].sessions,
            suffix="b",
            analyses=((TEAM_B_ANALYSIS_ID, "memory_upload"),),
        )
        router = MutableTenantRouter(
            {team_id: database.sessions for team_id, database in databases.items()},
            {TEAM_A: 7, TEAM_B: 11},
        )
        yield ArtifactHarness(
            databases=databases,
            router=router,
            repository=SQLAlchemyEngineResultArtifactRepository(
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
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                    )


def _expected_values(
    *,
    execution_id: UUID = EXECUTION_ID,
    analysis_id: UUID = MEMORY_ANALYSIS_ID,
) -> dict[str, object]:
    artifact_id = result_artifact_id(execution_id)
    return {
        "id": artifact_id,
        "analysis_id": analysis_id,
        "upload_id": artifact_id,
        "idempotency_key": f"internal:engine_result:{execution_id}",
        "request_hash": REQUEST_HASH,
        "artifact_kind": "engine_result",
        "mime_type": "application/json",
        "size_bytes": SIZE_BYTES,
        "sha256_b64": CHECKSUM,
        "object_key": (
            f"raw/analyses/{analysis_id}/internal/engine-results/{artifact_id}.json"
        ),
        "version_id": None,
        "state": "pending",
        "finalized_at": None,
        "expires_at": NOW + timedelta(days=30),
        "deleted_at": None,
        "version": 1,
    }


async def _reserve(
    harness: ArtifactHarness,
    *,
    tenant: TenantBucket = TENANT_A,
    analysis_id: UUID = MEMORY_ANALYSIS_ID,
    execution_id: UUID = EXECUTION_ID,
    artifact_id: UUID = ARTIFACT_ID,
    engine_id: str = "android_memory",
) -> EngineResultArtifactRecord:
    return await harness.repository.reserve(
        tenant=tenant,
        analysis_id=analysis_id,
        execution_id=execution_id,
        artifact_id=artifact_id,
        engine_id=engine_id,
        request_hash=REQUEST_HASH,
        size_bytes=SIZE_BYTES,
        sha256_b64=CHECKSUM,
        now=NOW,
    )


async def _insert_artifact(
    harness: ArtifactHarness,
    values: dict[str, object],
) -> None:
    async with harness.databases[TEAM_A].sessions.begin() as session:
        session.add(Artifact(**values))  # type: ignore[arg-type]


async def _wait_for_two_blocked_artifact_dml(database: TenantDatabase) -> int:
    deadline = asyncio.get_running_loop().time() + 5
    async with database.engine.connect() as observer:
        while True:
            waiting = await observer.scalar(_WAITING_ARTIFACT_DML_LOCKS)
            if type(waiting) is int and waiting >= 2:
                return waiting
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"expected two blocked Artifact DML locks, observed {waiting!r}"
                )
            await asyncio.sleep(0.01)


async def _run_with_artifact_dml_blocked(
    database: TenantDatabase,
    first: Callable[[], Awaitable[_T]],
    second: Callable[[], Awaitable[_T]],
) -> tuple[_T, _T, int]:
    tasks: list[asyncio.Task[_T]] = []
    async with database.engine.connect() as blocker:
        transaction = await blocker.begin()
        try:
            await blocker.execute(text("LOCK TABLE artifacts IN SHARE MODE"))
            tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
            waiting = await _wait_for_two_blocked_artifact_dml(database)
            await transaction.commit()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            return results[0], results[1], waiting
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                if transaction.is_active:
                    await transaction.rollback()
            finally:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)


def test_repository_exposes_only_stable_redacted_errors() -> None:
    secret = "private-bucket/object-key/version-id"

    assert EngineResultValidationError is CanonicalError
    conflict = EngineResultConflictError(secret)
    unavailable = EngineResultUnavailableError(secret)
    assert str(conflict) == "engine result integrity conflict"
    assert str(unavailable) == "engine result service is unavailable"
    assert secret not in repr(conflict)
    assert secret not in repr(unavailable)


@pytest.mark.asyncio
async def test_guard_redacts_integrity_error_sql_and_params_without_chaining() -> None:
    secret = "private SQL, object key, and bound params"

    async def dependency_failure() -> None:
        raise IntegrityError(
            f"INSERT INTO artifacts VALUES ('{secret}')",
            {"private": secret},
            RuntimeError(secret),
        )

    with pytest.raises(EngineResultConflictError) as caught:
        await SQLAlchemyEngineResultArtifactRepository._guard(dependency_failure())

    assert str(caught.value) == "engine result integrity conflict"
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_engine_mode_pairs_reserve_only_in_the_selected_tenant_database(
    artifact_harness: ArtifactHarness,
) -> None:
    memory = await _reserve(artifact_harness)
    trace_execution = UUID("83000000-0000-4000-8000-000000000002")
    trace = await _reserve(
        artifact_harness,
        analysis_id=TRACE_ANALYSIS_ID,
        execution_id=trace_execution,
        artifact_id=result_artifact_id(trace_execution),
        engine_id="smartperfetto",
    )
    device_execution = UUID("83000000-0000-4000-8000-000000000003")
    device_trace = await _reserve(
        artifact_harness,
        analysis_id=DEVICE_ANALYSIS_ID,
        execution_id=device_execution,
        artifact_id=result_artifact_id(device_execution),
        engine_id="smartperfetto",
    )

    assert memory.artifact_id == ARTIFACT_ID
    assert memory.analysis_id == MEMORY_ANALYSIS_ID
    assert memory.upload_id == ARTIFACT_ID
    assert memory.idempotency_key == f"internal:engine_result:{EXECUTION_ID}"
    assert memory.request_hash == REQUEST_HASH
    assert memory.artifact_kind == "engine_result"
    assert memory.mime_type == "application/json"
    assert memory.size_bytes == SIZE_BYTES
    assert memory.state == "pending"
    assert memory.expires_at == NOW + timedelta(days=30)
    assert memory.version == 1
    assert memory.version_id is None
    assert CHECKSUM not in repr(memory)
    assert "raw/analyses" not in repr(memory)
    assert trace.analysis_id == TRACE_ANALYSIS_ID
    assert device_trace.analysis_id == DEVICE_ANALYSIS_ID
    assert len(await artifact_harness.rows(TEAM_A)) == 3
    assert await artifact_harness.rows(TEAM_B) == []
    assert artifact_harness.router.calls == [TEAM_A, TEAM_A, TEAM_A]


@pytest.mark.asyncio
async def test_identical_reserve_is_idempotent_and_keeps_one_pending_row(
    artifact_harness: ArtifactHarness,
) -> None:
    first = await _reserve(artifact_harness)
    replay = await _reserve(artifact_harness)

    assert replay == first
    assert len(await artifact_harness.rows(TEAM_A)) == 1


@pytest.mark.asyncio
async def test_reserve_persists_every_exact_engine_result_artifact_column(
    artifact_harness: ArtifactHarness,
) -> None:
    await _reserve(artifact_harness)

    rows = await artifact_harness.rows(TEAM_A)
    assert len(rows) == 1
    row = rows[0]
    assert (
        row.id,
        row.application_version_id,
        row.analysis_id,
        row.scenario_result_id,
        row.sample_attempt_id,
        row.upload_id,
    ) == (ARTIFACT_ID, None, MEMORY_ANALYSIS_ID, None, None, ARTIFACT_ID)
    assert row.idempotency_key == f"internal:engine_result:{EXECUTION_ID}"
    assert row.request_hash == REQUEST_HASH
    assert row.artifact_kind == "engine_result"
    assert row.mime_type == "application/json"
    assert row.size_bytes == SIZE_BYTES
    assert row.sha256_b64 == CHECKSUM
    assert row.object_key == (
        f"raw/analyses/{MEMORY_ANALYSIS_ID}/internal/engine-results/{ARTIFACT_ID}.json"
    )
    assert row.object_key.endswith(".json")
    assert row.state == "pending"
    assert row.version_id is None
    assert row.finalized_at is None
    assert row.expires_at == NOW + timedelta(days=30)
    assert row.deleted_at is None
    assert row.version == 1


@pytest.mark.asyncio
async def test_valid_finalized_reservation_replays_the_same_record(
    artifact_harness: ArtifactHarness,
) -> None:
    reserved = await _reserve(artifact_harness)
    finalized = await artifact_harness.repository.finalize(
        tenant=TENANT_A,
        analysis_id=MEMORY_ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        expected_version=reserved.version,
        storage_version_id="opaque-final-version",
        now=NOW + timedelta(minutes=1),
        expires_at=reserved.expires_at,
    )

    assert finalized is not None
    assert await _reserve(artifact_harness) == finalized
    assert len(await artifact_harness.rows(TEAM_A)) == 1


@pytest.mark.asyncio
async def test_deleted_expired_and_malformed_existing_shapes_are_conflicts(
    artifact_harness: ArtifactHarness,
) -> None:
    cases: tuple[dict[str, object], ...] = (
        {"state": "deleted", "deleted_at": NOW},
        {"state": "expired"},
        {"state": "pending", "expires_at": NOW - timedelta(seconds=1)},
        {"state": "pending", "version": 2},
        {
            "state": "finalized",
            "version": 3,
            "version_id": "malformed-final-version",
            "finalized_at": NOW,
        },
    )
    for overrides in cases:
        values = _expected_values()
        values.update(overrides)
        await _insert_artifact(artifact_harness, values)
        with pytest.raises(EngineResultConflictError):
            await _reserve(artifact_harness)
        async with artifact_harness.databases[TEAM_A].sessions.begin() as session:
            await session.execute(Artifact.__table__.delete())


@pytest.mark.asyncio
async def test_require_resource_version_accepts_the_exact_active_route(
    artifact_harness: ArtifactHarness,
) -> None:
    assert await artifact_harness.repository.require_resource_version(TENANT_A) is None
    assert artifact_harness.router.calls == [TEAM_A]
    assert await artifact_harness.rows(TEAM_A) == []


@pytest.mark.asyncio
async def test_other_route_missing_analysis_and_cross_mode_are_stable_conflicts(
    artifact_harness: ArtifactHarness,
) -> None:
    cases = (
        (TENANT_B, MEMORY_ANALYSIS_ID, "android_memory"),
        (TENANT_A, MISSING_ANALYSIS_ID, "android_memory"),
        (TENANT_A, MEMORY_ANALYSIS_ID, "smartperfetto"),
        (TENANT_A, TRACE_ANALYSIS_ID, "android_memory"),
        (TENANT_A, DEVICE_ANALYSIS_ID, "android_memory"),
    )
    for tenant, analysis_id, engine_id in cases:
        with pytest.raises(EngineResultConflictError) as caught:
            await _reserve(
                artifact_harness,
                tenant=tenant,
                analysis_id=analysis_id,
                engine_id=engine_id,
            )
        assert str(caught.value) == "engine result integrity conflict"

    assert await artifact_harness.rows(TEAM_A) == []
    assert await artifact_harness.rows(TEAM_B) == []


@pytest.mark.asyncio
async def test_artifact_identity_must_be_derived_from_execution(
    artifact_harness: ArtifactHarness,
) -> None:
    with pytest.raises(EngineResultConflictError):
        await _reserve(artifact_harness, artifact_id=uuid4())
    assert await artifact_harness.rows(TEAM_A) == []


@pytest.mark.asyncio
async def test_existing_deterministic_row_compares_every_identity_and_metadata_field(
    artifact_harness: ArtifactHarness,
) -> None:
    mismatches: tuple[tuple[str, object], ...] = (
        ("analysis_id", OTHER_ANALYSIS_ID),
        ("upload_id", uuid4()),
        ("idempotency_key", "internal:engine_result:another-execution"),
        ("request_hash", "b" * 64),
        ("artifact_kind", "trace"),
        ("mime_type", "text/plain"),
        ("size_bytes", SIZE_BYTES + 1),
        ("sha256_b64", OTHER_CHECKSUM),
        ("object_key", "private/customer/object-key-marker"),
    )
    for field_name, value in mismatches:
        values = _expected_values()
        values[field_name] = value
        await _insert_artifact(artifact_harness, values)
        with pytest.raises(EngineResultConflictError) as caught:
            await _reserve(artifact_harness)
        assert str(caught.value) == "engine result integrity conflict"
        assert "private/customer" not in repr(caught.value)
        async with artifact_harness.databases[TEAM_A].sessions.begin() as session:
            row = await session.get(Artifact, ARTIFACT_ID)
            assert row is not None
            await session.delete(row)


@pytest.mark.asyncio
async def test_non_primary_unique_collisions_keep_the_existing_row_and_map_to_conflict(
    artifact_harness: ArtifactHarness,
) -> None:
    expected = _expected_values()
    collisions = ("upload_id", "object_key", "idempotency_key")
    for field_name in collisions:
        values = _expected_values(execution_id=uuid4())
        values[field_name] = expected[field_name]
        await _insert_artifact(artifact_harness, values)
        with pytest.raises(EngineResultConflictError) as caught:
            await _reserve(artifact_harness)
        assert str(caught.value) == "engine result integrity conflict"
        assert len(await artifact_harness.rows(TEAM_A)) == 1
        async with artifact_harness.databases[TEAM_A].sessions.begin() as session:
            await session.execute(Artifact.__table__.delete())


@pytest.mark.asyncio
async def test_reserve_share_locks_analysis_against_tombstone_toctou(
    artifact_harness: ArtifactHarness,
) -> None:
    updater = artifact_harness.databases[TEAM_A].sessions()
    transaction = await updater.begin()
    await updater.execute(
        update(Analysis)
        .where(Analysis.id == MEMORY_ANALYSIS_ID)
        .values(tombstoned_at=NOW)
    )
    reservation = asyncio.create_task(_reserve(artifact_harness))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reservation), timeout=0.1)
        await transaction.commit()
        with pytest.raises(EngineResultConflictError):
            await reservation
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await updater.close()

    assert await artifact_harness.rows(TEAM_A) == []


@pytest.mark.asyncio
async def test_finalize_is_cas_protected_and_pins_one_exact_storage_version(
    artifact_harness: ArtifactHarness,
) -> None:
    reserved = await _reserve(artifact_harness)
    expires_at = reserved.expires_at
    finalized = await artifact_harness.repository.finalize(
        tenant=TENANT_A,
        analysis_id=MEMORY_ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        expected_version=reserved.version,
        storage_version_id="opaque-version-a",
        now=NOW + timedelta(minutes=1),
        expires_at=expires_at,
    )
    stale = await artifact_harness.repository.finalize(
        tenant=TENANT_A,
        analysis_id=MEMORY_ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        expected_version=reserved.version,
        storage_version_id="opaque-version-b",
        now=NOW + timedelta(minutes=2),
        expires_at=expires_at,
    )
    reloaded = await artifact_harness.repository.reload(
        tenant=TENANT_A,
        analysis_id=MEMORY_ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
    )

    assert finalized is not None
    assert finalized.state == "finalized"
    assert finalized.version == 2
    assert finalized.version_id == "opaque-version-a"
    assert finalized.expires_at == expires_at
    assert stale is None
    assert reloaded == finalized
    assert "opaque-version-a" not in repr(reloaded)


@pytest.mark.asyncio
async def test_finalize_rejects_an_expiry_change_without_mutating_the_reservation(
    artifact_harness: ArtifactHarness,
) -> None:
    reserved = await _reserve(artifact_harness)
    changed = await artifact_harness.repository.finalize(
        tenant=TENANT_A,
        analysis_id=MEMORY_ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        expected_version=reserved.version,
        storage_version_id="must-not-persist",
        now=NOW + timedelta(minutes=1),
        expires_at=reserved.expires_at + timedelta(days=1),
    )

    assert changed is None
    row = (await artifact_harness.rows(TEAM_A))[0]
    assert (row.state, row.version, row.version_id, row.finalized_at) == (
        "pending",
        1,
        None,
        None,
    )
    assert row.expires_at == reserved.expires_at

    finalized = await artifact_harness.repository.finalize(
        tenant=TENANT_A,
        analysis_id=MEMORY_ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        expected_version=reserved.version,
        storage_version_id="exact-expiry-version",
        now=NOW + timedelta(minutes=2),
        expires_at=reserved.expires_at,
    )
    assert finalized is not None
    assert finalized.expires_at == reserved.expires_at


@pytest.mark.asyncio
async def test_resource_version_fence_precedes_every_insert_update_and_reload(
    artifact_harness: ArtifactHarness,
) -> None:
    artifact_harness.router.versions[TEAM_A] = 8
    with pytest.raises(EngineResultUnavailableError):
        await _reserve(artifact_harness)
    assert await artifact_harness.rows(TEAM_A) == []

    artifact_harness.router.versions[TEAM_A] = 7
    reserved = await _reserve(artifact_harness)
    artifact_harness.router.versions[TEAM_A] = 8
    operations = (
        artifact_harness.repository.require_resource_version(TENANT_A),
        artifact_harness.repository.reload(
            tenant=TENANT_A,
            analysis_id=MEMORY_ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
        ),
        artifact_harness.repository.finalize(
            tenant=TENANT_A,
            analysis_id=MEMORY_ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            expected_version=reserved.version,
            storage_version_id="must-not-persist",
            now=NOW,
            expires_at=NOW + timedelta(days=30),
        ),
    )
    for operation in operations:
        with pytest.raises(EngineResultUnavailableError):
            await operation

    row = (await artifact_harness.rows(TEAM_A))[0]
    assert (row.state, row.version, row.version_id) == ("pending", 1, None)


@pytest.mark.asyncio
async def test_tenant_and_session_versions_must_be_actual_positive_integers(
    artifact_harness: ArtifactHarness,
) -> None:
    initial_calls = list(artifact_harness.router.calls)
    for value in (True, 0, -1, 7.0, "7"):
        tenant = TenantBucket(TEAM_A, "private", value)  # type: ignore[arg-type]
        with pytest.raises(EngineResultUnavailableError):
            await _reserve(artifact_harness, tenant=tenant)
    assert artifact_harness.router.calls == initial_calls

    for value in (True, 0, -1, 7.0, "7"):
        artifact_harness.router.versions[TEAM_A] = value
        with pytest.raises(EngineResultUnavailableError):
            await _reserve(artifact_harness)
    assert await artifact_harness.rows(TEAM_A) == []


@pytest.mark.asyncio
async def test_concurrent_identical_reserve_and_finalize_converge_on_one_row(
    artifact_harness: ArtifactHarness,
) -> None:
    for index, storage_versions in enumerate(
        (("same-version", "same-version"), ("version-a", "version-b")),
        start=1,
    ):
        execution_id = UUID(f"83000000-0000-4000-8000-{index + 10:012d}")
        artifact_id = result_artifact_id(execution_id)
        first, second, waiting_inserts = await _run_with_artifact_dml_blocked(
            artifact_harness.databases[TEAM_A],
            lambda: _reserve(
                artifact_harness,
                execution_id=execution_id,
                artifact_id=artifact_id,
            ),
            lambda: _reserve(
                artifact_harness,
                execution_id=execution_id,
                artifact_id=artifact_id,
            ),
        )
        assert waiting_inserts >= 2
        assert first == second
        first_result, second_result, waiting_updates = await _run_with_artifact_dml_blocked(
            artifact_harness.databases[TEAM_A],
            lambda: artifact_harness.repository.finalize(
                tenant=TENANT_A,
                analysis_id=MEMORY_ANALYSIS_ID,
                artifact_id=artifact_id,
                expected_version=first.version,
                storage_version_id=storage_versions[0],
                now=NOW + timedelta(minutes=1),
                expires_at=first.expires_at,
            ),
            lambda: artifact_harness.repository.finalize(
                tenant=TENANT_A,
                analysis_id=MEMORY_ANALYSIS_ID,
                artifact_id=artifact_id,
                expected_version=first.version,
                storage_version_id=storage_versions[1],
                now=NOW + timedelta(minutes=1),
                expires_at=first.expires_at,
            ),
        )
        assert waiting_updates >= 2
        results = (first_result, second_result)
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        reloaded = await artifact_harness.repository.reload(
            tenant=TENANT_A,
            analysis_id=MEMORY_ANALYSIS_ID,
            artifact_id=artifact_id,
        )
        assert reloaded == winners[0]
        assert reloaded.version_id in set(storage_versions)

    assert len(await artifact_harness.rows(TEAM_A)) == 2


@pytest.mark.asyncio
async def test_router_failure_is_unavailable_without_private_detail(
    artifact_harness: ArtifactHarness,
) -> None:
    secret = "private database url and object marker"
    artifact_harness.router.failure = RuntimeError(secret)

    with pytest.raises(EngineResultUnavailableError) as caught:
        await _reserve(artifact_harness)

    assert str(caught.value) == "engine result service is unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)
    assert await artifact_harness.rows(TEAM_A) == []
