from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
    EngineExecution,
    GlobalJob,
    IdempotencyKey,
    OutboxEvent,
    ScenarioJob,
    SynthesisExecution,
    Team,
    TeamEngineWorkspace,
    TenantResource,
    TenantQuota,
    WorkerClaim,
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
    SynthesisRunConfiguration,
    SynthesisRunService,
    canonical_analysis_request_hash,
    canonical_memory_analysis_request_hash,
    canonical_trace_analysis_request_hash,
    scenario_job_id,
    trace_analysis_ready_event_id,
)
from perfpilot_api.reports.writer import (
    AnalysisReportWriteRequest,
    compose_analysis_report,
    report_version_id,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
)
from perfpilot_api.services.trace_executions import SQLAlchemyTraceExecutionRepository
from perfpilot_api.services.uploads import (
    SQLAlchemyUploadRepository,
    TenantBucket,
    UploadDescriptor,
)
from perfpilot_api.workers.trace_orchestrator import (
    SQLAlchemyTraceWorkQueueRepository,
)

TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("10000000-0000-4000-8000-000000000002")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
OTHER_ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000002")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("50000000-0000-4000-8000-000000000001")
MAPPING_ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000002")
MAPPING_UPLOAD_ID = UUID("50000000-0000-4000-8000-000000000002")
APPLICATION_ID = UUID("70000000-0000-4000-8000-000000000001")
APPLICATION_VERSION_ID = UUID("71000000-0000-4000-8000-000000000001")
OTHER_APPLICATION_VERSION_ID = UUID("71000000-0000-4000-8000-000000000002")
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


class MappingTenantRouter:
    def __init__(
        self,
        session_factories: dict[UUID, async_sessionmaker[AsyncSession]],
    ) -> None:
        self._session_factories = session_factories
        self.calls: list[UUID] = []

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        self.calls.append(team_id)
        async with self._session_factories[team_id].begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = 1
            yield session


class FailOnceTenantRouter(DirectTenantRouter):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.fail_next_transaction = True

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        self.calls.append(team_id)
        async with self._session_factory.begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = 1
            yield session
            if self.fail_next_transaction:
                self.fail_next_transaction = False
                raise RuntimeError("injected tenant transaction failure")


@dataclass
class ReservationRaceCoordinator:
    initial_lookup: asyncio.Barrier = field(default_factory=lambda: asyncio.Barrier(2))
    second_lookup: asyncio.Barrier = field(default_factory=lambda: asyncio.Barrier(2))
    enabled: bool = True

    async def synchronize(self, lookup_number: int) -> None:
        if lookup_number == 1:
            await asyncio.wait_for(self.initial_lookup.wait(), timeout=2)
        elif lookup_number == 2:
            try:
                await asyncio.wait_for(self.second_lookup.wait(), timeout=0.25)
            except TimeoutError:
                pass
            self.enabled = False


class CoordinatedControlSession(AsyncSession):
    def __init__(
        self,
        *args: object,
        coordinator: ReservationRaceCoordinator,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._coordinator = coordinator
        self._idempotency_lookups = 0

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await super().scalar(statement, *args, **kwargs)
        if self._coordinator.enabled and "FROM idempotency_keys" in str(statement):
            self._idempotency_lookups += 1
            await self._coordinator.synchronize(self._idempotency_lookups)
        return result


@dataclass
class AnalysisDatabases:
    control_engine: AsyncEngine
    tenant_engine: AsyncEngine
    control_sessions: async_sessionmaker[AsyncSession]
    tenant_sessions: async_sessionmaker[AsyncSession]
    tenant_router: DirectTenantRouter
    repository: SQLAlchemyAnalysisRepository


@dataclass
class TwoTenantAnalysisDatabases:
    base: AnalysisDatabases
    other_tenant_engine: AsyncEngine
    other_tenant_sessions: async_sessionmaker[AsyncSession]
    tenant_router: MappingTenantRouter
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


@pytest.fixture
async def two_tenant_analysis_databases(
    analysis_databases: AnalysisDatabases,
) -> AsyncIterator[TwoTenantAnalysisDatabases]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_analysis_tenant_other_{uuid4().hex}"
    database_created = False
    other_tenant_engine: AsyncEngine | None = None
    try:
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(database_name)
                )
            )
            database_created = True

        other_tenant_url = admin_url.set(database=database_name)
        command.upgrade(_migration_config("tenant", other_tenant_url), "head")
        other_tenant_engine = create_async_engine(other_tenant_url)
        other_tenant_sessions = async_sessionmaker(
            other_tenant_engine,
            expire_on_commit=False,
        )
        tenant_router = MappingTenantRouter(
            {
                TEAM_ID: analysis_databases.tenant_sessions,
                OTHER_TEAM_ID: other_tenant_sessions,
            }
        )
        repository = SQLAlchemyAnalysisRepository(
            control_session_factory=analysis_databases.control_sessions,
            tenant_router=tenant_router,  # type: ignore[arg-type]
        )
        yield TwoTenantAnalysisDatabases(
            base=analysis_databases,
            other_tenant_engine=other_tenant_engine,
            other_tenant_sessions=other_tenant_sessions,
            tenant_router=tenant_router,
            repository=repository,
        )
    finally:
        if other_tenant_engine is not None:
            await other_tenant_engine.dispose()
        if database_created:
            with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
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


