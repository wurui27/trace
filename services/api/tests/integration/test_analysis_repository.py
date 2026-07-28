from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.control.models import (
    GlobalJob,
    IdempotencyKey,
    OutboxEvent,
    ScenarioJob,
    Team,
    TenantQuota,
)
from perfpilot_api.db.tenant.models import (
    Analysis,
    Application,
    ApplicationVersion,
    Artifact,
    ReportVersion,
    ScenarioRecipe,
    ScenarioResult,
)
from perfpilot_api.services.analyses import (
    AnalysisIdempotencyConflictError,
    AnalysisNotFoundError,
    AnalysisQueueLimitError,
    AnalysisUnavailableError,
    ApkInspectionError,
    InspectedApkMetadata,
    PreparedScenario,
    ReportNotAvailableError,
    SQLAlchemyAnalysisRepository,
    SchedulingRequirements,
    StaleTaskVersionError,
    scenario_job_id,
)

TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("10000000-0000-4000-8000-000000000002")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
OTHER_ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000002")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("50000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
CHECKSUM = "iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E="
OTHER_CHECKSUM = "ERERERERERERERERERERERERERERERERERERERERERE="
REQUEST_HASH = "a" * 64
APK_MIME = "application/vnd.android.package-archive"

_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL analysis repository tests")
    url = make_url(raw_url)
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        pytest.fail(f"{_POSTGRES_URL_ENV} must be a PostgreSQL psycopg URL")
    return url


def _conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _migration_config(tree: str, url: URL) -> Config:
    migration_root = _MIGRATIONS_ROOT / tree
    config = Config(str(migration_root / "alembic.ini"))
    config.set_main_option("script_location", str(migration_root))
    config.set_main_option(
        "sqlalchemy.url",
        url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


class DirectTenantRouter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.calls: list[UUID] = []

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        self.calls.append(team_id)
        async with self._session_factory.begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = 1
            yield session


@dataclass
class AnalysisDatabases:
    control_engine: AsyncEngine
    tenant_engine: AsyncEngine
    control_sessions: async_sessionmaker[AsyncSession]
    tenant_sessions: async_sessionmaker[AsyncSession]
    tenant_router: DirectTenantRouter
    repository: SQLAlchemyAnalysisRepository


@pytest.fixture
async def analysis_databases() -> AsyncIterator[AnalysisDatabases]:
    admin_url = _postgres_url()
    database_names = {
        "control": f"perfpilot_analysis_control_{uuid4().hex}",
        "tenant": f"perfpilot_analysis_tenant_{uuid4().hex}",
    }
    created_names: list[str] = []
    engines: list[AsyncEngine] = []
    try:
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            for database_name in database_names.values():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                        sql.Identifier(database_name)
                    )
                )
                created_names.append(database_name)

        control_url = admin_url.set(database=database_names["control"])
        tenant_url = admin_url.set(database=database_names["tenant"])
        command.upgrade(_migration_config("control", control_url), "head")
        command.upgrade(_migration_config("tenant", tenant_url), "head")

        control_engine = create_async_engine(control_url)
        tenant_engine = create_async_engine(tenant_url)
        engines.extend((control_engine, tenant_engine))
        control_sessions = async_sessionmaker(control_engine, expire_on_commit=False)
        tenant_sessions = async_sessionmaker(tenant_engine, expire_on_commit=False)

        async with control_sessions.begin() as session:
            session.add_all(
                (
                    Team(id=TEAM_ID, name="Repository Team", state="active"),
                    Team(id=OTHER_TEAM_ID, name="Other Team", state="active"),
                    TenantQuota(
                        id=UUID("60000000-0000-4000-8000-000000000001"),
                        team_id=TEAM_ID,
                        active_device_limit=2,
                        queued_device_limit=1,
                        version=1,
                    ),
                    TenantQuota(
                        id=UUID("60000000-0000-4000-8000-000000000002"),
                        team_id=OTHER_TEAM_ID,
                        active_device_limit=2,
                        queued_device_limit=3,
                        version=1,
                    ),
                )
            )

        tenant_router = DirectTenantRouter(tenant_sessions)
        repository = SQLAlchemyAnalysisRepository(
            control_session_factory=control_sessions,
            tenant_router=tenant_router,  # type: ignore[arg-type]
        )
        yield AnalysisDatabases(
            control_engine=control_engine,
            tenant_engine=tenant_engine,
            control_sessions=control_sessions,
            tenant_sessions=tenant_sessions,
            tenant_router=tenant_router,
            repository=repository,
        )
    finally:
        for engine in reversed(engines):
            await engine.dispose()
        if created_names:
            with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
                for database_name in reversed(created_names):
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(database_name)
                        )
                    )


