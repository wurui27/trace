from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perfpilot_api.db.tenant.models import Analysis, Application, ApplicationVersion, Artifact
from perfpilot_api.db.tenant.router import (
    TenantClusterEndpoint,
    TenantRoute,
    TenantRouter,
)
from perfpilot_api.engines.android_memory_contracts import (
    MemoryArtifactRef,
    MemoryCaptureManifest,
    MemorySubject,
)
from perfpilot_api.services.internal_artifacts import manifest_artifact_id
from perfpilot_api.services.memory_executions import (
    MemoryExecutionNotFoundError,
    SQLAlchemyMemoryExecutionRepository,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_A = UUID("81000000-0000-4000-8000-000000000001")
TEAM_B = UUID("81000000-0000-4000-8000-000000000002")
RESOURCE_A = UUID("82000000-0000-4000-8000-000000000001")
RESOURCE_B = UUID("82000000-0000-4000-8000-000000000002")
ANALYSIS_A = UUID("83000000-0000-4000-8000-000000000001")
OTHER_ANALYSIS_A = UUID("83000000-0000-4000-8000-000000000002")
ANALYSIS_B = UUID("83000000-0000-4000-8000-000000000003")
CAPTURE_A = UUID("84000000-0000-4000-8000-000000000001")
EVIDENCE_A = UUID("85000000-0000-4000-8000-000000000001")
APPLICATION_ID = UUID("86000000-0000-4000-8000-000000000001")
APPLICATION_VERSION_ID = UUID("87000000-0000-4000-8000-000000000001")
USER_ID = UUID("88000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)

_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "tenant"


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
            pytest.fail(f"{_POSTGRES_URL_ENV} is required")
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run memory execution repository tests")
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


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _manifest() -> MemoryCaptureManifest:
    return MemoryCaptureManifest(
        schema_version="1.0",
        analysis_id=ANALYSIS_A,
        capture_id=CAPTURE_A,
        phase="single",
        source="manual_upload",
        subject=MemorySubject(package="dev.perfpilot.memory"),
        artifacts=(MemoryArtifactRef(artifact_id=EVIDENCE_A, role="meminfo"),),
    )


async def _seed_tenant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    analyses: tuple[UUID, ...],
    include_capture: bool,
) -> None:
    async with sessions.begin() as session:
        session.add(
            Application(
                id=APPLICATION_ID,
                name="Memory Execution App",
                package_name="dev.perfpilot.memory",
            )
        )
        await session.flush()
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
                apk_sha256_b64=_checksum(b"apk"),
                manifest_sha256="a" * 64,
            )
        )
        await session.flush()
        session.add_all(
            Analysis(
                id=analysis_id,
                application_version_id=APPLICATION_VERSION_ID,
                requested_by_user_id=USER_ID,
                analysis_mode="memory_upload",
                question="Where is retained memory?",
                state="created",
                version=1,
            )
            for analysis_id in analyses
        )
        await session.flush()
        if not include_capture:
            return
        payload = _manifest().canonical_bytes()
        manifest_id = manifest_artifact_id(CAPTURE_A)
        session.add_all(
            (
                Artifact(
                    id=manifest_id,
                    analysis_id=ANALYSIS_A,
                    upload_id=manifest_id,
                    idempotency_key=f"internal:memory_capture_manifest:{manifest_id}",
                    request_hash=hashlib.sha256(payload).hexdigest(),
                    artifact_kind="memory_capture_manifest",
                    mime_type="application/json",
                    size_bytes=len(payload),
                    sha256_b64=_checksum(payload),
                    object_key=(
                        f"raw/analyses/{ANALYSIS_A}/internal/memory_capture_manifest/{manifest_id}"
                    ),
                    version_id="manifest-version-a",
                    state="finalized",
                    finalized_at=NOW,
                    expires_at=NOW + timedelta(days=1),
                    deleted_at=None,
                    version=2,
                ),
                Artifact(
                    id=EVIDENCE_A,
                    analysis_id=ANALYSIS_A,
                    upload_id=EVIDENCE_A,
                    idempotency_key="memory-evidence-a",
                    request_hash="b" * 64,
                    artifact_kind="memory_evidence",
                    mime_type="text/plain",
                    size_bytes=len(b"meminfo"),
                    sha256_b64=_checksum(b"meminfo"),
                    object_key=(f"raw/analyses/{ANALYSIS_A}/inputs/memory_evidence/{EVIDENCE_A}"),
                    version_id="evidence-version-a",
                    state="finalized",
                    finalized_at=NOW,
                    expires_at=NOW + timedelta(days=1),
                    deleted_at=None,
                    version=2,
                ),
            )
        )


class _Routes:
    def __init__(self, database_names: dict[UUID, str]) -> None:
        self._database_names = database_names
        self.calls: list[UUID] = []

    async def active_for_team(self, team_id: UUID) -> TenantRoute | None:
        self.calls.append(team_id)
        database_name = self._database_names.get(team_id)
        if database_name is None:
            return None
        return TenantRoute(
            team_id=team_id,
            resource_id=RESOURCE_A if team_id == TEAM_A else RESOURCE_B,
            resource_version=1,
            credential_version=1,
            database_name=database_name,
            database_role_name="ignored_test_role",
            database_secret_ref=f"secret://tenant/{team_id}",
            write_paused=False,
        )