async def _seed_memory_application_versions(database: AnalysisDatabases) -> None:
    async with database.tenant_sessions.begin() as session:
        session.add(
            Application(
                id=APPLICATION_ID,
                name="Memory Repository App",
                package_name="dev.perfpilot.memory",
            )
        )
        session.add_all(
            (
                ApplicationVersion(
                    id=APPLICATION_VERSION_ID,
                    application_id=APPLICATION_ID,
                    package_name="dev.perfpilot.memory",
                    version_name="1.0",
                    version_code=1,
                    min_api_level=28,
                    target_api_level=35,
                    launch_activity="dev.perfpilot.memory.MainActivity",
                    supported_abis=["arm64-v8a"],
                    has_native_libraries=False,
                    apk_sha256_b64=CHECKSUM,
                    manifest_sha256="a" * 64,
                ),
                ApplicationVersion(
                    id=OTHER_APPLICATION_VERSION_ID,
                    application_id=APPLICATION_ID,
                    package_name="dev.perfpilot.memory",
                    version_name="2.0",
                    version_code=2,
                    min_api_level=28,
                    target_api_level=35,
                    launch_activity="dev.perfpilot.memory.MainActivity",
                    supported_abis=["arm64-v8a"],
                    has_native_libraries=False,
                    apk_sha256_b64=OTHER_CHECKSUM,
                    manifest_sha256="b" * 64,
                ),
            )
        )


async def _seed_memory_application_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add(
            Application(
                id=APPLICATION_ID,
                name="Other Tenant Memory App",
                package_name="dev.perfpilot.memory",
            )
        )
        session.add(
            ApplicationVersion(
                id=APPLICATION_VERSION_ID,
                application_id=APPLICATION_ID,
                package_name="dev.perfpilot.memory",
                version_name="1.0",
                version_code=1,
                min_api_level=28,
                target_api_level=35,
                launch_activity="dev.perfpilot.memory.MainActivity",
                supported_abis=["arm64-v8a"],
                has_native_libraries=False,
                apk_sha256_b64=CHECKSUM,
                manifest_sha256="a" * 64,
            )
        )


async def _create_memory_analysis(
    database: AnalysisDatabases,
    *,
    analysis_id: UUID = ANALYSIS_ID,
    application_version_id: UUID = APPLICATION_VERSION_ID,
    idempotency_key: str = "memory-analysis-create-1",
    question: str | None = "退出页面后内存没有下降",
):
    request_hash = canonical_memory_analysis_request_hash(
        application_version_id=application_version_id,
        question=question,
    )
    return await database.repository.create_memory_analysis(
        team_id=TEAM_ID,
        requested_by_user_id=USER_ID,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        candidate_analysis_id=analysis_id,
        application_version_id=application_version_id,
        question=question,
        now=NOW,
    )


async def _create_trace_analysis(
    database: AnalysisDatabases,
    *,
    analysis_id: UUID = ANALYSIS_ID,
    idempotency_key: str = "trace-analysis-create-1",
    question: str | None = "为什么滑动卡顿？",
    inputs: tuple[dict[str, object], ...] | None = None,
):
    canonical_inputs = inputs or (
        {
            "kind": "trace",
            "mime": "application/octet-stream",
            "size": 4,
            "sha256_b64": CHECKSUM,
        },
    )
    request_hash = canonical_trace_analysis_request_hash(
        analysis_profile="auto",
        question=question,
        inputs=canonical_inputs,
    )
    return await database.repository.create_trace_analysis(
        team_id=TEAM_ID,
        requested_by_user_id=USER_ID,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        candidate_analysis_id=analysis_id,
        analysis_profile="auto",
        question=question,
        inputs=canonical_inputs,
        now=NOW,
    )


async def _create_device_analysis_graph(
    database: AnalysisDatabases,
    repository: SQLAlchemyAnalysisRepository,
    *,
    idempotency_key: str,
) -> tuple[str, UUID, str]:
    request_hash = canonical_analysis_request_hash(
        scenarios=("cold_start", "scroll", "memory_cycle"),
        apk_mime=APK_MIME,
        apk_size=4,
        apk_sha256_b64=CHECKSUM,
    )
    reservation = await repository.reserve_creation(
        team_id=TEAM_ID,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        candidate_analysis_id=ANALYSIS_ID,
        now=NOW,
    )
    await repository.ensure_tenant_parent(
        team_id=TEAM_ID,
        analysis_id=reservation.analysis_id,
        requested_by_user_id=USER_ID,
    )
    async with database.tenant_sessions.begin() as session:
        session.add(
            Artifact(
                id=ARTIFACT_ID,
                analysis_id=reservation.analysis_id,
                upload_id=UPLOAD_ID,
                idempotency_key="initial-apk",
                request_hash="b" * 64,
                artifact_kind="apk",
                mime_type=APK_MIME,
                size_bytes=4,
                sha256_b64=CHECKSUM,
                object_key="raw/analyses/race/initial.apk",
                state="pending",
                expires_at=NOW + timedelta(minutes=15),
                version=1,
            )
        )
    await repository.mark_tenant_created(
        team_id=TEAM_ID,
        analysis_id=reservation.analysis_id,
        now=NOW,
    )
    await repository.complete_creation(
        team_id=TEAM_ID,
        analysis_id=reservation.analysis_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        expected_version=reservation.version,
        now=NOW,
    )
    return "device", reservation.analysis_id, request_hash


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