async def _reserve_creation(
    repository: SQLAlchemyAnalysisRepository,
    *,
    analysis_id: UUID = ANALYSIS_ID,
    idempotency_key: str = "analysis-create-1",
    request_hash: str = REQUEST_HASH,
):
    return await repository.reserve_creation(
        team_id=TEAM_ID,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        candidate_analysis_id=analysis_id,
        now=NOW,
    )


async def _complete_creation(
    database: AnalysisDatabases,
    *,
    analysis_id: UUID = ANALYSIS_ID,
    idempotency_key: str = "analysis-create-1",
    request_hash: str = REQUEST_HASH,
) -> None:
    repository = database.repository
    reservation = await _reserve_creation(
        repository,
        analysis_id=analysis_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    await repository.ensure_tenant_parent(
        team_id=TEAM_ID,
        analysis_id=analysis_id,
        requested_by_user_id=USER_ID,
    )
    await repository.mark_tenant_created(
        team_id=TEAM_ID,
        analysis_id=analysis_id,
        now=NOW,
    )
    await repository.complete_creation(
        team_id=TEAM_ID,
        analysis_id=analysis_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        expected_version=reservation.version,
        now=NOW,
    )


async def _seed_finalized_apk(database: AnalysisDatabases) -> None:
    async with database.tenant_sessions.begin() as session:
        session.add(
            Artifact(
                id=ARTIFACT_ID,
                analysis_id=ANALYSIS_ID,
                upload_id=UPLOAD_ID,
                idempotency_key="initial-apk",
                request_hash="b" * 64,
                artifact_kind="apk",
                mime_type=APK_MIME,
                size_bytes=4,
                sha256_b64=CHECKSUM,
                object_key="raw/analyses/repository-test/initial.apk",
                version_id="immutable-version-1",
                state="finalized",
                finalized_at=NOW,
                expires_at=NOW + timedelta(days=30),
                version=2,
            )
        )


def _apk_metadata() -> InspectedApkMetadata:
    return InspectedApkMetadata(
        package_name="dev.perfpilot.repository",
        version_name="1.2.3",
        version_code=12,
        launch_activity="dev.perfpilot.repository.MainActivity",
        min_sdk=28,
        target_sdk=35,
        supported_abis=("arm64-v8a", "x86_64"),
        has_native_libraries=True,
        manifest_sha256="c" * 64,
    )


async def _claim_apk_inspection(
    database: AnalysisDatabases,
    *,
    analysis_id: UUID = ANALYSIS_ID,
    upload_id: UUID = UPLOAD_ID,
    checksum: str = CHECKSUM,
    now: datetime = NOW,
) -> UUID:
    preparation = await database.repository.require_finalizable(
        team_id=TEAM_ID,
        analysis_id=analysis_id,
        upload_id=upload_id,
        sha256_b64=checksum,
        size=4,
        now=now,
    )
    assert preparation.requirements is None
    assert preparation.inspection_token is not None
    return preparation.inspection_token


async def _persist_metadata_and_stage(
    database: AnalysisDatabases,
) -> tuple[PreparedScenario, ...]:
    await _complete_creation(database)
    await _seed_finalized_apk(database)
    inspection_token = await _claim_apk_inspection(database)
    await database.repository.persist_apk_metadata(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        apk_sha256_b64=CHECKSUM,
        metadata=_apk_metadata(),
        inspection_token=inspection_token,
        now=NOW + timedelta(minutes=1),
    )
    return await database.repository.stage_tenant_scenarios(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        now=NOW + timedelta(minutes=2),
    )


def _recipe_hash(recipe: dict[str, object]) -> str:
    encoded = json.dumps(
        recipe,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.asyncio
async def test_quota_serializes_same_hash_replay_and_rejects_conflicts(
    analysis_databases: AnalysisDatabases,
) -> None:
    repository = analysis_databases.repository

    first, replay = await asyncio.gather(
        _reserve_creation(repository, analysis_id=ANALYSIS_ID),
        _reserve_creation(repository, analysis_id=OTHER_ANALYSIS_ID),
    )

    assert replay == first
    assert first.analysis_id in (ANALYSIS_ID, OTHER_ANALYSIS_ID)
    with pytest.raises(AnalysisIdempotencyConflictError):
        await _reserve_creation(repository, request_hash="d" * 64)
    with pytest.raises(AnalysisQueueLimitError):
        await _reserve_creation(
            repository,
            analysis_id=uuid4(),
            idempotency_key="analysis-create-2",
        )

    async with analysis_databases.control_sessions() as session:
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
    assert job_count == 1
    assert key_count == 1


@pytest.mark.asyncio
async def test_creation_resumes_after_tenant_parent_without_duplicate_rows(
    analysis_databases: AnalysisDatabases,
) -> None:
    repository = analysis_databases.repository
    reservation = await _reserve_creation(repository)
    assert (
        await repository.ensure_tenant_parent(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            requested_by_user_id=USER_ID,
        )
        == "creating"
    )

    replay = await _reserve_creation(repository, analysis_id=OTHER_ANALYSIS_ID)
    assert replay == reservation
    assert (
        await repository.ensure_tenant_parent(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            requested_by_user_id=USER_ID,
        )
        == "creating"
    )
    await repository.mark_tenant_created(team_id=TEAM_ID, analysis_id=ANALYSIS_ID, now=NOW)
    await repository.complete_creation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="analysis-create-1",
        request_hash=REQUEST_HASH,
        expected_version=replay.version,
        now=NOW,
    )

    completed = await _reserve_creation(repository, analysis_id=OTHER_ANALYSIS_ID)
    assert completed.state == "created"
    await repository.mark_tenant_created(team_id=TEAM_ID, analysis_id=ANALYSIS_ID, now=NOW)
    await repository.complete_creation(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="analysis-create-1",
        request_hash=REQUEST_HASH,
        expected_version=completed.version,
        now=NOW,
    )

    async with analysis_databases.control_sessions() as control_session:
        job = await control_session.get(GlobalJob, ANALYSIS_ID)
        key = await control_session.scalar(
            select(IdempotencyKey).where(IdempotencyKey.response_resource_id == ANALYSIS_ID)
        )
    async with analysis_databases.tenant_sessions() as tenant_session:
        tenant_parent = await tenant_session.get(Analysis, ANALYSIS_ID)
        tenant_count = await tenant_session.scalar(select(func.count()).select_from(Analysis))
    assert job is not None and job.state == "created" and job.version == 2
    assert key is not None and key.state == "completed" and key.version == 2
    assert tenant_parent is not None and tenant_parent.state == "created"
    assert tenant_count == 1


@pytest.mark.asyncio
async def test_metadata_creates_version_bound_default_recipes_and_exact_tenant_children(
    analysis_databases: AnalysisDatabases,
) -> None:
    prepared = await _persist_metadata_and_stage(analysis_databases)

    assert tuple(item.scenario_type for item in prepared) == (
        "cold_start",
        "scroll",
        "memory_cycle",
    )
    assert tuple(item.scenario_job_id for item in prepared) == tuple(
        scenario_job_id(ANALYSIS_ID, scenario_type)
        for scenario_type in ("cold_start", "scroll", "memory_cycle")
    )
    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        applications = list((await session.scalars(select(Application))).all())
        versions = list((await session.scalars(select(ApplicationVersion))).all())
        recipes = list((await session.scalars(select(ScenarioRecipe))).all())
        children = list((await session.scalars(select(ScenarioResult))).all())

    assert analysis is not None and analysis.state == "queued"
    assert analysis.application_version_id is not None
    assert len(applications) == len(versions) == 1
    assert versions[0].id == analysis.application_version_id
    assert versions[0].apk_sha256_b64 == CHECKSUM
    assert versions[0].supported_abis == ["arm64-v8a", "x86_64"]
    assert len(recipes) == len(children) == 3
    assert {recipe.application_version_id for recipe in recipes} == {versions[0].id}
    assert {recipe.scenario_type for recipe in recipes} == {
        "cold_start",
        "scroll",
        "memory_cycle",
    }
    assert all(recipe.is_active and recipe.recipe_version == 1 for recipe in recipes)
    assert {(child.id, child.scenario_type, child.state) for child in children} == {
        (item.scenario_job_id, item.scenario_type, "queued") for item in prepared
    }


@pytest.mark.asyncio
async def test_apk_inspection_claim_is_exclusive_and_releasable(
    analysis_databases: AnalysisDatabases,
) -> None:
    repository = analysis_databases.repository
    await _complete_creation(analysis_databases)
    await _seed_finalized_apk(analysis_databases)

    first = await repository.require_finalizable(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        sha256_b64=CHECKSUM,
        size=4,
        now=NOW,
    )
    assert first.requirements is None
    assert first.inspection_token is not None

    with pytest.raises(AnalysisUnavailableError, match="inspection is already in progress"):
        await repository.require_finalizable(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            sha256_b64=CHECKSUM,
            size=4,
            now=NOW + timedelta(seconds=1),
        )

    await repository.release_apk_inspection(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        inspection_token=first.inspection_token,
        now=NOW + timedelta(seconds=2),
    )
    retry = await repository.require_finalizable(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        sha256_b64=CHECKSUM,
        size=4,
        now=NOW + timedelta(seconds=3),
    )
    assert retry.inspection_token is not None
    assert retry.inspection_token != first.inspection_token

    takeover = await repository.require_finalizable(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        sha256_b64=CHECKSUM,
        size=4,
        now=NOW + timedelta(minutes=6),
    )
    assert takeover.inspection_token is not None
    assert takeover.inspection_token != retry.inspection_token

    with pytest.raises(StaleTaskVersionError):
        await repository.persist_apk_metadata(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            apk_sha256_b64=CHECKSUM,
            metadata=_apk_metadata(),
            inspection_token=retry.inspection_token,
            now=NOW + timedelta(minutes=6, seconds=1),
        )
    with pytest.raises(StaleTaskVersionError):
        await repository.fail_apk_inspection(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            failure_code="apk_archive_invalid",
            inspection_token=retry.inspection_token,
            now=NOW + timedelta(minutes=6, seconds=2),
        )
    await repository.release_apk_inspection(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        inspection_token=retry.inspection_token,
        now=NOW + timedelta(minutes=6, seconds=3),
    )
    with pytest.raises(AnalysisUnavailableError, match="inspection is already in progress"):
        await repository.require_finalizable(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            sha256_b64=CHECKSUM,
            size=4,
            now=NOW + timedelta(minutes=6, seconds=4),
        )


@pytest.mark.asyncio
async def test_finalization_resumes_from_persisted_metadata_without_reinspection(
    analysis_databases: AnalysisDatabases,
) -> None:
    repository = analysis_databases.repository
    await _complete_creation(analysis_databases)
    await _seed_finalized_apk(analysis_databases)
    inspection_token = await _claim_apk_inspection(analysis_databases)
    await repository.persist_apk_metadata(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        apk_sha256_b64=CHECKSUM,
        metadata=_apk_metadata(),
        inspection_token=inspection_token,
        now=NOW + timedelta(minutes=1),
    )
    await repository.stage_tenant_scenarios(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        now=NOW + timedelta(minutes=2),
    )

    resumed = await repository.require_finalizable(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        sha256_b64=CHECKSUM,
        size=4,
        now=NOW + timedelta(minutes=3),
    )

    assert resumed.requirements == SchedulingRequirements(
        min_api_level=28,
        supported_abis=("arm64-v8a", "x86_64"),
    )


@pytest.mark.asyncio
async def test_each_apk_version_gets_distinct_version_bound_default_recipes(
    analysis_databases: AnalysisDatabases,
) -> None:
    repository = analysis_databases.repository
    await _complete_creation(analysis_databases)
    await _seed_finalized_apk(analysis_databases)
    first_inspection_token = await _claim_apk_inspection(analysis_databases)
    first_version_id = await repository.persist_apk_metadata(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        apk_sha256_b64=CHECKSUM,
        metadata=_apk_metadata(),
        inspection_token=first_inspection_token,
        now=NOW + timedelta(minutes=1),
    )

    async with analysis_databases.control_sessions.begin() as session:
        quota = await session.scalar(select(TenantQuota).where(TenantQuota.team_id == TEAM_ID))
        assert quota is not None
        quota.queued_device_limit = 2

    await _complete_creation(
        analysis_databases,
        analysis_id=OTHER_ANALYSIS_ID,
        idempotency_key="analysis-create-2",
        request_hash="e" * 64,
    )
    other_artifact_id = UUID("40000000-0000-4000-8000-000000000002")
    async with analysis_databases.tenant_sessions.begin() as session:
        session.add(
            Artifact(
                id=other_artifact_id,
                analysis_id=OTHER_ANALYSIS_ID,
                upload_id=UUID("50000000-0000-4000-8000-000000000002"),
                idempotency_key="initial-apk",
                request_hash="f" * 64,
                artifact_kind="apk",
                mime_type=APK_MIME,
                size_bytes=4,
                sha256_b64=OTHER_CHECKSUM,
                object_key="raw/analyses/repository-test/second.apk",
                version_id="immutable-version-2",
                state="finalized",
                finalized_at=NOW,
                expires_at=NOW + timedelta(days=30),
                version=2,
            )
        )
    second_metadata = InspectedApkMetadata(
        package_name="dev.perfpilot.repository",
        version_name="1.2.4",
        version_code=13,
        launch_activity="dev.perfpilot.repository.MainActivity",
        min_sdk=28,
        target_sdk=35,
        supported_abis=("arm64-v8a", "x86_64"),
        has_native_libraries=True,
        manifest_sha256="d" * 64,
    )
    other_upload_id = UUID("50000000-0000-4000-8000-000000000002")
    second_inspection_token = await _claim_apk_inspection(
        analysis_databases,
        analysis_id=OTHER_ANALYSIS_ID,
        upload_id=other_upload_id,
        checksum=OTHER_CHECKSUM,
        now=NOW + timedelta(minutes=1),
    )
    second_version_id = await repository.persist_apk_metadata(
        team_id=TEAM_ID,
        analysis_id=OTHER_ANALYSIS_ID,
        artifact_id=other_artifact_id,
        apk_sha256_b64=OTHER_CHECKSUM,
        metadata=second_metadata,
        inspection_token=second_inspection_token,
        now=NOW + timedelta(minutes=2),
    )

    async with analysis_databases.tenant_sessions() as session:
        recipes = list((await session.scalars(select(ScenarioRecipe))).all())

    assert first_version_id != second_version_id
    first_recipe_ids = {
        recipe.id for recipe in recipes if recipe.application_version_id == first_version_id
    }
    second_recipe_ids = {
        recipe.id for recipe in recipes if recipe.application_version_id == second_version_id
    }
    assert len(first_recipe_ids) == len(second_recipe_ids) == 3
    assert first_recipe_ids.isdisjoint(second_recipe_ids)


@pytest.mark.asyncio
async def test_staging_resume_uses_frozen_children_after_active_recipe_changes(
    analysis_databases: AnalysisDatabases,
) -> None:
    first = await _persist_metadata_and_stage(analysis_databases)
    original_cold = next(item for item in first if item.scenario_type == "cold_start")
    replacement_recipe: dict[str, object] = {
        "schema_version": "1.0",
        "scenario_type": "cold_start",
        "actions": [
            {"action": "launch"},
            {"action": "keyevent", "keycode": "BACK"},
        ],
    }
    replacement_hash = _recipe_hash(replacement_recipe)

    async with analysis_databases.tenant_sessions.begin() as session:
        active = await session.scalar(
            select(ScenarioRecipe).where(
                ScenarioRecipe.scenario_type == "cold_start",
                ScenarioRecipe.is_active.is_(True),
            )
        )
        assert active is not None and active.application_version_id is not None
        active.is_active = False
        session.add(
            ScenarioRecipe(
                id=UUID("70000000-0000-4000-8000-000000000001"),
                application_id=active.application_id,
                application_version_id=active.application_version_id,
                scenario_type="cold_start",
                recipe_version=2,
                recipe_hash=replacement_hash,
                recipe=replacement_recipe,
                is_active=True,
            )
        )

    replay = await analysis_databases.repository.stage_tenant_scenarios(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        now=NOW + timedelta(minutes=3),
    )

    assert replay == first
    replayed_cold = next(item for item in replay if item.scenario_type == "cold_start")
    assert replayed_cold.recipe_version == 1
    assert replayed_cold.recipe_hash == original_cold.recipe_hash
    assert replayed_cold.recipe_hash != replacement_hash


@pytest.mark.asyncio
async def test_control_queue_is_exactly_once_with_three_children_and_requirements(
    analysis_databases: AnalysisDatabases,
) -> None:
    prepared = await _persist_metadata_and_stage(analysis_databases)
    requirements = SchedulingRequirements(
        min_api_level=28,
        supported_abis=("arm64-v8a", "x86_64"),
    )

    for _ in range(2):
        await analysis_databases.repository.queue_control_scenarios(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            scenarios=prepared,
            requirements=requirements,
            now=NOW + timedelta(minutes=3),
        )

    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        children = list(
            (
                await session.scalars(
                    select(ScenarioJob).where(ScenarioJob.analysis_id == ANALYSIS_ID)
                )
            ).all()
        )
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.global_job_id == ANALYSIS_ID,
                        OutboxEvent.event_type == "analysis_queued",
                    )
                )
            ).all()
        )

    assert job is not None and job.state == "queued"
    assert job.input_artifact_id == ARTIFACT_ID
    assert job.min_api_level == 28
    assert job.supported_abis == ["arm64-v8a", "x86_64"]
    assert len(children) == 3
    assert {child.id for child in children} == {item.scenario_job_id for item in prepared}
    assert all(child.input_artifact_id == ARTIFACT_ID for child in children)
    assert all(child.min_api_level == 28 for child in children)
    assert all(child.supported_abis == ["arm64-v8a", "x86_64"] for child in children)
    assert {
        (
            child.scenario_type,
            child.scenario_recipe_id,
            child.recipe_version,
            child.recipe_hash,
        )
        for child in children
    } == {
        (
            item.scenario_type,
            item.scenario_recipe_id,
            item.recipe_version,
            item.recipe_hash,
        )
        for item in prepared
    }
    assert len(events) == 1
    assert events[0].subject_type == "analysis"
    assert events[0].subject_id == ANALYSIS_ID
    assert events[0].scenario_job_id is None


