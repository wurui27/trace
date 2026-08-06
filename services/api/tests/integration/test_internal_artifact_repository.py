from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import func, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.tenant.models import Analysis, Application, ApplicationVersion, Artifact
from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.services.internal_artifacts import (
    InternalArtifactUnavailableError,
    S3InternalArtifactSink,
    SQLAlchemyInternalArtifactRepository,
    manifest_artifact_id,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
CAPTURE_ID = UUID("40000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("50000000-0000-4000-8000-000000000001")
APPLICATION_ID = UUID("60000000-0000-4000-8000-000000000001")
APPLICATION_VERSION_ID = UUID("70000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)

_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "tenant"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL internal artifact tests")
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


@pytest.fixture
async def tenant_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_internal_artifact_{uuid4().hex}"
    with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name))
        )
    url = admin_url.set(database=database_name)
    engine: AsyncEngine | None = None
    try:
        command.upgrade(_migration_config(url), "head")
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add(
                Application(
                    id=APPLICATION_ID,
                    name="Internal Artifact App",
                    package_name="dev.perfpilot.internal",
                )
            )
            await session.flush()
            session.add(
                ApplicationVersion(
                    id=APPLICATION_VERSION_ID,
                    application_id=APPLICATION_ID,
                    package_name="dev.perfpilot.internal",
                    version_name="1.0",
                    version_code=1,
                    min_api_level=28,
                    target_api_level=35,
                    launch_activity="dev.perfpilot.internal.MainActivity",
                    supported_abis=["arm64-v8a"],
                    has_native_libraries=False,
                    apk_sha256_b64=base64.b64encode(b"a" * 32).decode("ascii"),
                    manifest_sha256="a" * 64,
                )
            )
            await session.flush()
            session.add(
                Analysis(
                    id=ANALYSIS_ID,
                    application_version_id=APPLICATION_VERSION_ID,
                    requested_by_user_id=USER_ID,
                    analysis_mode="memory_upload",
                    question=None,
                    state="created",
                    version=1,
                )
            )
        yield sessions
    finally:
        if engine is not None:
            await engine.dispose()
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


class MutableTenantRouter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resource_version: int,
    ) -> None:
        self._session_factory = session_factory
        self.resource_version = resource_version
        self.session_versions: list[int] = []

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        assert team_id == TEAM_ID
        self.session_versions.append(self.resource_version)
        async with self._session_factory.begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = self.resource_version
            yield session


class FixedBucketResolver:
    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        assert team_id == TEAM_ID
        return TenantBucket(
            team_id=TEAM_ID,
            bucket="private-generation-one-bucket",
            resource_version=1,
        )


class RecordingS3Client:
    def __init__(self, *, payload: bytes, router: MutableTenantRouter) -> None:
        self.payload = payload
        self.router = router
        self.switch_on_put = False
        self.events: list[str] = []
        self.checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")

    def put_object(self, **_: object) -> dict[str, object]:
        self.events.append("put")
        if self.switch_on_put:
            self.router.resource_version = 2
        return {"VersionId": "immutable-version-1", "ChecksumSHA256": self.checksum}

    def head_object(self, **_: object) -> dict[str, object]:
        self.events.append("head")
        return {
            "VersionId": "immutable-version-1",
            "ChecksumSHA256": self.checksum,
            "ContentType": "application/json",
            "ContentLength": len(self.payload),
            "DeleteMarker": False,
        }


def _payload() -> bytes:
    return MemoryCaptureManifest.model_validate(
        {
            "schema_version": "1.0",
            "analysis_id": ANALYSIS_ID,
            "capture_id": CAPTURE_ID,
            "phase": "single",
            "source": "manual_upload",
            "captured_at": None,
            "subject": {"package": "dev.perfpilot.internal", "android_sdk": 35},
            "artifacts": [{"artifact_id": EVIDENCE_ID, "role": "meminfo"}],
        }
    ).canonical_bytes()


def _sink(
    router: MutableTenantRouter,
    client: RecordingS3Client,
) -> S3InternalArtifactSink:
    return S3InternalArtifactSink(
        repository=SQLAlchemyInternalArtifactRepository(tenant_router=router),  # type: ignore[arg-type]
        bucket_resolver=FixedBucketResolver(),
        client=client,
        clock=lambda: NOW,
    )


async def _artifact_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[int, Artifact | None]:
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(Artifact))
        row = await session.get(Artifact, manifest_artifact_id(CAPTURE_ID))
    return int(count or 0), row


@pytest.mark.asyncio
async def test_repository_resource_mismatch_fails_before_insert_or_s3(
    tenant_database: async_sessionmaker[AsyncSession],
) -> None:
    payload = _payload()
    router = MutableTenantRouter(tenant_database, resource_version=2)
    client = RecordingS3Client(payload=payload, router=router)

    with pytest.raises(InternalArtifactUnavailableError) as caught:
        await _sink(router, client).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=manifest_artifact_id(CAPTURE_ID),
            artifact_kind="memory_capture_manifest",
            payload=payload,
        )

    assert client.events == []
    assert router.session_versions == [2]
    assert await _artifact_rows(tenant_database) == (0, None)
    assert "private-generation-one-bucket" not in str(caught.value)
    assert "private-generation-one-bucket" not in repr(caught.value)


@pytest.mark.asyncio
async def test_repository_allows_device_analysis_memory_manifest(
    tenant_database: async_sessionmaker[AsyncSession],
) -> None:
    async with tenant_database.begin() as session:
        await session.execute(
            update(Analysis)
            .where(Analysis.id == ANALYSIS_ID)
            .values(analysis_mode="device")
        )
    payload = _payload()
    router = MutableTenantRouter(tenant_database, resource_version=1)
    client = RecordingS3Client(payload=payload, router=router)

    artifact_id = await _sink(router, client).write_json(
        team_id=TEAM_ID,
        expected_tenant_resource_version=1,
        analysis_id=ANALYSIS_ID,
        artifact_id=manifest_artifact_id(CAPTURE_ID),
        artifact_kind="memory_capture_manifest",
        payload=payload,
    )

    assert artifact_id == manifest_artifact_id(CAPTURE_ID)
    assert client.events == ["put", "head"]


@pytest.mark.asyncio
async def test_finalize_generation_change_never_updates_or_recovers_in_new_generation(
    tenant_database: async_sessionmaker[AsyncSession],
) -> None:
    payload = _payload()
    router = MutableTenantRouter(tenant_database, resource_version=1)
    client = RecordingS3Client(payload=payload, router=router)
    client.switch_on_put = True

    with pytest.raises(InternalArtifactUnavailableError) as caught:
        await _sink(router, client).write_json(
            team_id=TEAM_ID,
            expected_tenant_resource_version=1,
            analysis_id=ANALYSIS_ID,
            artifact_id=manifest_artifact_id(CAPTURE_ID),
            artifact_kind="memory_capture_manifest",
            payload=payload,
        )

    count, row = await _artifact_rows(tenant_database)
    assert client.events == ["put", "head"]
    assert router.session_versions == [1, 2]
    assert count == 1
    assert row is not None
    assert (row.state, row.version, row.version_id) == ("pending", 1, None)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
