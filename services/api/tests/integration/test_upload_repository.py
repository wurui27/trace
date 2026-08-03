from __future__ import annotations

import asyncio
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.tenant.models import (
    Analysis,
    Application,
    ApplicationVersion,
    Artifact,
)
from perfpilot_api.services.uploads import (
    SQLAlchemyUploadRepository,
    StoredUpload,
    TenantBucket,
    UploadDescriptor,
    UploadIdempotencyConflictError,
    UploadNotFoundError,
    UploadUnavailableError,
)

TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
APPLICATION_ID = UUID("20000000-0000-4000-8000-000000000001")
APPLICATION_VERSION_ID = UUID("30000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("40000000-0000-4000-8000-000000000001")
TRACE_ANALYSIS_ID = UUID("40000000-0000-4000-8000-000000000002")
OTHER_ANALYSIS_ID = UUID("40000000-0000-4000-8000-000000000099")
ARTIFACT_ID_1 = UUID("50000000-0000-4000-8000-000000000001")
ARTIFACT_ID_2 = UUID("50000000-0000-4000-8000-000000000002")
UPLOAD_ID_1 = UUID("60000000-0000-4000-8000-000000000001")
UPLOAD_ID_2 = UUID("60000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)
CHECKSUM = "iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E="
DESCRIPTOR = UploadDescriptor(
    artifact_kind="apk",
    mime="application/vnd.android.package-archive",
    size=4,
    sha256_b64=CHECKSUM,
)
TRACE_DESCRIPTOR = UploadDescriptor(
    artifact_kind="trace",
    mime="application/octet-stream",
    size=4,
    sha256_b64=CHECKSUM,
)
TENANT = TenantBucket(team_id=TEAM_ID, bucket="pp-team-a", resource_version=1)

_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "tenant"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL upload tests")
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
class UploadDatabase:
    url: URL
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


@pytest.fixture
async def upload_database() -> AsyncIterator[UploadDatabase]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_upload_{uuid4().hex}"
    with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name))
        )
    url = admin_url.set(database=database_name)
    command.upgrade(_migration_config(url), "head")
    engine = create_async_engine(url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory.begin() as session:
        session.add(Application(id=APPLICATION_ID, name="Test App", description=None))
        await session.flush()
        session.add(
            ApplicationVersion(
                id=APPLICATION_VERSION_ID,
                application_id=APPLICATION_ID,
                package_name="dev.perfpilot.test",
                version_name="1.0",
                version_code=1,
                min_api_level=28,
                supported_abis=["arm64-v8a"],
                manifest_sha256=None,
            )
        )
        await session.flush()
        session.add(
            Analysis(
                id=ANALYSIS_ID,
                application_version_id=APPLICATION_VERSION_ID,
                requested_by_user_id=None,
                analysis_mode="device",
                state="uploading",
            )
        )
    try:
        yield UploadDatabase(url=url, engine=engine, session_factory=session_factory)
    finally:
        await engine.dispose()
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


class DirectTenantRouter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resource_version: int = 1,
    ) -> None:
        self._session_factory = session_factory
        self._resource_version = resource_version

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        assert team_id == TEAM_ID
        async with self._session_factory.begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = self._resource_version
            yield session


def _repository(
    database: UploadDatabase,
    *,
    resource_version: int = 1,
) -> SQLAlchemyUploadRepository:
    return SQLAlchemyUploadRepository(
        tenant_router=DirectTenantRouter(
            database.session_factory,
            resource_version=resource_version,
        )
    )


async def _reserve(
    repository: SQLAlchemyUploadRepository,
    *,
    request_hash: str = "a" * 64,
    artifact_id: UUID = ARTIFACT_ID_1,
    upload_id: UUID = UPLOAD_ID_1,
    object_key: str = "raw/analyses/a/inputs/apk/upload-1",
    now: datetime = NOW,
) -> StoredUpload:
    return await repository.reserve_slot(
        tenant=TENANT,
        analysis_id=ANALYSIS_ID,
        idempotency_key="apk-upload-1",
        request_hash=request_hash,
        descriptor=DESCRIPTOR,
        artifact_id=artifact_id,
        upload_id=upload_id,
        object_key=object_key,
        now=now,
        expires_at=now + timedelta(minutes=15),
    )


async def _seed_trace_analysis(upload_database: UploadDatabase) -> None:
    async with upload_database.session_factory.begin() as session:
        session.add(
            Analysis(
                id=TRACE_ANALYSIS_ID,
                application_version_id=None,
                requested_by_user_id=None,
                analysis_mode="trace_upload",
                question="为什么滑动卡顿？",
                analysis_profile="auto",
                input_manifest=[
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": 4,
                        "sha256_b64": CHECKSUM,
                    }
                ],
                state="created",
                version=1,
            )
        )