class _Secrets:
    async def get(self, _reference: str, *, context: object) -> bytes:
        assert getattr(context, "team_id", None) in {TEAM_A, TEAM_B}
        return b"ignored-test-password"


class _Buckets:
    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        assert team_id in {TEAM_A, TEAM_B}
        return TenantBucket(
            team_id=team_id,
            bucket="tenant-a-bucket" if team_id == TEAM_A else "tenant-b-bucket",
            resource_version=1,
        )


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None


class _VersionedS3:
    def __init__(self) -> None:
        self.payload = _manifest().canonical_bytes()
        self.calls: list[dict[str, object]] = []

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "VersionId": "manifest-version-a",
            "ChecksumSHA256": _checksum(self.payload),
            "ContentType": "application/json",
            "ContentLength": len(self.payload),
            "DeleteMarker": False,
            "Body": _Body(self.payload),
        }


@dataclass(slots=True)
class _DatabaseFixture:
    router: TenantRouter
    routes: _Routes
    admin_url: URL
    engines: tuple[AsyncEngine, ...]
    database_names: tuple[str, ...]


@pytest.fixture
async def routed_tenant_databases() -> AsyncIterator[_DatabaseFixture]:
    admin_url = _postgres_url()
    suffix = uuid4().hex[:20]
    database_names = (f"pme_a_{suffix}", f"pme_b_{suffix}")
    with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
        for database_name in database_names:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(database_name)
                )
            )
    engines: list[AsyncEngine] = []
    router: TenantRouter | None = None
    try:
        urls = tuple(admin_url.set(database=database_name) for database_name in database_names)
        for url in urls:
            command.upgrade(_migration_config(url), "head")
            engines.append(create_async_engine(url))
        sessions_a = async_sessionmaker(engines[0], expire_on_commit=False)
        sessions_b = async_sessionmaker(engines[1], expire_on_commit=False)
        await _seed_tenant(
            sessions_a,
            analyses=(ANALYSIS_A, OTHER_ANALYSIS_A),
            include_capture=True,
        )
        await _seed_tenant(
            sessions_b,
            analyses=(ANALYSIS_B,),
            include_capture=False,
        )
        routes = _Routes({TEAM_A: database_names[0], TEAM_B: database_names[1]})

        def engine_factory(url: URL, **_kwargs: object) -> AsyncEngine:
            assert url.database in database_names
            return create_async_engine(admin_url.set(database=url.database))

        router = TenantRouter(
            control_resources=routes,
            secret_store=_Secrets(),  # type: ignore[arg-type]
            cluster=TenantClusterEndpoint(
                host=admin_url.host or "127.0.0.1",
                port=admin_url.port or 5432,
                sslmode="disable",
            ),
            engine_factory=engine_factory,  # type: ignore[arg-type]
            pool_size=1,
            max_cached_pools=2,
            max_global_checkouts=2,
        )
        yield _DatabaseFixture(
            router=router,
            routes=routes,
            admin_url=admin_url,
            engines=tuple(engines),
            database_names=database_names,
        )
    finally:
        if router is not None:
            await router.dispose()
        for engine in engines:
            await engine.dispose()
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            for database_name in database_names:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
                )


def _repository(
    database: _DatabaseFixture,
    s3: _VersionedS3,
) -> SQLAlchemyMemoryExecutionRepository:
    return SQLAlchemyMemoryExecutionRepository(
        tenant_router=database.router,
        bucket_resolver=_Buckets(),
        client=s3,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_real_routed_repository_reads_only_the_pinned_tenant_object(
    routed_tenant_databases: _DatabaseFixture,
) -> None:
    s3 = _VersionedS3()
    loaded = await _repository(routed_tenant_databases, s3).load_capture(
        team_id=TEAM_A,
        analysis_id=ANALYSIS_A,
        capture_id=CAPTURE_A,
    )

    assert loaded.analysis_id == ANALYSIS_A
    assert loaded.manifest.capture_id == CAPTURE_A
    assert tuple(item.artifact_id for item in loaded.evidence_artifacts) == (EVIDENCE_A,)
    manifest_id = manifest_artifact_id(CAPTURE_A)
    assert s3.calls == [
        {
            "Bucket": "tenant-a-bucket",
            "Key": (f"raw/analyses/{ANALYSIS_A}/internal/memory_capture_manifest/{manifest_id}"),
            "VersionId": "manifest-version-a",
            "ChecksumMode": "ENABLED",
        }
    ]


@pytest.mark.asyncio
async def test_real_sql_route_rejects_other_tenant_and_analysis_before_object_access(
    routed_tenant_databases: _DatabaseFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = _VersionedS3()
    repository = _repository(routed_tenant_databases, s3)

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("SQL ownership predicate did not reject the manifest row")

    monkeypatch.setattr(repository, "_stored_manifest", forbidden)
    for team_id, analysis_id in (
        (TEAM_B, ANALYSIS_A),
        (TEAM_A, OTHER_ANALYSIS_A),
    ):
        with pytest.raises(MemoryExecutionNotFoundError, match="memory capture"):
            await repository.load_capture(
                team_id=team_id,
                analysis_id=analysis_id,
                capture_id=CAPTURE_A,
            )

    assert s3.calls == []
    assert routed_tenant_databases.routes.calls[-2:] == [TEAM_B, TEAM_A]