@pytest.mark.asyncio
async def test_report_read_rejects_control_tenant_child_drift(
    analysis_databases: AnalysisDatabases,
) -> None:
    prepared = await _persist_metadata_and_stage(analysis_databases)
    await analysis_databases.repository.queue_control_scenarios(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        scenarios=prepared,
        requirements=SchedulingRequirements(
            min_api_level=28,
            supported_abis=("arm64-v8a", "x86_64"),
        ),
        now=NOW + timedelta(minutes=3),
    )

    async with analysis_databases.control_sessions.begin() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        assert job is not None
        job.state = "failed"
        job.failure_code = "scenario_failed"
        job.completed_at = NOW
        job.version += 1
        children = list(
            (
                await session.scalars(
                    select(ScenarioJob).where(ScenarioJob.analysis_id == ANALYSIS_ID)
                )
            ).all()
        )
        assert len(children) == 3
        for child in children:
            child.state = "failed"
            child.failure_code = "trace_invalid"
            child.completed_at = NOW
            child.version += 1

    async with analysis_databases.tenant_sessions.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None
        analysis.state = "failed"
        analysis.failure_code = "scenario_failed"
        analysis.completed_at = NOW
        analysis.version += 1
        scenarios = list(
            (
                await session.scalars(
                    select(ScenarioResult).where(ScenarioResult.analysis_id == ANALYSIS_ID)
                )
            ).all()
        )
        assert len(scenarios) == 3
        for scenario in scenarios:
            scenario.state = "failed"
            scenario.failure_code = (
                "different_failure" if scenario.scenario_type == "scroll" else "trace_invalid"
            )
            scenario.device_group_reason = "device_unavailable"
            scenario.completed_at = NOW
            scenario.version += 1

    with pytest.raises(ReportNotAvailableError, match="not available"):
        await analysis_databases.repository.load_report(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
        )