def _engine_execution(**overrides: object) -> EngineExecution:
    values: dict[str, object] = {
        "analysis_id": ANALYSIS_ID,
        "team_id": TEAM_ID,
        "engine_id": "smartperfetto",
        "attempt_number": 1,
        "tenant_resource_version": 1,
        "adapter_version": "1.0.0",
        "engine_commit_sha": "a" * 40,
        "engine_image_digest": "sha256:" + "b" * 64,
        "input_manifest_hash": "c" * 64,
        "config_hash": "d" * 64,
        "state": "pending",
    }
    values.update(overrides)
    return EngineExecution(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_engine_execution_rejects_cross_team_analysis_authority(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)

    async with analysis_databases.control_sessions() as session:
        session.add(_engine_execution(team_id=OTHER_TEAM_ID))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_engine_workspace_unique_constraints_are_enforced(
    analysis_databases: AnalysisDatabases,
) -> None:
    async with analysis_databases.control_sessions.begin() as session:
        session.add(
            TeamEngineWorkspace(
                team_id=TEAM_ID,
                engine_id="smartperfetto",
                external_workspace_id="workspace-1",
                state="active",
            )
        )

    async with analysis_databases.control_sessions() as session:
        session.add(
            TeamEngineWorkspace(
                team_id=TEAM_ID,
                engine_id="smartperfetto",
                external_workspace_id="workspace-2",
                state="active",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with analysis_databases.control_sessions() as session:
        session.add(
            TeamEngineWorkspace(
                team_id=OTHER_TEAM_ID,
                engine_id="smartperfetto",
                external_workspace_id="workspace-1",
                state="active",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "unknown"),
        ("engine_commit_sha", "a" * 39),
        ("engine_image_digest", "latest"),
        ("input_manifest_hash", "c" * 63),
        ("config_hash", "d" * 63),
        ("attempt_number", 0),
        ("input_manifest_hash", "C" * 64),
        ("engine_image_digest", "sha256:" + "B" * 64),
    ],
)
async def test_engine_execution_rejects_invalid_authority_metadata(
    analysis_databases: AnalysisDatabases,
    field: str,
    value: str | int,
) -> None:
    await _complete_creation(analysis_databases)

    async with analysis_databases.control_sessions() as session:
        session.add(_engine_execution(**{field: value}))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_engine_execution_attempts_are_unique_per_engine_and_analysis(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)
    async with analysis_databases.control_sessions.begin() as session:
        session.add(_engine_execution())

    async with analysis_databases.control_sessions() as session:
        session.add(_engine_execution())
        with pytest.raises(IntegrityError):
            await session.commit()

    async with analysis_databases.control_sessions.begin() as session:
        session.add(_engine_execution(attempt_number=2))

    async with analysis_databases.control_sessions() as session:
        attempts = await session.scalars(
            select(EngineExecution.attempt_number)
            .where(EngineExecution.analysis_id == ANALYSIS_ID)
            .order_by(EngineExecution.attempt_number)
        )
    assert list(attempts) == [1, 2]


@pytest.mark.asyncio
async def test_engine_metadata_cascades_with_its_control_plane_owner(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)
    workspace_team_id = uuid4()
    async with analysis_databases.control_sessions.begin() as session:
        session.add(Team(id=workspace_team_id, name="Workspace Cascade", state="active"))
        await session.flush()
        session.add_all(
            (
                _engine_execution(),
                TeamEngineWorkspace(
                    team_id=workspace_team_id,
                    engine_id="smartperfetto",
                    external_workspace_id="workspace-cascade",
                    state="active",
                ),
            )
        )

    async with analysis_databases.control_sessions.begin() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        team = await session.get(Team, workspace_team_id)
        assert job is not None and team is not None
        await session.delete(job)
        await session.delete(team)

    async with analysis_databases.control_sessions() as session:
        execution_count = await session.scalar(
            select(func.count()).select_from(EngineExecution)
        )
        workspace_count = await session.scalar(
            select(func.count()).select_from(TeamEngineWorkspace)
        )
    assert execution_count == 0
    assert workspace_count == 0


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
async def test_trace_creation_persists_tenant_manifest_without_device_side_effects(
    analysis_databases: AnalysisDatabases,
) -> None:
    first = await _create_trace_analysis(analysis_databases)
    replay = await _create_trace_analysis(
        analysis_databases,
        analysis_id=OTHER_ANALYSIS_ID,
    )

    assert replay == first
    assert first.analysis_id == ANALYSIS_ID
    assert first.analysis_mode == "trace_upload"
    assert first.analysis_profile == "auto"
    assert first.question == "为什么滑动卡顿？"
    assert first.application_version_id is None
    assert first.apk_upload is None
    assert first.scenarios == ()
    assert [
        (
            item.state,
            item.artifact_kind,
            item.mime,
            item.size,
            item.sha256_b64,
            item.upload_id,
            item.artifact_id,
        )
        for item in first.input_uploads
    ] == [
        (
            "awaiting_upload",
            "trace",
            "application/octet-stream",
            4,
            CHECKSUM,
            None,
            None,
        )
    ]

    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
        scenario_job_count = await session.scalar(select(func.count()).select_from(ScenarioJob))
        execution_count = await session.scalar(select(func.count()).select_from(EngineExecution))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert job is not None
    assert (job.analysis_mode, job.state, job.device_migration_allowed) == (
        "trace_upload",
        "created",
        False,
    )
    assert (job_count, key_count, scenario_job_count, execution_count, outbox_count) == (
        1,
        1,
        0,
        0,
        0,
    )

    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        artifact_count = await session.scalar(select(func.count()).select_from(Artifact))
        scenario_count = await session.scalar(select(func.count()).select_from(ScenarioResult))
    assert analysis is not None
    assert analysis.analysis_profile == "auto"
    assert analysis.input_manifest == [
        {
            "kind": "trace",
            "mime": "application/octet-stream",
            "size": 4,
            "sha256_b64": CHECKSUM,
        }
    ]
    assert analysis.question == "为什么滑动卡顿？"
    assert (artifact_count, scenario_count) == (0, 0)


@pytest.mark.asyncio
async def test_ai_only_rerun_reserves_next_generation_and_replays_same_key(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _create_trace_analysis(analysis_databases)
    source_id = uuid4()
    synthesis_id = uuid4()
    canonical_id = uuid4()
    report_id = report_version_id(synthesis_id)
    examples = Path(__file__).resolve().parents[4] / "contracts" / "v1" / "examples"
    core = json.loads(
        (examples / "normalized-trace-report.valid.json").read_text(encoding="utf-8")
    )
    core["analysis_id"] = str(ANALYSIS_ID)
    core["provenance"]["canonical_artifact_id"] = str(canonical_id)
    core["provenance"]["canonical_sha256_b64"] = CHECKSUM
    synthesis = json.loads(
        (examples / "synthesis-output.valid.json").read_text(encoding="utf-8")
    )
    composed = compose_analysis_report(
        AnalysisReportWriteRequest(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            synthesis_execution_id=synthesis_id,
            tenant_resource_version=1,
            generation=1,
            generated_at=NOW,
            core_document=core,
            synthesis_document=synthesis,
            synthesis_failure_code=None,
            canonical_artifact_id=canonical_id,
            canonical_sha256_b64=CHECKSUM,
            projection_artifact_id=uuid4(),
            projection_sha256_b64=CHECKSUM,
            synthesis_artifact_id=uuid4(),
            synthesis_sha256_b64=CHECKSUM,
            normalizer_version="smartperfetto-normalizer-1",
            prompt_template_version="perfpilot-synthesis-v1",
            prompt_template_sha256_b64=CHECKSUM,
            report_worker_image_digest="sha256:" + "1" * 64,
            provider_protocol="chat-completions-json-schema-v1",
            provider_name="test-provider",
            model="test-model",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            latency_ms=50,
        ),
        report_version=1,
    )
    async with analysis_databases.control_sessions.begin() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        assert job is not None
        job.state = "completed"
        job.started_at = NOW
        job.completed_at = NOW
        job.version += 1
        session.add(
            TenantResource(
                team_id=TEAM_ID,
                resource_version=1,
                state="active",
                provisioning_step="active",
                credential_version=1,
                retry_count=0,
                fencing_token=0,
                write_paused=False,
            )
        )
        session.add(
            EngineExecution(
                id=source_id,
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                engine_id="smartperfetto",
                attempt_number=1,
                tenant_resource_version=1,
                adapter_version="1.0.0",
                engine_commit_sha="a" * 40,
                engine_image_digest="sha256:" + "b" * 64,
                input_manifest_hash="c" * 64,
                config_hash="d" * 64,
                state="completed",
                raw_result_artifact_id=canonical_id,
                normalized_report_version_id=report_id,
                started_at=NOW,
                completed_at=NOW,
                version=3,
            )
        )
        session.add(
            SynthesisExecution(
                id=synthesis_id,
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                source_execution_id=source_id,
                tenant_resource_version=1,
                generation=1,
                state="succeeded",
                request_fingerprint="e" * 64,
                normalizer_version="smartperfetto-normalizer-1",
                report_worker_image_digest="sha256:" + "1" * 64,
                projection_sha256_b64=CHECKSUM,
                provider_protocol="chat-completions-json-schema-v1",
                provider_name="test-provider",
                provider_model="test-model",
                prompt_template_version="perfpilot-synthesis-v1",
                prompt_template_sha256_b64=CHECKSUM,
                attempt_count=1,
                report_generated_at=NOW,
                report_version_id=report_id,
                started_at=NOW,
                completed_at=NOW,
                version=2,
            )
        )
    async with analysis_databases.tenant_sessions.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None
        analysis.state = "completed"
        analysis.started_at = NOW
        analysis.completed_at = NOW
        analysis.version += 1
        session.add(
            Artifact(
                id=canonical_id,
                analysis_id=ANALYSIS_ID,
                upload_id=canonical_id,
                idempotency_key=f"internal:engine_result:{source_id}",
                request_hash="f" * 64,
                artifact_kind="engine_result",
                mime_type="application/json",
                size_bytes=4,
                sha256_b64=CHECKSUM,
                object_key=f"raw/analyses/{ANALYSIS_ID}/engine-result/{canonical_id}.json",
                version_id="canonical-version-1",
                state="finalized",
                finalized_at=NOW,
                expires_at=NOW + timedelta(days=30),
                version=2,
            )
        )
        session.add(
            ReportVersion(
                id=report_id,
                analysis_id=ANALYSIS_ID,
                scenario_result_id=None,
                report_version=1,
                state="complete",
                generated_at=NOW,
                tool_version="perfpilot-report-writer-1",
                rule_version="smartperfetto-normalizer-1",
                source_artifact_id=canonical_id,
                provenance={},
                report=composed.document,
                report_sha256_b64=composed.sha256_b64,
            )
        )

    service = SynthesisRunService(
        control_session_factory=analysis_databases.control_sessions,
        tenant_router=analysis_databases.tenant_router,  # type: ignore[arg-type]
        execution_repository=SQLAlchemySynthesisExecutionRepository(
            analysis_databases.control_sessions,
            clock=lambda: NOW,
        ),
        configuration=SynthesisRunConfiguration(
            normalizer_version="smartperfetto-normalizer-1",
            prompt_template_version="perfpilot-synthesis-v1",
            prompt_template_sha256_b64=CHECKSUM,
            report_worker_image_digest="sha256:" + "1" * 64,
            provider_name="test-provider",
            model="test-model",
            inference_config_hash="a" * 64,
        ),
        clock=lambda: NOW,
    )
    first = await service.create(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="rerun-generation-two",
    )
    replay = await service.create(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="rerun-generation-two",
    )

    assert first == replay
    assert (first.generation, first.state) == (2, "queued")
    view = await analysis_databases.repository.load_view(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW,
    )
    assert view.report_available is True
    assert [(stage.stage, stage.state) for stage in view.stages] == [
        ("input_validation", "completed"),
        ("smartperfetto", "completed"),
        ("perfpilot_ai", "pending"),
        ("report", "completed"),
    ]
    assert await analysis_databases.repository.load_report(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
    ) == composed.document
    async with analysis_databases.control_sessions() as session:
        runs = list(
            (
                await session.scalars(
                    select(SynthesisExecution).where(
                        SynthesisExecution.analysis_id == ANALYSIS_ID
                    )
                )
            ).all()
        )
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.global_job_id == ANALYSIS_ID,
                        OutboxEvent.event_type == "analysis_synthesis_requested",
                    )
                )
            ).all()
        )
    assert sorted(run.generation for run in runs) == [1, 2]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_trace_status_keeps_optional_pending_when_required_trace_is_ready(
    analysis_databases: AnalysisDatabases,
) -> None:
    inputs: tuple[dict[str, object], ...] = (
        {
            "kind": "trace",
            "mime": "application/octet-stream",
            "size": 4,
            "sha256_b64": CHECKSUM,
        },
        {
            "kind": "mapping",
            "mime": "text/plain",
            "size": 4,
            "sha256_b64": OTHER_CHECKSUM,
        },
    )
    await _create_trace_analysis(analysis_databases, inputs=inputs)
    upload_repository = SQLAlchemyUploadRepository(
        tenant_router=analysis_databases.tenant_router,  # type: ignore[arg-type]
    )
    tenant = TenantBucket(team_id=TEAM_ID, bucket="unused", resource_version=1)
    trace = await upload_repository.reserve_slot(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        idempotency_key="input-trace",
        request_hash="d" * 64,
        descriptor=UploadDescriptor("trace", "application/octet-stream", 4, CHECKSUM),
        artifact_id=ARTIFACT_ID,
        upload_id=UPLOAD_ID,
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/trace/{UPLOAD_ID}",
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    mapping = await upload_repository.reserve_slot(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        idempotency_key="input-mapping",
        request_hash="e" * 64,
        descriptor=UploadDescriptor("mapping", "text/plain", 4, OTHER_CHECKSUM),
        artifact_id=MAPPING_ARTIFACT_ID,
        upload_id=MAPPING_UPLOAD_ID,
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/mapping/{MAPPING_UPLOAD_ID}",
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    await analysis_databases.repository.mark_trace_uploading(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW,
    )

    pending = await analysis_databases.repository.load_view(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW,
    )
    assert pending.state == "uploading"
    assert [item.state for item in pending.input_uploads] == ["pending", "pending"]
    assert not await analysis_databases.repository.trace_required_input_ready(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
    )

    finalized = await upload_repository.finalize_upload(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        upload_id=trace.upload_id,
        expected_version=trace.version,
        storage_version_id="trace-version-1",
        finalized_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(days=30),
    )
    assert finalized is not None
    assert await analysis_databases.repository.trace_required_input_ready(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
    )

    ready = await analysis_databases.repository.load_view(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW + timedelta(minutes=1),
    )
    assert [item.state for item in ready.input_uploads] == ["finalized", "pending"]
    assert ready.input_uploads[0].artifact_id == ARTIFACT_ID
    assert ready.input_uploads[1].upload_id == mapping.upload_id

    for _ in range(2):
        await analysis_databases.repository.queue_trace_execution(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            now=NOW + timedelta(minutes=1),
        )

    queued = await analysis_databases.repository.load_view(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW + timedelta(minutes=1),
    )
    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.global_job_id == ANALYSIS_ID,
                        OutboxEvent.event_type == "trace_analysis_ready",
                    )
                )
            ).all()
        )
    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)

    assert queued.state == "analyzing"
    assert [item.state for item in queued.input_uploads] == ["finalized", "pending"]
    assert job is not None and job.state == "analyzing"
    assert analysis is not None and analysis.state == "analyzing"
    assert len(events) == 1
    assert events[0].id == trace_analysis_ready_event_id(ANALYSIS_ID)
    assert events[0].subject_type == "analysis"
    assert events[0].subject_id == ANALYSIS_ID
    assert events[0].scenario_job_id is None

    queue_clock = [NOW + timedelta(minutes=2)]
    claim_ids = iter(
        (
            UUID("80000000-0000-4000-8000-000000000001"),
            UUID("80000000-0000-4000-8000-000000000002"),
            UUID("80000000-0000-4000-8000-000000000003"),
        )
    )
    claim_tokens = iter(("a" * 32, "b" * 32, "c" * 32))
    queue = SQLAlchemyTraceWorkQueueRepository(
        session_factory=analysis_databases.control_sessions,
        lease_seconds=30,
        clock=lambda: queue_clock[0],
        uuid_source=lambda: next(claim_ids),
        token_source=lambda: next(claim_tokens),
    )

    competing_claims = await asyncio.gather(
        queue.claim_next(consumer_id="trace-worker-a"),
        queue.claim_next(consumer_id="trace-worker-b"),
    )
    claimed = [claim for claim in competing_claims if claim is not None]
    assert len(claimed) == 1
    first_claim = claimed[0]
    assert first_claim.analysis_id == ANALYSIS_ID
    assert "a" * 32 not in repr(first_claim)
    assert "b" * 32 not in repr(first_claim)
    assert "c" * 32 not in repr(first_claim)

    queue_clock[0] += timedelta(seconds=31)
    recovered_claim = await queue.claim_next(consumer_id="trace-worker-c")
    assert recovered_claim is not None
    assert recovered_claim.claim_id != first_claim.claim_id
    async with analysis_databases.control_sessions() as session:
        stored_claims = list(
            (
                await session.scalars(
                    select(WorkerClaim)
                    .where(WorkerClaim.global_job_id == ANALYSIS_ID)
                    .order_by(WorkerClaim.created_at, WorkerClaim.id)
                )
            ).all()
        )
        event = await session.get(OutboxEvent, events[0].id)
    assert [claim.state for claim in stored_claims] == ["expired", "active"]
    assert event is not None
    assert event.published_at is None

    await queue.reschedule(recovered_claim, delay_seconds=5)
    async with analysis_databases.control_sessions() as session:
        event = await session.get(OutboxEvent, events[0].id)
    assert event is not None
    assert event.ready_at == queue_clock[0] + timedelta(seconds=5)

    queue_clock[0] += timedelta(seconds=6)
    second_claim = await queue.claim_next(consumer_id="trace-worker-d")
    assert second_claim is not None
    assert second_claim.claim_id != first_claim.claim_id
    await queue.complete(second_claim)

    async with analysis_databases.control_sessions() as session:
        stored_claims = list(
            (
                await session.scalars(
                    select(WorkerClaim)
                    .where(WorkerClaim.global_job_id == ANALYSIS_ID)
                    .order_by(WorkerClaim.created_at, WorkerClaim.id)
                )
            ).all()
        )
        event = await session.get(OutboxEvent, events[0].id)
    assert [claim.state for claim in stored_claims] == [
        "expired",
        "completed",
        "completed",
    ]
    assert event is not None
    assert event.published_at == queue_clock[0]
    assert await queue.claim_next(consumer_id="trace-worker-e") is None


