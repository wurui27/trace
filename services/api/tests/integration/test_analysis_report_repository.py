from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
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

from perfpilot_api.db.tenant.models import Analysis, Artifact, ReportVersion
from perfpilot_api.reports.writer import (
    AnalysisReportWriteRequest,
    AnalysisReportWriter,
    ReportIntegrityError,
    ReportSourceError,
    report_version_id,
)


TEAM_ID = UUID("11000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
SYNTHESIS_ID = UUID("22000000-0000-4000-8000-000000000001")
CANONICAL_ID = UUID("85000000-0000-4000-8000-000000000001")
PROJECTION_ID = UUID("89000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("88000000-0000-4000-8000-000000000001")
CHECKSUM = base64.b64encode(b"c" * 32).decode("ascii")
PROMPT_CHECKSUM = base64.b64encode(b"p" * 32).decode("ascii")
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "tenant"


def _postgres_url() -> URL:
    raw = os.getenv("PERFPILOT_TEST_POSTGRES_URL")
    if raw is None:
        if os.getenv("PERFPILOT_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("PERFPILOT_TEST_POSTGRES_URL is required")
        pytest.skip("set PERFPILOT_TEST_POSTGRES_URL to run report repository tests")
    url = make_url(raw)
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        pytest.fail("PERFPILOT_TEST_POSTGRES_URL must be a PostgreSQL psycopg URL")
    return url


def _conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _migration_config(url: URL) -> Config:
    config = Config(str(MIGRATIONS / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["sqlalchemy_url"] = url
    return config


class Router:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], version: int) -> None:
        self.sessions = sessions
        self.version = version

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        assert team_id == TEAM_ID
        async with self.sessions.begin() as session:
            session.info["team_id"] = team_id
            session.info["tenant_resource_version"] = self.version
            yield session


@dataclass
class Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    writer: AnalysisReportWriter


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts" / "v1" / "examples" / name).read_text(encoding="utf-8")
    )


def _request() -> AnalysisReportWriteRequest:
    return AnalysisReportWriteRequest(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        synthesis_execution_id=SYNTHESIS_ID,
        tenant_resource_version=7,
        generation=1,
        generated_at=NOW,
        core_document=_load("normalized-trace-report.valid.json"),
        synthesis_document=_load("synthesis-output.valid.json"),
        synthesis_failure_code=None,
        canonical_artifact_id=CANONICAL_ID,
        canonical_sha256_b64=CHECKSUM,
        projection_artifact_id=PROJECTION_ID,
        projection_sha256_b64=CHECKSUM,
        synthesis_artifact_id=CANDIDATE_ID,
        synthesis_sha256_b64=CHECKSUM,
        normalizer_version="smartperfetto-normalizer-1",
        prompt_template_version="1.0.0",
        prompt_template_sha256_b64=PROMPT_CHECKSUM,
        report_worker_image_digest="sha256:" + "1" * 64,
        provider_protocol="chat-completions-json-schema-v1",
        provider_name="approved-provider",
        model="approved-model",
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
        latency_ms=1234,
    )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    admin_url = _postgres_url()
    database_name = f"perfpilot_report_{uuid4().hex}"
    engine: AsyncEngine | None = None
    created = False
    try:
        with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(database_name)
                )
            )
            created = True
        url = admin_url.set(database=database_name)
        command.upgrade(_migration_config(url), "head")
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add(
                Analysis(
                    id=ANALYSIS_ID,
                    application_version_id=None,
                    analysis_mode="trace_upload",
                    analysis_profile="auto",
                    input_manifest=[],
                    state="analyzing",
                    version=1,
                )
            )
            for index, (artifact_id, kind) in enumerate(
                (
                    (CANONICAL_ID, "engine_result"),
                    (PROJECTION_ID, "ai_projection"),
                    (CANDIDATE_ID, "ai_synthesis_result"),
                )
            ):
                session.add(
                    Artifact(
                        id=artifact_id,
                        analysis_id=ANALYSIS_ID,
                        upload_id=UUID(f"99000000-0000-4000-8000-{index + 1:012d}"),
                        artifact_kind=kind,
                        mime_type="application/json",
                        size_bytes=10,
                        sha256_b64=CHECKSUM,
                        object_key=f"reports/source-{index}.json",
                        version_id=f"version-{index}",
                        state="finalized",
                        finalized_at=NOW,
                        expires_at=NOW + timedelta(days=30),
                        version=2,
                    )
                )
            session.add(
                ReportVersion(
                    id=uuid4(),
                    analysis_id=ANALYSIS_ID,
                    scenario_result_id=None,
                    report_version=3,
                    state="partial",
                    generated_at=NOW - timedelta(days=1),
                    tool_version="legacy",
                    rule_version="legacy",
                    source_artifact_id=None,
                    provenance={},
                    bundle=None,
                    bundle_sha256_b64=None,
                    report=None,
                    report_sha256_b64=None,
                    ai_projection_artifact_id=None,
                    ai_synthesis_artifact_id=None,
                )
            )
        router = Router(sessions, 7)
        yield Harness(
            engine=engine,
            sessions=sessions,
            writer=AnalysisReportWriter(tenant_router=router),  # type: ignore[arg-type]
        )
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            with psycopg.connect(_conninfo(admin_url), autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(database_name)
                    )
                )


@pytest.mark.asyncio
async def test_publish_is_concurrent_idempotent_and_preserves_metadata_rows(
    harness: Harness,
) -> None:
    first, replay = await asyncio.gather(
        harness.writer.publish(_request()),
        harness.writer.publish(_request()),
    )

    assert first.id == replay.id == report_version_id(SYNTHESIS_ID)
    assert first.report_version == replay.report_version == 4
    assert first.sha256_b64 == replay.sha256_b64
    async with harness.sessions() as session:
        rows = list(
            await session.scalars(
                select(ReportVersion)
                .where(ReportVersion.analysis_id == ANALYSIS_ID)
                .order_by(ReportVersion.report_version)
            )
        )
        assert len(rows) == 2
        assert rows[0].report_version == 3 and rows[0].report is None
        assert rows[1].source_artifact_id == CANONICAL_ID
        assert rows[1].ai_projection_artifact_id == PROJECTION_ID
        assert rows[1].ai_synthesis_artifact_id == CANDIDATE_ID
        assert rows[1].report_sha256_b64 == first.sha256_b64
        assert await session.scalar(select(func.count()).select_from(ReportVersion)) == 2


@pytest.mark.asyncio
async def test_publish_rejects_different_bytes_for_same_identity_without_overwrite(
    harness: Harness,
) -> None:
    original = await harness.writer.publish(_request())
    changed_output = _load("synthesis-output.valid.json")
    changed_output["executive_summary"] = "A different but otherwise valid summary."

    with pytest.raises(ReportIntegrityError):
        await harness.writer.publish(
            replace(_request(), synthesis_document=changed_output)
        )

    async with harness.sessions() as session:
        row = await session.get(ReportVersion, original.id)
        assert row is not None
        assert row.report_sha256_b64 == original.sha256_b64
        assert row.report["synthesis"]["output"]["executive_summary"] != changed_output[
            "executive_summary"
        ]


@pytest.mark.asyncio
async def test_publish_rejects_changed_tenant_resource_version(harness: Harness) -> None:
    with pytest.raises(ReportSourceError):
        await harness.writer.publish(replace(_request(), tenant_resource_version=8))

    async with harness.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReportVersion)
                .where(ReportVersion.report.is_not(None))
            )
            == 0
        )