@pytest.mark.asyncio
async def test_control_ownership_rejects_wrong_team_before_tenant_routing(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)
    route_count = len(analysis_databases.tenant_router.calls)

    with pytest.raises(AnalysisNotFoundError):
        await analysis_databases.repository.load_view(
            team_id=OTHER_TEAM_ID,
            analysis_id=ANALYSIS_ID,
            now=NOW,
        )

    assert len(analysis_databases.tenant_router.calls) == route_count


@pytest.mark.asyncio
async def test_report_versions_allow_same_number_for_siblings_but_not_same_scenario(
    analysis_databases: AnalysisDatabases,
) -> None:
    prepared = await _persist_metadata_and_stage(analysis_databases)
    first_id, second_id = (prepared[0].scenario_job_id, prepared[1].scenario_job_id)

    async with analysis_databases.tenant_sessions.begin() as session:
        session.add_all(
            (
                ReportVersion(
                    id=UUID("80000000-0000-4000-8000-000000000001"),
                    analysis_id=ANALYSIS_ID,
                    scenario_result_id=first_id,
                    report_version=1,
                    state="failed",
                    generated_at=NOW,
                    tool_version="test-tool-1",
                    rule_version="test-rules-1",
                    provenance={},
                    bundle=None,
                    bundle_sha256_b64=None,
                ),
                ReportVersion(
                    id=UUID("80000000-0000-4000-8000-000000000002"),
                    analysis_id=ANALYSIS_ID,
                    scenario_result_id=second_id,
                    report_version=1,
                    state="failed",
                    generated_at=NOW,
                    tool_version="test-tool-1",
                    rule_version="test-rules-1",
                    provenance={},
                    bundle=None,
                    bundle_sha256_b64=None,
                ),
            )
        )

    with pytest.raises(IntegrityError) as duplicate:
        async with analysis_databases.tenant_sessions.begin() as session:
            session.add(
                ReportVersion(
                    id=UUID("80000000-0000-4000-8000-000000000003"),
                    analysis_id=ANALYSIS_ID,
                    scenario_result_id=first_id,
                    report_version=1,
                    state="failed",
                    generated_at=NOW,
                    tool_version="test-tool-1",
                    rule_version="test-rules-1",
                    provenance={},
                    bundle=None,
                    bundle_sha256_b64=None,
                )
            )

    assert "uq_report_versions_scenario_version" in str(duplicate.value.orig)
    async with analysis_databases.tenant_sessions() as session:
        count = await session.scalar(select(func.count()).select_from(ReportVersion))
    assert count == 2