async def _reserve_trace(
    repository: SQLAlchemyUploadRepository,
    *,
    analysis_id: UUID = TRACE_ANALYSIS_ID,
    idempotency_key: str = "input-trace",
    descriptor: UploadDescriptor = TRACE_DESCRIPTOR,
    artifact_id: UUID = ARTIFACT_ID_1,
    upload_id: UUID = UPLOAD_ID_1,
    object_key: str = "raw/analyses/trace/inputs/trace/upload-1",
) -> StoredUpload:
    return await repository.reserve_slot(
        tenant=TENANT,
        analysis_id=analysis_id,
        idempotency_key=idempotency_key,
        request_hash="c" * 64,
        descriptor=descriptor,
        artifact_id=artifact_id,
        upload_id=upload_id,
        object_key=object_key,
        now=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


@pytest.mark.asyncio
async def test_trace_reservation_requires_the_declared_slot_and_moves_tenant_to_uploading(
    upload_database: UploadDatabase,
) -> None:
    await _seed_trace_analysis(upload_database)
    repository = _repository(upload_database)

    created = await _reserve_trace(repository)
    replayed = await _reserve_trace(
        repository,
        artifact_id=ARTIFACT_ID_2,
        upload_id=UPLOAD_ID_2,
        object_key="raw/analyses/trace/inputs/trace/substitution",
    )

    assert replayed == created
    assert replayed.object_key == "raw/analyses/trace/inputs/trace/upload-1"
    async with upload_database.session_factory() as session:
        analysis = await session.get(Analysis, TRACE_ANALYSIS_ID)
        artifact_count = await session.scalar(
            select(func.count()).select_from(Artifact).where(
                Artifact.analysis_id == TRACE_ANALYSIS_ID
            )
        )
    assert analysis is not None
    assert (analysis.state, analysis.version) == ("uploading", 2)
    assert artifact_count == 1


@pytest.mark.parametrize(
    "case",
    [
        "wrong_key",
        "changed_mime",
        "changed_size",
        "changed_checksum",
        "undeclared_kind",
        "cross_tenant_id",
    ],
)
@pytest.mark.asyncio
async def test_trace_reservation_rejects_undeclared_or_cross_tenant_inputs_without_writes(
    upload_database: UploadDatabase,
    case: str,
) -> None:
    await _seed_trace_analysis(upload_database)
    repository = _repository(upload_database)
    analysis_id = OTHER_ANALYSIS_ID if case == "cross_tenant_id" else TRACE_ANALYSIS_ID
    idempotency_key = "trace-copy" if case == "wrong_key" else "input-trace"
    descriptor = TRACE_DESCRIPTOR
    if case == "changed_mime":
        descriptor = UploadDescriptor("trace", "application/zip", 4, CHECKSUM)
    elif case == "changed_size":
        descriptor = UploadDescriptor("trace", "application/octet-stream", 5, CHECKSUM)
    elif case == "changed_checksum":
        descriptor = UploadDescriptor("trace", "application/octet-stream", 4, "E" * 43 + "=")
    elif case == "undeclared_kind":
        descriptor = UploadDescriptor("log", "text/plain", 4, CHECKSUM)

    with pytest.raises(UploadNotFoundError):
        await _reserve_trace(
            repository,
            analysis_id=analysis_id,
            idempotency_key=idempotency_key,
            descriptor=descriptor,
        )

    async with upload_database.session_factory() as session:
        artifact_count = await session.scalar(
            select(func.count()).select_from(Artifact).where(
                Artifact.analysis_id == TRACE_ANALYSIS_ID
            )
        )
        analysis = await session.get(Analysis, TRACE_ANALYSIS_ID)
    assert artifact_count == 0
    assert analysis is not None
    assert (analysis.state, analysis.version) == ("created", 1)


@pytest.mark.asyncio
async def test_repository_replays_live_slot_conflicts_changed_request_and_rotates_expiry(
    upload_database: UploadDatabase,
) -> None:
    repository = _repository(upload_database)

    created = await _reserve(repository)
    replayed = await _reserve(
        repository,
        artifact_id=ARTIFACT_ID_2,
        upload_id=UPLOAD_ID_2,
        object_key="raw/analyses/a/inputs/apk/unused-replay",
    )
    assert replayed == created

    with pytest.raises(UploadIdempotencyConflictError):
        await _reserve(repository, request_hash="b" * 64)

    rotated = await _reserve(
        repository,
        artifact_id=ARTIFACT_ID_2,
        upload_id=UPLOAD_ID_2,
        object_key="raw/analyses/a/inputs/apk/upload-2",
        now=NOW + timedelta(minutes=16),
    )
    assert rotated.artifact_id == created.artifact_id
    assert rotated.upload_id == UPLOAD_ID_2
    assert rotated.object_key != created.object_key
    assert rotated.version == created.version + 1


@pytest.mark.asyncio
async def test_concurrent_reservations_create_one_durable_idempotency_row(
    upload_database: UploadDatabase,
) -> None:
    repository = _repository(upload_database)

    first, second = await asyncio.gather(
        _reserve(repository),
        _reserve(
            repository,
            artifact_id=ARTIFACT_ID_2,
            upload_id=UPLOAD_ID_2,
            object_key="raw/analyses/a/inputs/apk/concurrent-upload-2",
        ),
    )

    assert first == second
    async with upload_database.session_factory() as session:
        durable_rows = await session.scalar(
            select(func.count())
            .select_from(Artifact)
            .where(
                Artifact.analysis_id == ANALYSIS_ID,
                Artifact.idempotency_key == "apk-upload-1",
            )
        )
    assert durable_rows == 1


@pytest.mark.asyncio
async def test_concurrent_finalize_has_one_cas_winner_and_immutable_storage_version(
    upload_database: UploadDatabase,
) -> None:
    repository = _repository(upload_database)
    created = await _reserve(repository)

    first, second = await asyncio.gather(
        repository.finalize_upload(
            tenant=TENANT,
            analysis_id=ANALYSIS_ID,
            upload_id=created.upload_id,
            expected_version=created.version,
            storage_version_id="storage-version-a",
            finalized_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(days=30),
        ),
        repository.finalize_upload(
            tenant=TENANT,
            analysis_id=ANALYSIS_ID,
            upload_id=created.upload_id,
            expected_version=created.version,
            storage_version_id="storage-version-b",
            finalized_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(days=30),
        ),
    )

    winners = [result for result in (first, second) if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert winner.version_id in {"storage-version-a", "storage-version-b"}
    assert winner.version == created.version + 1

    persisted = await repository.load_upload(
        tenant=TENANT,
        analysis_id=ANALYSIS_ID,
        upload_id=created.upload_id,
    )
    assert persisted.state == "finalized"
    assert persisted.version_id == winner.version_id
    assert persisted.version == winner.version

    downloadable = await repository.load_download(
        tenant=TENANT,
        analysis_id=ANALYSIS_ID,
        artifact_id=created.artifact_id,
        now=NOW + timedelta(minutes=2),
    )
    assert downloadable.version_id == winner.version_id

    async with upload_database.session_factory.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None
        analysis.tombstoned_at = NOW + timedelta(minutes=3)

    with pytest.raises(UploadNotFoundError):
        await repository.load_download(
            tenant=TENANT,
            analysis_id=ANALYSIS_ID,
            artifact_id=created.artifact_id,
            now=NOW + timedelta(minutes=4),
        )


@pytest.mark.asyncio
async def test_resource_version_mismatch_fails_closed_without_writing(
    upload_database: UploadDatabase,
) -> None:
    stale_repository = _repository(upload_database, resource_version=2)

    with pytest.raises(UploadUnavailableError) as exc_info:
        await _reserve(stale_repository)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    async with upload_database.session_factory() as session:
        durable_rows = await session.scalar(select(func.count()).select_from(Artifact))
    assert durable_rows == 0


@pytest.mark.parametrize(
    ("preflight_case", "expected_message"),
    [
        ("duplicate_object_key", "artifact object-key uniqueness preflight failed"),
        ("inconsistent_state", "artifact state-metadata preflight failed"),
    ],
)
def test_migration_preflight_rejects_unsafe_existing_artifacts_without_identifiers(
    upload_database: UploadDatabase,
    preflight_case: str,
    expected_message: str,
) -> None:
    config = _migration_config(upload_database.url)
    command.downgrade(config, "0001_tenant_schema")
    secret_object_key = "private/customer/object-key-marker"
    artifact_rows = [
        (
            ARTIFACT_ID_1,
            UPLOAD_ID_1,
            secret_object_key,
            "storage-version-marker" if preflight_case == "inconsistent_state" else None,
        )
    ]
    if preflight_case == "duplicate_object_key":
        artifact_rows.append((ARTIFACT_ID_2, UPLOAD_ID_2, secret_object_key, None))

    with psycopg.connect(_conninfo(upload_database.url)) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO artifacts "
                "(id, analysis_id, upload_id, artifact_kind, mime_type, size_bytes, "
                "sha256_b64, object_key, version_id, state, finalized_at, expires_at) "
                "VALUES (%s, %s, %s, 'apk', 'application/octet-stream', 4, %s, %s, %s, "
                "'pending', NULL, %s)",
                [
                    (
                        artifact_id,
                        ANALYSIS_ID,
                        upload_id,
                        CHECKSUM,
                        object_key,
                        version_id,
                        NOW + timedelta(days=1),
                    )
                    for artifact_id, upload_id, object_key, version_id in artifact_rows
                ],
            )

    with pytest.raises(RuntimeError) as exc_info:
        command.upgrade(config, "head")

    assert str(exc_info.value) == expected_message
    assert secret_object_key not in str(exc_info.value)
    assert "storage-version-marker" not in str(exc_info.value)