@pytest.mark.asyncio
async def test_trace_execution_repository_loads_immutable_input_and_projects_both_parents(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _create_trace_analysis(analysis_databases)
    upload_repository = SQLAlchemyUploadRepository(
        tenant_router=analysis_databases.tenant_router,  # type: ignore[arg-type]
    )
    tenant = TenantBucket(team_id=TEAM_ID, bucket="unused", resource_version=1)
    trace = await upload_repository.reserve_slot(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        idempotency_key="input-trace",
        request_hash="d" * 64,
        descriptor=UploadDescriptor("trace", "application/octet-stream", 4, CHECKSUM),
        artifact_id=ARTIFACT_ID,
        upload_id=UPLOAD_ID,
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/trace/{UPLOAD_ID}",
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    finalized = await upload_repository.finalize_upload(
        tenant=tenant,
        analysis_id=ANALYSIS_ID,
        upload_id=trace.upload_id,
        expected_version=trace.version,
        storage_version_id="trace-version-1",
        finalized_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(days=30),
    )
    assert finalized is not None
    await analysis_databases.repository.mark_trace_uploading(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        now=NOW,
    )
    repository = SQLAlchemyTraceExecutionRepository(
        control_session_factory=analysis_databases.control_sessions,
        tenant_router=analysis_databases.tenant_router,  # type: ignore[arg-type]
    )

    loaded = await repository.load_analysis(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert loaded.analysis_state == "uploading"
    assert loaded.tenant_resource_version == 1
    assert loaded.latest_execution is None
    assert [(item.artifact_kind, item.state, item.version) for item in loaded.input_artifacts] == [
        ("trace", "finalized", 2)
    ]

    await repository.project_parent(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        target_state="analyzing",
        failure_code=None,
        now=NOW + timedelta(minutes=2),
    )
    await repository.project_parent(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        target_state="completed",
        failure_code=None,
        now=NOW + timedelta(minutes=3),
    )

    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
    assert job is not None and analysis is not None
    assert (job.state, analysis.state) == ("completed", "completed")
    assert job.completed_at == analysis.completed_at == NOW + timedelta(minutes=3)
    assert (job.version, analysis.version) == (5, 4)


@pytest.mark.asyncio
async def test_memory_creation_binds_existing_version_and_replays_without_side_effects(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _seed_memory_application_versions(analysis_databases)
    async with analysis_databases.control_sessions() as session:
        quota = await session.scalar(select(TenantQuota).where(TenantQuota.team_id == TEAM_ID))
        assert quota is not None
        quota_before = (
            quota.active_device_limit,
            quota.queued_device_limit,
            quota.version,
            quota.updated_at,
        )

    first = await _create_memory_analysis(analysis_databases)
    replay = await _create_memory_analysis(
        analysis_databases,
        analysis_id=OTHER_ANALYSIS_ID,
    )

    assert replay == first
    assert first.analysis_id == ANALYSIS_ID
    assert first.analysis_mode == "memory_upload"
    assert first.application_version_id == APPLICATION_VERSION_ID
    assert first.question == "退出页面后内存没有下降"
    assert first.state == "created"
    assert first.apk_upload is None
    assert first.scenarios == ()

    expected_hash = canonical_memory_analysis_request_hash(
        application_version_id=APPLICATION_VERSION_ID,
        question="退出页面后内存没有下降",
    )
    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        key = await session.scalar(
            select(IdempotencyKey).where(IdempotencyKey.response_resource_id == ANALYSIS_ID)
        )
        quota = await session.scalar(select(TenantQuota).where(TenantQuota.team_id == TEAM_ID))
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
        scenario_job_count = await session.scalar(select(func.count()).select_from(ScenarioJob))
        execution_count = await session.scalar(select(func.count()).select_from(EngineExecution))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert job is not None
    assert (job.team_id, job.idempotency_key, job.analysis_mode, job.state) == (
        TEAM_ID,
        "memory-analysis-create-1",
        "memory_upload",
        "created",
    )
    assert job.version == 2
    assert key is not None
    assert (key.team_id, key.request_hash, key.state, key.response_resource_id) == (
        TEAM_ID,
        expected_hash,
        "completed",
        ANALYSIS_ID,
    )
    assert quota is not None
    assert (
        quota.active_device_limit,
        quota.queued_device_limit,
        quota.version,
        quota.updated_at,
    ) == quota_before
    assert (job_count, key_count, scenario_job_count, execution_count, outbox_count) == (
        1,
        1,
        0,
        0,
        0,
    )

    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        scenario_count = await session.scalar(select(func.count()).select_from(ScenarioResult))
        artifact_count = await session.scalar(select(func.count()).select_from(Artifact))
    assert analysis is not None
    assert (
        analysis.application_version_id,
        analysis.requested_by_user_id,
        analysis.analysis_mode,
        analysis.question,
        analysis.state,
    ) == (
        APPLICATION_VERSION_ID,
        USER_ID,
        "memory_upload",
        "退出页面后内存没有下降",
        "created",
    )
    assert (analysis_count, scenario_count, artifact_count) == (1, 0, 0)


@pytest.mark.asyncio
async def test_memory_creation_rejects_version_and_question_idempotency_mismatches(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _seed_memory_application_versions(analysis_databases)
    await _create_memory_analysis(analysis_databases)

    with pytest.raises(AnalysisIdempotencyConflictError):
        await _create_memory_analysis(
            analysis_databases,
            analysis_id=OTHER_ANALYSIS_ID,
            question="different question",
        )
    with pytest.raises(AnalysisIdempotencyConflictError):
        await _create_memory_analysis(
            analysis_databases,
            analysis_id=OTHER_ANALYSIS_ID,
            application_version_id=OTHER_APPLICATION_VERSION_ID,
        )

    async with analysis_databases.control_sessions() as session:
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
    async with analysis_databases.tenant_sessions() as session:
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
    assert (job_count, key_count, analysis_count) == (1, 1, 1)


@pytest.mark.asyncio
async def test_memory_creation_replay_verifies_stored_tenant_question(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _seed_memory_application_versions(analysis_databases)
    await _create_memory_analysis(analysis_databases)
    async with analysis_databases.tenant_sessions.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None
        analysis.question = "tampered question"

    with pytest.raises(AnalysisUnavailableError, match="tenant analysis state is unavailable"):
        await _create_memory_analysis(
            analysis_databases,
            analysis_id=OTHER_ANALYSIS_ID,
        )

    async with analysis_databases.control_sessions() as session:
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
    async with analysis_databases.tenant_sessions() as session:
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
    assert (job_count, key_count, analysis_count) == (1, 1, 1)


@pytest.mark.asyncio
async def test_deleted_memory_analysis_never_returns_private_question_on_load_or_replay(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _seed_memory_application_versions(analysis_databases)
    await _create_memory_analysis(analysis_databases, question="customer-secret-question")
    async with analysis_databases.tenant_sessions.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None and analysis.tombstoned_at is None
        analysis.state = "deleted"

    with pytest.raises(
        AnalysisUnavailableError,
        match="tenant analysis state is unavailable",
    ):
        await _create_memory_analysis(
            analysis_databases,
            analysis_id=OTHER_ANALYSIS_ID,
            question="customer-secret-question",
        )
    with pytest.raises(
        AnalysisUnavailableError,
        match="tenant analysis state is unavailable",
    ):
        await analysis_databases.repository.load_view(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_memory_creation_replays_after_tenant_transaction_rollback(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _seed_memory_application_versions(analysis_databases)
    failing_router = FailOnceTenantRouter(analysis_databases.tenant_sessions)
    repository = SQLAlchemyAnalysisRepository(
        control_session_factory=analysis_databases.control_sessions,
        tenant_router=failing_router,  # type: ignore[arg-type]
    )
    request_hash = canonical_memory_analysis_request_hash(
        application_version_id=APPLICATION_VERSION_ID,
        question="retained objects",
    )

    with pytest.raises(RuntimeError, match="injected tenant transaction failure"):
        await repository.create_memory_analysis(
            team_id=TEAM_ID,
            requested_by_user_id=USER_ID,
            idempotency_key="memory-tenant-rollback",
            request_hash=request_hash,
            candidate_analysis_id=ANALYSIS_ID,
            application_version_id=APPLICATION_VERSION_ID,
            question="retained objects",
            now=NOW,
        )

    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        key = await session.scalar(
            select(IdempotencyKey).where(IdempotencyKey.response_resource_id == ANALYSIS_ID)
        )
    async with analysis_databases.tenant_sessions() as session:
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        version = await session.get(ApplicationVersion, APPLICATION_VERSION_ID)
    assert job is not None and (job.state, job.version) == ("creating", 1)
    assert key is not None and (key.state, key.version) == ("pending", 1)
    assert analysis_count == 0
    assert version is not None

    replay = await repository.create_memory_analysis(
        team_id=TEAM_ID,
        requested_by_user_id=USER_ID,
        idempotency_key="memory-tenant-rollback",
        request_hash=request_hash,
        candidate_analysis_id=OTHER_ANALYSIS_ID,
        application_version_id=APPLICATION_VERSION_ID,
        question="retained objects",
        now=NOW,
    )

    assert replay.analysis_id == ANALYSIS_ID
    assert replay.question == "retained objects"
    async with analysis_databases.control_sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        key = await session.scalar(
            select(IdempotencyKey).where(IdempotencyKey.response_resource_id == ANALYSIS_ID)
        )
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
    assert job is not None and (job.state, job.version) == ("created", 2)
    assert key is not None and (key.state, key.version) == ("completed", 2)
    assert analysis is not None and analysis.state == "created"
    assert (job_count, key_count, analysis_count) == (1, 1, 1)


@pytest.mark.asyncio
async def test_memory_creation_missing_routed_version_leaves_no_partial_rows(
    analysis_databases: AnalysisDatabases,
) -> None:
    missing_version_id = UUID("71000000-0000-4000-8000-000000000099")

    with pytest.raises(AnalysisNotFoundError, match="application version was not found"):
        await _create_memory_analysis(
            analysis_databases,
            application_version_id=missing_version_id,
        )

    async with analysis_databases.control_sessions() as session:
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
        scenario_job_count = await session.scalar(select(func.count()).select_from(ScenarioJob))
    async with analysis_databases.tenant_sessions() as session:
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        artifact_count = await session.scalar(select(func.count()).select_from(Artifact))
        scenario_count = await session.scalar(select(func.count()).select_from(ScenarioResult))
    assert (job_count, key_count, scenario_job_count) == (0, 0, 0)
    assert (analysis_count, artifact_count, scenario_count) == (0, 0, 0)


@pytest.mark.asyncio
async def test_memory_version_lookup_routes_by_authoritative_team_id(
    two_tenant_analysis_databases: TwoTenantAnalysisDatabases,
) -> None:
    databases = two_tenant_analysis_databases
    await _seed_memory_application_version(databases.other_tenant_sessions)
    request_hash = canonical_memory_analysis_request_hash(
        application_version_id=APPLICATION_VERSION_ID,
        question="retained objects",
    )

    with pytest.raises(AnalysisNotFoundError, match="application version was not found"):
        await databases.repository.create_memory_analysis(
            team_id=TEAM_ID,
            requested_by_user_id=USER_ID,
            idempotency_key="routed-memory-analysis",
            request_hash=request_hash,
            candidate_analysis_id=ANALYSIS_ID,
            application_version_id=APPLICATION_VERSION_ID,
            question="retained objects",
            now=NOW,
        )

    assert databases.tenant_router.calls == [TEAM_ID]
    async with databases.base.control_sessions() as session:
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
    async with databases.base.tenant_sessions() as session:
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        version_count = await session.scalar(select(func.count()).select_from(ApplicationVersion))
    async with databases.other_tenant_sessions() as session:
        other_analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        other_version_count = await session.scalar(
            select(func.count()).select_from(ApplicationVersion)
        )
    assert (job_count, key_count, analysis_count, version_count) == (0, 0, 0, 0)
    assert (other_analysis_count, other_version_count) == (0, 1)

    view = await databases.repository.create_memory_analysis(
        team_id=OTHER_TEAM_ID,
        requested_by_user_id=USER_ID,
        idempotency_key="routed-memory-analysis",
        request_hash=request_hash,
        candidate_analysis_id=OTHER_ANALYSIS_ID,
        application_version_id=APPLICATION_VERSION_ID,
        question="retained objects",
        now=NOW,
    )

    assert view.analysis_id == OTHER_ANALYSIS_ID
    assert view.team_id == OTHER_TEAM_ID
    assert databases.tenant_router.calls == [TEAM_ID, OTHER_TEAM_ID, OTHER_TEAM_ID]
    async with databases.base.control_sessions() as session:
        job = await session.get(GlobalJob, OTHER_ANALYSIS_ID)
        key = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.response_resource_id == OTHER_ANALYSIS_ID
            )
        )
    async with databases.other_tenant_sessions() as session:
        analysis = await session.get(Analysis, OTHER_ANALYSIS_ID)
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        version_count = await session.scalar(select(func.count()).select_from(ApplicationVersion))
    assert job is not None and (job.team_id, job.state) == (OTHER_TEAM_ID, "created")
    assert key is not None and (key.team_id, key.state) == (OTHER_TEAM_ID, "completed")
    assert analysis is not None and analysis.application_version_id == APPLICATION_VERSION_ID
    assert (analysis_count, version_count) == (1, 1)


@pytest.mark.asyncio
async def test_device_and_memory_creation_serialize_cross_mode_idempotency(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _seed_memory_application_versions(analysis_databases)
    coordinator = ReservationRaceCoordinator()
    coordinated_sessions = async_sessionmaker(
        analysis_databases.control_engine,
        class_=CoordinatedControlSession,
        expire_on_commit=False,
        coordinator=coordinator,
    )
    repository = SQLAlchemyAnalysisRepository(
        control_session_factory=coordinated_sessions,
        tenant_router=analysis_databases.tenant_router,  # type: ignore[arg-type]
    )
    idempotency_key = "cross-mode-analysis-race"
    memory_hash = canonical_memory_analysis_request_hash(
        application_version_id=APPLICATION_VERSION_ID,
        question="retained objects",
    )

    async def create_memory() -> tuple[str, UUID, str]:
        view = await repository.create_memory_analysis(
            team_id=TEAM_ID,
            requested_by_user_id=USER_ID,
            idempotency_key=idempotency_key,
            request_hash=memory_hash,
            candidate_analysis_id=OTHER_ANALYSIS_ID,
            application_version_id=APPLICATION_VERSION_ID,
            question="retained objects",
            now=NOW,
        )
        return "memory_upload", view.analysis_id, memory_hash

    outcomes = await asyncio.gather(
        _create_device_analysis_graph(
            analysis_databases,
            repository,
            idempotency_key=idempotency_key,
        ),
        create_memory(),
        return_exceptions=True,
    )
    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]

    assert len(successes) == len(failures) == 1
    assert isinstance(failures[0], AnalysisIdempotencyConflictError)
    winner_mode, winner_analysis_id, winner_hash = successes[0]

    async with analysis_databases.control_sessions() as session:
        job = await session.scalar(select(GlobalJob))
        key = await session.scalar(select(IdempotencyKey))
        quota = await session.scalar(select(TenantQuota).where(TenantQuota.team_id == TEAM_ID))
        job_count = await session.scalar(select(func.count()).select_from(GlobalJob))
        key_count = await session.scalar(select(func.count()).select_from(IdempotencyKey))
        scenario_job_count = await session.scalar(select(func.count()).select_from(ScenarioJob))
    async with analysis_databases.tenant_sessions() as session:
        analysis = await session.scalar(select(Analysis))
        analysis_count = await session.scalar(select(func.count()).select_from(Analysis))
        artifact_count = await session.scalar(select(func.count()).select_from(Artifact))
        scenario_count = await session.scalar(select(func.count()).select_from(ScenarioResult))

    assert job is not None and key is not None and analysis is not None and quota is not None
    assert (job.id, job.team_id, job.idempotency_key, job.analysis_mode, job.state) == (
        winner_analysis_id,
        TEAM_ID,
        idempotency_key,
        winner_mode,
        "created",
    )
    assert (key.response_resource_id, key.request_hash, key.state) == (
        winner_analysis_id,
        winner_hash,
        "completed",
    )
    assert (analysis.id, analysis.analysis_mode, analysis.state) == (
        winner_analysis_id,
        winner_mode,
        "created",
    )
    assert quota.version == 1
    assert (job_count, key_count, scenario_job_count, analysis_count, scenario_count) == (
        1,
        1,
        0,
        1,
        0,
    )
    assert artifact_count == (1 if winner_mode == "device" else 0)


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