@pytest.mark.asyncio
async def test_apk_failure_is_durable_and_repairs_control_after_cross_database_crash(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)
    await _seed_finalized_apk(analysis_databases)
    repository = analysis_databases.repository
    inspection_token = await _claim_apk_inspection(analysis_databases)

    async with analysis_databases.tenant_sessions.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None
        assert analysis.apk_inspection_token == inspection_token
        analysis.state = "failed"
        analysis.failure_code = "apk_manifest_invalid"
        analysis.completed_at = NOW + timedelta(minutes=1)
        analysis.version += 1

    with pytest.raises(ApkInspectionError) as failure:
        await repository.require_finalizable(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            sha256_b64=CHECKSUM,
            size=4,
            now=NOW + timedelta(minutes=2),
        )
    assert failure.value.code == "apk_manifest_invalid"

    async with analysis_databases.control_sessions() as control_session:
        repaired = await control_session.get(GlobalJob, ANALYSIS_ID)
    async with analysis_databases.tenant_sessions() as tenant_session:
        tenant_analysis = await tenant_session.get(Analysis, ANALYSIS_ID)
    assert repaired is not None and repaired.state == "failed"
    assert repaired.failure_code == "apk_manifest_invalid"
    assert tenant_analysis is not None and tenant_analysis.state == "failed"
    assert tenant_analysis.failure_code == "apk_manifest_invalid"
