from __future__ import annotations

import os
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from time import monotonic, sleep
from typing import Literal, get_args
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

CONTROL_TABLES = {
    "users",
    "teams",
    "memberships",
    "tenant_resources",
    "agents",
    "devices",
    "global_jobs",
    "scenario_jobs",
    "agent_leases",
    "sample_validation_claims",
    "worker_claims",
    "team_engine_workspaces",
    "engine_executions",
    "outbox_events",
    "inbox_events",
    "idempotency_keys",
    "sessions",
    "tenant_quotas",
    "audit_events",
}
TENANT_TABLES = {
    "applications",
    "application_versions",
    "scenario_recipes",
    "analyses",
    "scenario_results",
    "sample_attempts",
    "artifacts",
    "report_versions",
    "metrics",
    "findings",
    "evidence",
    "recommendations",
}

_API_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_ROOT = _API_ROOT / "migrations"
_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REQUIRE_POSTGRES_ENV = "PERFPILOT_REQUIRE_POSTGRES_TESTS"
_MigrationTree = Literal["control", "tenant"]

_VERSIONED_CONTROL_TABLES = {
    "global_jobs",
    "scenario_jobs",
    "agent_leases",
    "sample_validation_claims",
    "worker_claims",
    "team_engine_workspaces",
    "engine_executions",
    "outbox_events",
    "inbox_events",
}
_FORBIDDEN_CONTROL_COLUMNS = {
    "package_name",
    "customer_filename",
    "object_key",
    "database_url",
    "metrics",
    "evidence",
    "sample_content",
    "payload",
    "raw_result",
    "prompt",
    "question",
    "signed_url",
    "path",
    "dsn",
}
_OUTBOX_COLUMNS = {
    "id",
    "team_id",
    "global_job_id",
    "scenario_job_id",
    "event_type",
    "subject_type",
    "subject_id",
    "ready_at",
    "published_at",
    "dead_lettered_at",
    "retry_count",
    "version",
    "created_at",
    "updated_at",
}


@dataclass(frozen=True)
class MigrationDatabases:
    control_url: URL
    tenant_url: URL
    control_engine: Engine
    tenant_engine: Engine

    def url_for(self, tree: _MigrationTree) -> URL:
        return self.control_url if tree == "control" else self.tenant_url

    def engine_for(self, tree: _MigrationTree) -> Engine:
        return self.control_engine if tree == "control" else self.tenant_engine


def _postgres_url() -> URL:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url:
        url = make_url(raw_url)
        if (
            url.drivername != "postgresql+psycopg"
            or not url.username
            or not url.host
            or not url.database
        ):
            pytest.fail(f"{_POSTGRES_URL_ENV} must be a PostgreSQL database URL")
        return url
    if os.getenv(_REQUIRE_POSTGRES_ENV) == "1":
        pytest.fail(f"{_POSTGRES_URL_ENV} is required")
    pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL migration tests")


def _psycopg_conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def migration_databases() -> Iterator[MigrationDatabases]:
    admin_url = _postgres_url()
    suffix = uuid4().hex
    database_names = {
        "control": f"perfpilot_test_control_{suffix}",
        "tenant": f"perfpilot_test_tenant_{suffix}",
    }
    created_databases: list[str] = []
    engines: list[Engine] = []

    try:
        with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
            for database_name in database_names.values():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                        sql.Identifier(database_name)
                    )
                )
                created_databases.append(database_name)

        control_url = admin_url.set(database=database_names["control"])
        tenant_url = admin_url.set(database=database_names["tenant"])
        control_engine = create_engine(control_url, poolclass=NullPool)
        tenant_engine = create_engine(tenant_url, poolclass=NullPool)
        engines.extend((control_engine, tenant_engine))
        yield MigrationDatabases(
            control_url=control_url,
            tenant_url=tenant_url,
            control_engine=control_engine,
            tenant_engine=tenant_engine,
        )
    finally:
        for engine in engines:
            engine.dispose()
        if created_databases:
            with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
                for database_name in reversed(created_databases):
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(database_name)
                        )
                    )


def _alembic_config(tree: _MigrationTree, database_url: URL) -> Config:
    migration_root = (_MIGRATIONS_ROOT / tree).resolve()
    config = Config(str(migration_root / "alembic.ini"))
    config.set_main_option("script_location", str(migration_root))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def _upgrade(tree: _MigrationTree, database_url: URL) -> None:
    command.upgrade(_alembic_config(tree, database_url), "head")


def _downgrade(tree: _MigrationTree, database_url: URL) -> None:
    command.downgrade(_alembic_config(tree, database_url), "base")


def _table_inventory(tree: _MigrationTree) -> set[str]:
    return CONTROL_TABLES if tree == "control" else TENANT_TABLES


def _unique_column_sets(inspector: object, table_name: str) -> set[tuple[str, ...]]:
    unique_constraints = inspector.get_unique_constraints(table_name)  # type: ignore[attr-defined]
    indexes = inspector.get_indexes(table_name)  # type: ignore[attr-defined]
    return {
        tuple(constraint["column_names"])
        for constraint in unique_constraints
        if constraint["column_names"]
    } | {
        tuple(index["column_names"])
        for index in indexes
        if index["unique"] and index["column_names"]
    }


def _normalize_postgresql_predicate(predicate: object) -> str:
    normalized = "".join(str(predicate).casefold().split()).replace("::text", "")
    return normalized.translate(str.maketrans("", "", '()"'))


def _check_string_literals(predicate: object) -> set[str]:
    return set(re.findall(r"'([^']+)'", str(predicate)))


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://url-user:url-secret-marker@127.0.0.1:55439/postgres",
        ("postgresql+psycopg://127.0.0.1:55439/postgres?application_name=url-secret-marker"),
        "postgresql+psycopg://url-user:url-secret-marker@/postgres",
        "postgresql+psycopg://url-user:url-secret-marker@127.0.0.1:55439",
    ],
    ids=["wrong-driver", "missing-username", "missing-host", "missing-database"],
)
def test_postgres_fixture_rejects_unsafe_admin_urls(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_POSTGRES_URL_ENV, database_url)

    with pytest.raises(pytest.fail.Exception) as exc_info:
        _postgres_url()

    assert _POSTGRES_URL_ENV in str(exc_info.value)
    assert "url-secret-marker" not in str(exc_info.value)


def test_numeric_models_use_decimal_python_types() -> None:
    from perfpilot_api.db.control.models import Device
    from perfpilot_api.db.tenant.models import Metric

    assert get_args(Device.__annotations__["temperature_c"])[0] == Decimal | None
    assert get_args(Metric.__annotations__["numeric_value"])[0] == Decimal | None
    assert Device.__table__.c.temperature_c.type.python_type is Decimal
    assert Metric.__table__.c.numeric_value.type.python_type is Decimal


def test_engine_execution_orm_uses_external_run_identifiers() -> None:
    from perfpilot_api.db.control.models import EngineExecution

    column_names = set(EngineExecution.__table__.columns.keys())
    assert {"external_session_id", "external_run_id"} <= column_names
    assert {"session_id", "run_id"}.isdisjoint(column_names)

    execution = EngineExecution(
        analysis_id=uuid4(),
        team_id=uuid4(),
        engine_id="smartperfetto",
        attempt_number=1,
        adapter_version="1.0.0",
        engine_commit_sha="a" * 40,
        engine_image_digest="sha256:" + "b" * 64,
        input_manifest_hash="c" * 64,
        config_hash="d" * 64,
        external_session_id="session-1",
        external_run_id="run-1",
        state="pending",
    )
    assert execution.external_session_id == "session-1"
    assert execution.external_run_id == "run-1"


@pytest.mark.parametrize("tree", ["control", "tenant"])
def test_migration_env_disposes_engine_in_a_finally_block(tree: _MigrationTree) -> None:
    source = (_MIGRATIONS_ROOT / tree / "env.py").read_text()

    assert "try:\n        with connectable.connect()" in source
    assert "finally:\n        connectable.dispose()" in source


def test_control_migration_creates_only_control_tables(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)

    control_inspector = inspect(migration_databases.control_engine)
    assert set(control_inspector.get_table_names()) == CONTROL_TABLES | {"alembic_version"}


def test_tenant_migration_creates_only_tenant_tables(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("tenant", migration_databases.tenant_url)

    tenant_inspector = inspect(migration_databases.tenant_engine)
    assert set(tenant_inspector.get_table_names()) == TENANT_TABLES | {"alembic_version"}


@pytest.mark.parametrize("tree", ["control", "tenant"])
def test_migrations_match_their_orm_metadata(
    tree: _MigrationTree,
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade(tree, migration_databases.url_for(tree))
    models = import_module(f"perfpilot_api.db.{tree}.models")
    metadata = models.ControlBase.metadata if tree == "control" else models.TenantBase.metadata

    assert set(metadata.tables) == _table_inventory(tree)
    with migration_databases.engine_for(tree).connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        assert compare_metadata(context, metadata) == []


@pytest.mark.parametrize("tree", ["control", "tenant"])
def test_all_domain_tables_use_uuid_keys_and_timezone_timestamps(
    tree: _MigrationTree,
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade(tree, migration_databases.url_for(tree))
    schema_inspector = inspect(migration_databases.engine_for(tree))

    for table_name in _table_inventory(tree):
        columns = {column["name"]: column for column in schema_inspector.get_columns(table_name)}
        primary_key = schema_inspector.get_pk_constraint(table_name)
        assert primary_key["constrained_columns"] == ["id"], table_name
        assert columns["id"]["type"].__class__.__name__ == "UUID", table_name
        for timestamp_name in ("created_at", "updated_at"):
            timestamp = columns[timestamp_name]
            assert timestamp["type"].timezone is True, (table_name, timestamp_name)
            assert timestamp["nullable"] is False, (table_name, timestamp_name)


def test_mutable_control_rows_have_positive_optimistic_versions(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    for table_name in _VERSIONED_CONTROL_TABLES:
        columns = {column["name"]: column for column in control_inspector.get_columns(table_name)}
        version = columns["version"]
        assert version["type"].__class__.__name__ in {"INTEGER", "Integer"}
        assert version["nullable"] is False
        assert str(version["default"]).strip("'(): ") == "1"
        checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in control_inspector.get_check_constraints(table_name)
        }
        assert checks[f"ck_{table_name}_version_positive"].replace(" ", "") == "version>0"


def test_control_schema_enforces_required_uniqueness(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    assert ("username",) in _unique_column_sets(control_inspector, "users")
    assert ("team_id", "user_id") in _unique_column_sets(control_inspector, "memberships")
    assert ("team_id", "resource_version") in _unique_column_sets(
        control_inspector, "tenant_resources"
    )
    assert ("team_id", "idempotency_key") in _unique_column_sets(control_inspector, "global_jobs")
    assert ("id", "team_id") in _unique_column_sets(control_inspector, "global_jobs")
    assert ("analysis_id", "scenario_type") in _unique_column_sets(
        control_inspector, "scenario_jobs"
    )
    assert ("serial",) in _unique_column_sets(control_inspector, "devices")
    assert ("consumer_name", "event_id") in _unique_column_sets(control_inspector, "inbox_events")


def test_control_schema_persists_external_engine_authority(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    workspace_columns = {
        column["name"] for column in control_inspector.get_columns("team_engine_workspaces")
    }
    execution_columns = {
        column["name"] for column in control_inspector.get_columns("engine_executions")
    }
    assert workspace_columns == {
        "id",
        "team_id",
        "engine_id",
        "external_workspace_id",
        "state",
        "version",
        "created_at",
        "updated_at",
    }
    assert execution_columns == {
        "id",
        "analysis_id",
        "team_id",
        "engine_id",
        "attempt_number",
        "adapter_version",
        "engine_commit_sha",
        "engine_image_digest",
        "input_manifest_hash",
        "config_hash",
        "external_workspace_id",
        "external_session_id",
        "external_run_id",
        "state",
        "last_event_cursor",
        "stable_error_code",
        "started_at",
        "completed_at",
        "raw_result_artifact_id",
        "normalized_report_version_id",
        "version",
        "created_at",
        "updated_at",
    }
    assert workspace_columns.isdisjoint(_FORBIDDEN_CONTROL_COLUMNS)
    assert execution_columns.isdisjoint(_FORBIDDEN_CONTROL_COLUMNS)
    workspace_column_details = {
        column["name"]: column for column in control_inspector.get_columns("team_engine_workspaces")
    }
    execution_column_details = {
        column["name"]: column for column in control_inspector.get_columns("engine_executions")
    }
    assert all(
        workspace_column_details[column_name]["nullable"] is False
        for column_name in ("team_id", "engine_id", "state", "version")
    )
    assert workspace_column_details["external_workspace_id"]["nullable"] is True
    assert all(
        execution_column_details[column_name]["nullable"] is False
        for column_name in (
            "analysis_id",
            "team_id",
            "engine_id",
            "attempt_number",
            "adapter_version",
            "engine_commit_sha",
            "engine_image_digest",
            "input_manifest_hash",
            "config_hash",
            "state",
            "version",
        )
    )
    assert all(
        execution_column_details[column_name]["nullable"] is True
        for column_name in (
            "external_workspace_id",
            "external_session_id",
            "external_run_id",
            "last_event_cursor",
            "stable_error_code",
            "started_at",
            "completed_at",
            "raw_result_artifact_id",
            "normalized_report_version_id",
        )
    )
    assert all(
        column["type"].__class__.__name__ not in {"JSON", "JSONB"}
        for table_name in ("team_engine_workspaces", "engine_executions")
        for column in control_inspector.get_columns(table_name)
    )


def test_control_schema_enforces_external_engine_constraints_and_indexes(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    assert ("team_id", "engine_id") in _unique_column_sets(
        control_inspector, "team_engine_workspaces"
    )
    assert ("engine_id", "external_workspace_id") in _unique_column_sets(
        control_inspector, "team_engine_workspaces"
    )
    assert ("analysis_id", "engine_id", "attempt_number") in _unique_column_sets(
        control_inspector, "engine_executions"
    )
    engine_foreign_key = next(
        foreign_key
        for foreign_key in control_inspector.get_foreign_keys("engine_executions")
        if foreign_key["name"] == "fk_engine_executions_analysis_team"
    )
    assert engine_foreign_key["constrained_columns"] == ["analysis_id", "team_id"]
    assert engine_foreign_key["referred_table"] == "global_jobs"
    assert engine_foreign_key["referred_columns"] == ["id", "team_id"]
    assert engine_foreign_key["options"]["ondelete"] == "CASCADE"
    workspace_foreign_key = next(
        foreign_key
        for foreign_key in control_inspector.get_foreign_keys("team_engine_workspaces")
        if foreign_key["referred_table"] == "teams"
    )
    assert workspace_foreign_key["options"]["ondelete"] == "CASCADE"
    indexes = {
        index["name"]: index["column_names"]
        for index in control_inspector.get_indexes("engine_executions")
    }
    assert indexes["ix_engine_executions_state_created"] == ["state", "created_at"]
    assert indexes["ix_engine_executions_team_analysis"] == ["team_id", "analysis_id"]
    execution_checks = {
        constraint["name"]: _normalize_postgresql_predicate(constraint["sqltext"])
        for constraint in control_inspector.get_check_constraints("engine_executions")
    }
    assert execution_checks["ck_engine_executions_attempt_positive"] == "attempt_number>0"
    execution_state_check = execution_checks["ck_engine_executions_state"]
    assert "state" in execution_state_check
    assert _check_string_literals(execution_state_check) == {
        "pending",
        "running",
        "awaiting_user",
        "completed",
        "insufficient_data",
        "failed",
        "canceled",
    }
    assert execution_checks["ck_engine_executions_commit_sha"] == (
        "engine_commit_sha~'^[0-9a-f]{40}$'"
    )
    assert execution_checks["ck_engine_executions_image_digest"] == (
        "engine_image_digest~'^sha256:[0-9a-f]{64}$'"
    )
    assert execution_checks["ck_engine_executions_input_manifest_hash"] == (
        "input_manifest_hash~'^[0-9a-f]{64}$'"
    )
    assert execution_checks["ck_engine_executions_config_hash"] == "config_hash~'^[0-9a-f]{64}$'"
    assert execution_checks["ck_engine_executions_version_positive"] == "version>0"
    workspace_checks = {
        constraint["name"]: _normalize_postgresql_predicate(constraint["sqltext"])
        for constraint in control_inspector.get_check_constraints("team_engine_workspaces")
    }
    workspace_state_check = workspace_checks["ck_team_engine_workspaces_state"]
    assert "state" in workspace_state_check
    assert _check_string_literals(workspace_state_check) == {
        "provisioning",
        "active",
        "deleting",
        "deleted",
        "failed",
    }
    assert workspace_checks["ck_team_engine_workspaces_version_positive"] == "version>0"


def test_control_external_engine_downgrade_refuses_nonempty_metadata(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    team_id = UUID("98000000-0000-4000-8000-000000000001")
    analysis_id = UUID("98000000-0000-4000-8000-000000000002")
    with migration_databases.control_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO teams (id, name, state) VALUES (:id, 'Engine Team', 'active')"),
            {"id": team_id},
        )
        connection.execute(
            text(
                "INSERT INTO global_jobs "
                "(id, team_id, idempotency_key, analysis_mode, state, supported_abis) "
                "VALUES (:id, :team_id, 'engine-metadata', 'device', 'queued', ARRAY[]::varchar[])"
            ),
            {"id": analysis_id, "team_id": team_id},
        )
        connection.execute(
            text(
                "INSERT INTO team_engine_workspaces "
                "(team_id, engine_id, external_workspace_id, state) "
                "VALUES (:team_id, 'smartperfetto', 'workspace-1', 'active')"
            ),
            {"team_id": team_id},
        )

    with pytest.raises(RuntimeError, match="engine metadata must be exported"):
        command.downgrade(
            _alembic_config("control", migration_databases.control_url),
            "0003_analysis_orchestration",
        )
    assert "team_engine_workspaces" in inspect(
        migration_databases.control_engine
    ).get_table_names()

    with migration_databases.control_engine.begin() as connection:
        connection.execute(text("DELETE FROM team_engine_workspaces"))
        connection.execute(
            text(
                "INSERT INTO engine_executions "
                "(analysis_id, team_id, engine_id, attempt_number, adapter_version, "
                "engine_commit_sha, engine_image_digest, input_manifest_hash, config_hash, state) "
                "VALUES (:analysis_id, :team_id, 'smartperfetto', 1, '1.0', repeat('a', 40), "
                "'sha256:' || repeat('b', 64), repeat('c', 64), repeat('d', 64), 'pending')"
            ),
            {"analysis_id": analysis_id, "team_id": team_id},
        )

    with pytest.raises(RuntimeError, match="engine metadata must be exported"):
        command.downgrade(
            _alembic_config("control", migration_databases.control_url),
            "0003_analysis_orchestration",
        )
    assert "engine_executions" in inspect(migration_databases.control_engine).get_table_names()


def test_control_external_engine_downgrade_serializes_with_concurrent_writers(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    team_id = UUID("98100000-0000-4000-8000-000000000001")
    with migration_databases.control_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO teams (id, name, state) VALUES (:id, 'Engine Race', 'active')"),
            {"id": team_id},
        )

    writer_connection = migration_databases.control_engine.connect()
    writer_transaction = writer_connection.begin()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        writer_connection.execute(
            text(
                "INSERT INTO team_engine_workspaces "
                "(team_id, engine_id, external_workspace_id, state) "
                "VALUES (:team_id, 'smartperfetto', 'workspace-race', 'active')"
            ),
            {"team_id": team_id},
        )
        downgrade_future = executor.submit(
            command.downgrade,
            _alembic_config("control", migration_databases.control_url),
            "0003_analysis_orchestration",
        )

        lock_wait_query = text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks AS requested_lock "
            "JOIN pg_class AS locked_relation ON locked_relation.oid = requested_lock.relation "
            "WHERE locked_relation.relname IN "
            "('engine_executions', 'team_engine_workspaces') "
            "AND requested_lock.database = "
            "(SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND requested_lock.mode = 'AccessExclusiveLock' "
            "AND requested_lock.granted = false)"
        )
        deadline = monotonic() + 5
        with migration_databases.control_engine.connect() as observer_connection:
            while not observer_connection.scalar(lock_wait_query):
                if monotonic() >= deadline:
                    pytest.fail("downgrade did not request the engine metadata table lock")
                sleep(0.02)

        writer_transaction.commit()
        with pytest.raises(RuntimeError, match="engine metadata must be exported"):
            downgrade_future.result(timeout=5)
    finally:
        if writer_transaction.is_active:
            writer_transaction.rollback()
        writer_connection.close()
        executor.shutdown(wait=True, cancel_futures=True)

    assert {"engine_executions", "team_engine_workspaces"} <= set(
        inspect(migration_databases.control_engine).get_table_names()
    )
    with migration_databases.control_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0005_memory_upload_mode"
        )


def test_control_schema_persists_provisioning_checkpoints_and_fencing(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    tenant_resource_columns = {
        column["name"] for column in control_inspector.get_columns("tenant_resources")
    }
    assert {
        "provisioning_step",
        "requested_owner_user_id",
        "database_owner_role_name",
        "database_migration_role_name",
        "database_migration_secret_ref",
        "database_role_name",
        "credential_version",
        "database_migration_revision",
        "database_ownership_receipt",
        "role_ownership_receipt",
        "bucket_ownership_receipt",
        "last_error_code",
        "retry_count",
        "next_retry_at",
        "worker_lease_owner",
        "worker_lease_expires_at",
        "fencing_token",
        "write_paused",
        "transition_kind",
        "transition_step",
        "pending_resource_version",
        "pending_database_name",
        "pending_database_role_name",
        "pending_database_secret_ref",
        "pending_credential_version",
        "previous_database_name",
        "previous_database_role_name",
        "previous_database_secret_ref",
        "previous_credential_version",
    } <= tenant_resource_columns

    serving_index = next(
        index
        for index in control_inspector.get_indexes("tenant_resources")
        if index["name"] == "uq_tenant_resources_team_serving"
    )
    assert serving_index["unique"] is True
    assert serving_index["column_names"] == ["team_id"]
    serving_where = serving_index["dialect_options"]["postgresql_where"]
    normalized_serving_where = _normalize_postgresql_predicate(serving_where)
    assert "state" in normalized_serving_where
    assert "active" in normalized_serving_where
    assert "migrating" in normalized_serving_where


def test_control_schema_scopes_idempotency_before_a_team_exists(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    idempotency_column_details = {
        column["name"]: column for column in control_inspector.get_columns("idempotency_keys")
    }
    assert {"operation", "scope_type", "scope_id"} <= idempotency_column_details.keys()
    assert idempotency_column_details["team_id"]["nullable"] is True
    assert (
        "operation",
        "scope_type",
        "scope_id",
        "key",
    ) in _unique_column_sets(control_inspector, "idempotency_keys")
    assert ("team_id", "key") not in _unique_column_sets(control_inspector, "idempotency_keys")
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in control_inspector.get_check_constraints("idempotency_keys")
    }
    assert "team_id IS NOT NULL" in checks["ck_idempotency_keys_team_scope_requires_team"]


def test_tenant_schema_enforces_required_uniqueness(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("tenant", migration_databases.tenant_url)
    tenant_inspector = inspect(migration_databases.tenant_engine)

    assert ("analysis_id", "report_version") in _unique_column_sets(
        tenant_inspector, "report_versions"
    )
    assert ("object_key", "version_id") in _unique_column_sets(tenant_inspector, "artifacts")
    assert ("scenario_job_id", "attempt_no") in _unique_column_sets(
        tenant_inspector, "sample_attempts"
    )


def test_tenant_artifact_upload_schema_enforces_immutable_slot_invariants(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("tenant", migration_databases.tenant_url)
    tenant_inspector = inspect(migration_databases.tenant_engine)

    artifact_columns = {
        column["name"]: column for column in tenant_inspector.get_columns("artifacts")
    }
    assert artifact_columns["idempotency_key"]["nullable"] is True
    assert artifact_columns["request_hash"]["nullable"] is True

    artifact_checks = {
        constraint["name"]: _normalize_postgresql_predicate(constraint["sqltext"])
        for constraint in tenant_inspector.get_check_constraints("artifacts")
    }
    assert artifact_checks["ck_artifacts_idempotency_pair"] == (
        "idempotency_keyisnullandrequest_hashisnullor"
        "idempotency_keyisnotnullandrequest_hashisnotnull"
    )
    assert artifact_checks["ck_artifacts_request_hash"] == (
        "request_hashisnullorrequest_hash~'^[0-9a-f]{64}$'"
    )
    assert artifact_checks["ck_artifacts_state_metadata"] == (
        "state<>'pending'orversion_idisnullandfinalized_atisnulland"
        "state<>'finalized'orversion_idisnotnullandfinalized_atisnotnull"
    )

    assert ("object_key",) in _unique_column_sets(tenant_inspector, "artifacts")

    analysis_idempotency_index = next(
        index
        for index in tenant_inspector.get_indexes("artifacts")
        if index["name"] == "uq_artifacts_analysis_idempotency"
    )
    assert analysis_idempotency_index["unique"] is True
    assert analysis_idempotency_index["column_names"] == [
        "analysis_id",
        "idempotency_key",
    ]
    analysis_idempotency_where = analysis_idempotency_index["dialect_options"]["postgresql_where"]
    assert _normalize_postgresql_predicate(analysis_idempotency_where) == (
        "analysis_idisnotnullandidempotency_keyisnotnull"
    )


def test_partial_indexes_enforce_active_leases_and_ready_outbox_dispatch(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    lease_index = next(
        index
        for index in control_inspector.get_indexes("agent_leases")
        if index["name"] == "uq_agent_leases_active_device"
    )
    assert lease_index["unique"] is True
    assert lease_index["column_names"] == ["device_id"]
    lease_where = lease_index["dialect_options"]["postgresql_where"]
    assert _normalize_postgresql_predicate(lease_where) == "state='active'"

    outbox_columns = {
        column["name"]: column for column in control_inspector.get_columns("outbox_events")
    }
    assert outbox_columns["ready_at"]["nullable"] is True
    outbox_index = next(
        index
        for index in control_inspector.get_indexes("outbox_events")
        if index["name"] == "ix_outbox_events_ready_unpublished"
    )
    outbox_where = outbox_index["dialect_options"]["postgresql_where"]
    assert _normalize_postgresql_predicate(outbox_where) == (
        "ready_atisnotnullandpublished_atisnull"
    )


def test_control_orchestration_tables_exclude_tenant_content(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    control_inspector = inspect(migration_databases.control_engine)

    for table_name in {
        "global_jobs",
        "scenario_jobs",
        "agent_leases",
        "sample_validation_claims",
        "worker_claims",
        "outbox_events",
        "inbox_events",
    }:
        columns = {column["name"] for column in control_inspector.get_columns(table_name)}
        assert columns.isdisjoint(_FORBIDDEN_CONTROL_COLUMNS), table_name
        assert all(
            column["type"].__class__.__name__ not in {"JSON", "JSONB"}
            for column in control_inspector.get_columns(table_name)
        ), table_name

    assert {
        column["name"] for column in control_inspector.get_columns("outbox_events")
    } == _OUTBOX_COLUMNS


@pytest.mark.parametrize("tree", ["control", "tenant"])
def test_foreign_keys_stay_within_their_database_and_are_indexed(
    tree: _MigrationTree,
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade(tree, migration_databases.url_for(tree))
    schema_inspector = inspect(migration_databases.engine_for(tree))
    inventory = _table_inventory(tree)

    for table_name in inventory:
        indexed_columns = {
            tuple(index["column_names"])
            for index in schema_inspector.get_indexes(table_name)
            if index["column_names"]
        } | {
            tuple(constraint["column_names"])
            for constraint in schema_inspector.get_unique_constraints(table_name)
            if constraint["column_names"]
        }
        for foreign_key in schema_inspector.get_foreign_keys(table_name):
            assert foreign_key["referred_table"] in inventory, table_name
            foreign_key_columns = tuple(foreign_key["constrained_columns"])
            assert any(
                columns[: len(foreign_key_columns)] == foreign_key_columns
                or (
                    table_name == "engine_executions"
                    and foreign_key_columns == ("analysis_id", "team_id")
                    and columns[: len(foreign_key_columns)] == ("team_id", "analysis_id")
                )
                for columns in indexed_columns
            ), (table_name, foreign_key_columns)


def test_named_state_count_and_xor_checks_exist(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    _upgrade("tenant", migration_databases.tenant_url)
    control_inspector = inspect(migration_databases.control_engine)
    tenant_inspector = inspect(migration_databases.tenant_engine)

    control_checks = {
        constraint["name"]
        for table_name in CONTROL_TABLES
        for constraint in control_inspector.get_check_constraints(table_name)
    }
    tenant_checks = {
        constraint["name"]
        for table_name in TENANT_TABLES
        for constraint in tenant_inspector.get_check_constraints(table_name)
    }
    assert {
        "ck_global_jobs_state",
        "ck_scenario_jobs_state",
        "ck_scenario_jobs_sample_counts",
        "ck_agent_leases_state",
        "ck_worker_claims_exactly_one_subject",
    } <= control_checks
    assert {
        "ck_sample_attempts_state",
        "ck_artifacts_exactly_one_owner",
    } <= tenant_checks


def test_memory_upload_mode_is_present_in_both_databases(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    _upgrade("tenant", migration_databases.tenant_url)

    control_checks = {
        item["name"]: item["sqltext"]
        for item in inspect(migration_databases.control_engine).get_check_constraints(
            "global_jobs"
        )
    }
    tenant_checks = {
        item["name"]: item["sqltext"]
        for item in inspect(migration_databases.tenant_engine).get_check_constraints(
            "analyses"
        )
    }

    expected_modes = {"device", "trace_upload", "memory_upload"}
    assert _check_string_literals(control_checks["ck_global_jobs_analysis_mode"]) == expected_modes
    assert _check_string_literals(tenant_checks["ck_analyses_mode"]) == expected_modes
    tenant_columns = {
        item["name"]: item
        for item in inspect(migration_databases.tenant_engine).get_columns("analyses")
    }
    assert tenant_columns["question"]["nullable"] is True
    assert tenant_columns["question"]["type"].length == 2000


@pytest.mark.parametrize(
    ("tree", "downgrade_revision", "head_revision"),
    [
        ("control", "0004_external_engine_foundation", "0005_memory_upload_mode"),
        ("tenant", "0003_analysis_orchestration", "0004_memory_upload_mode"),
    ],
)
def test_memory_upload_downgrade_refuses_existing_rows(
    tree: _MigrationTree,
    downgrade_revision: str,
    head_revision: str,
    migration_databases: MigrationDatabases,
) -> None:
    database_url = migration_databases.url_for(tree)
    engine = migration_databases.engine_for(tree)
    _upgrade(tree, database_url)

    with engine.begin() as connection:
        if tree == "control":
            team_id = UUID("a1000000-0000-4000-8000-000000000001")
            connection.execute(
                text("INSERT INTO teams (id, name, state) VALUES (:id, 'Memory', 'active')"),
                {"id": team_id},
            )
            connection.execute(
                text(
                    "INSERT INTO global_jobs "
                    "(id, team_id, idempotency_key, analysis_mode, state, supported_abis) "
                    "VALUES (:id, :team_id, 'memory-upload', 'memory_upload', "
                    "'created', ARRAY[]::varchar[])"
                ),
                {"id": UUID("a2000000-0000-4000-8000-000000000001"), "team_id": team_id},
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO analyses (id, analysis_mode, state) "
                    "VALUES (:id, 'memory_upload', 'created')"
                ),
                {"id": UUID("a3000000-0000-4000-8000-000000000001")},
            )

    with pytest.raises(RuntimeError, match="memory upload downgrade preflight failed"):
        command.downgrade(
            _alembic_config(tree, database_url),
            downgrade_revision,
        )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head_revision
    if tree == "tenant":
        assert "question" in {
            column["name"] for column in inspect(engine).get_columns("analyses")
        }


@pytest.mark.parametrize(
    ("tree", "downgrade_revision", "head_revision", "table_name", "constraint_name"),
    [
        (
            "control",
            "0004_external_engine_foundation",
            "0005_memory_upload_mode",
            "global_jobs",
            "ck_global_jobs_analysis_mode",
        ),
        (
            "tenant",
            "0003_analysis_orchestration",
            "0004_memory_upload_mode",
            "analyses",
            "ck_analyses_mode",
        ),
    ],
)
def test_memory_upload_downgrade_serializes_with_concurrent_writers(
    tree: _MigrationTree,
    downgrade_revision: str,
    head_revision: str,
    table_name: str,
    constraint_name: str,
    migration_databases: MigrationDatabases,
) -> None:
    database_url = migration_databases.url_for(tree)
    engine = migration_databases.engine_for(tree)
    _upgrade(tree, database_url)

    team_id = UUID("a4000000-0000-4000-8000-000000000001")
    if tree == "control":
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO teams (id, name, state) VALUES (:id, 'Memory Race', 'active')"),
                {"id": team_id},
            )

    writer_connection = engine.connect()
    writer_transaction = writer_connection.begin()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        if tree == "control":
            writer_connection.execute(
                text(
                    "INSERT INTO global_jobs "
                    "(id, team_id, idempotency_key, analysis_mode, state, supported_abis) "
                    "VALUES (:id, :team_id, 'memory-upload-race', 'memory_upload', "
                    "'created', ARRAY[]::varchar[])"
                ),
                {
                    "id": UUID("a5000000-0000-4000-8000-000000000001"),
                    "team_id": team_id,
                },
            )
        else:
            writer_connection.execute(
                text(
                    "INSERT INTO analyses (id, analysis_mode, state) "
                    "VALUES (:id, 'memory_upload', 'created')"
                ),
                {"id": UUID("a6000000-0000-4000-8000-000000000001")},
            )

        downgrade_future = executor.submit(
            command.downgrade,
            _alembic_config(tree, database_url),
            downgrade_revision,
        )
        lock_wait_query = text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks AS requested_lock "
            "JOIN pg_class AS locked_relation ON locked_relation.oid = requested_lock.relation "
            "WHERE locked_relation.relname = :table_name "
            "AND requested_lock.database = "
            "(SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND requested_lock.mode = 'AccessExclusiveLock' "
            "AND requested_lock.granted = false)"
        )
        deadline = monotonic() + 5
        with engine.connect() as observer_connection:
            while not observer_connection.scalar(lock_wait_query, {"table_name": table_name}):
                if monotonic() >= deadline:
                    pytest.fail("memory upload downgrade did not request its table lock")
                sleep(0.02)

        writer_transaction.commit()
        with pytest.raises(RuntimeError, match="memory upload downgrade preflight failed"):
            downgrade_future.result(timeout=5)
    finally:
        if writer_transaction.is_active:
            writer_transaction.rollback()
        writer_connection.close()
        executor.shutdown(wait=True, cancel_futures=True)

    checks = {
        item["name"]: item["sqltext"]
        for item in inspect(engine).get_check_constraints(table_name)
    }
    assert _check_string_literals(checks[constraint_name]) == {
        "device",
        "trace_upload",
        "memory_upload",
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head_revision
    if tree == "tenant":
        assert "question" in {
            column["name"] for column in inspect(engine).get_columns("analyses")
        }


@pytest.mark.parametrize("tree", ["control", "tenant"])
def test_each_migration_tree_has_one_head(
    tree: _MigrationTree,
    migration_databases: MigrationDatabases,
) -> None:
    migration_config = _alembic_config(tree, migration_databases.url_for(tree))

    assert len(ScriptDirectory.from_config(migration_config).get_heads()) == 1


@pytest.mark.parametrize("tree", ["control", "tenant"])
def test_migration_round_trip_returns_to_base_and_reupgrades(
    tree: _MigrationTree,
    migration_databases: MigrationDatabases,
) -> None:
    database_url = migration_databases.url_for(tree)
    engine = migration_databases.engine_for(tree)
    expected_tables = _table_inventory(tree) | {"alembic_version"}

    _upgrade(tree, database_url)
    assert set(inspect(engine).get_table_names()) == expected_tables

    _downgrade(tree, database_url)
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}

    _upgrade(tree, database_url)
    assert set(inspect(engine).get_table_names()) == expected_tables


def test_control_task7_downgrade_refuses_to_drop_scheduling_data(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    team_id = UUID("91000000-0000-4000-8000-000000000001")
    analysis_id = UUID("92000000-0000-4000-8000-000000000001")
    scenario_id = UUID("93000000-0000-4000-8000-000000000001")
    recipe_id = UUID("94000000-0000-4000-8000-000000000001")
    with migration_databases.control_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO teams (id, name, state) VALUES (:id, 'Task 7', 'active')"),
            {"id": team_id},
        )
        connection.execute(
            text(
                "INSERT INTO global_jobs "
                "(id, team_id, idempotency_key, analysis_mode, state, supported_abis) "
                "VALUES (:id, :team_id, 'task7', 'device', 'queued', ARRAY['arm64-v8a'])"
            ),
            {"id": analysis_id, "team_id": team_id},
        )
        connection.execute(
            text(
                "INSERT INTO scenario_jobs "
                "(id, analysis_id, scenario_type, state, scenario_recipe_id, "
                "recipe_version, recipe_hash, supported_abis) VALUES "
                "(:id, :analysis_id, 'cold_start', 'queued', :recipe_id, 1, "
                "repeat('a', 64), ARRAY['arm64-v8a'])"
            ),
            {"id": scenario_id, "analysis_id": analysis_id, "recipe_id": recipe_id},
        )

    with pytest.raises(RuntimeError, match="must be exported before downgrade"):
        command.downgrade(
            _alembic_config("control", migration_databases.control_url),
            "0002_tenant_provisioning_state",
        )

    assert "supported_abis" in {
        column["name"]
        for column in inspect(migration_databases.control_engine).get_columns("scenario_jobs")
    }


def test_control_task7_downgrade_serializes_with_concurrent_scheduling_writers(
    migration_databases: MigrationDatabases,
) -> None:
    command.upgrade(
        _alembic_config("control", migration_databases.control_url),
        "0003_analysis_orchestration",
    )
    team_id = UUID("91100000-0000-4000-8000-000000000001")
    analysis_id = UUID("92100000-0000-4000-8000-000000000001")
    with migration_databases.control_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO teams (id, name, state) VALUES (:id, 'Task 7 Race', 'active')"),
            {"id": team_id},
        )

    writer_connection = migration_databases.control_engine.connect()
    writer_transaction = writer_connection.begin()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        writer_connection.execute(
            text(
                "INSERT INTO global_jobs "
                "(id, team_id, idempotency_key, analysis_mode, state, supported_abis) "
                "VALUES (:id, :team_id, 'task7-race', 'device', 'queued', "
                "ARRAY['arm64-v8a'])"
            ),
            {"id": analysis_id, "team_id": team_id},
        )
        downgrade_future = executor.submit(
            command.downgrade,
            _alembic_config("control", migration_databases.control_url),
            "0002_tenant_provisioning_state",
        )

        lock_wait_query = text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks AS requested_lock "
            "JOIN pg_class AS locked_relation ON locked_relation.oid = requested_lock.relation "
            "WHERE locked_relation.relname = 'global_jobs' "
            "AND requested_lock.database = "
            "(SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND requested_lock.mode = 'AccessExclusiveLock' "
            "AND requested_lock.granted = false)"
        )
        deadline = monotonic() + 5
        with migration_databases.control_engine.connect() as observer_connection:
            while not observer_connection.scalar(lock_wait_query):
                if monotonic() >= deadline:
                    pytest.fail("downgrade did not request the scheduling table lock")
                sleep(0.02)

        writer_transaction.commit()
        with pytest.raises(RuntimeError, match="must be exported before downgrade"):
            downgrade_future.result(timeout=5)
    finally:
        if writer_transaction.is_active:
            writer_transaction.rollback()
        writer_connection.close()
        executor.shutdown(wait=True, cancel_futures=True)

    assert "supported_abis" in {
        column["name"]
        for column in inspect(migration_databases.control_engine).get_columns("global_jobs")
    }
    with migration_databases.control_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0003_analysis_orchestration"
        )


def test_tenant_task7_downgrade_refuses_to_drop_inspected_apk_metadata(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("tenant", migration_databases.tenant_url)
    application_id = UUID("95000000-0000-4000-8000-000000000001")
    version_id = UUID("96000000-0000-4000-8000-000000000001")
    with migration_databases.tenant_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO applications (id, name, package_name) "
                "VALUES (:id, 'Task 7', 'com.example.task7')"
            ),
            {"id": application_id},
        )
        connection.execute(
            text(
                "INSERT INTO application_versions "
                "(id, application_id, package_name, version_name, version_code, "
                "has_native_libraries, apk_sha256_b64) VALUES "
                "(:id, :application_id, 'com.example.task7', '1', 1, false, repeat('A', 44))"
            ),
            {"id": version_id, "application_id": application_id},
        )

    with pytest.raises(RuntimeError, match="must be exported before downgrade"):
        command.downgrade(
            _alembic_config("tenant", migration_databases.tenant_url),
            "0002_artifact_upload_slots",
        )

    assert "apk_sha256_b64" in {
        column["name"]
        for column in inspect(migration_databases.tenant_engine).get_columns("application_versions")
    }


def test_tenant_task7_downgrade_serializes_with_concurrent_metadata_writers(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("tenant", migration_databases.tenant_url)
    application_id = UUID("95100000-0000-4000-8000-000000000002")
    version_id = UUID("96100000-0000-4000-8000-000000000002")
    with migration_databases.tenant_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO applications (id, name, package_name) "
                "VALUES (:id, 'Task 7 Race', 'com.example.task7.race')"
            ),
            {"id": application_id},
        )

    writer_connection = migration_databases.tenant_engine.connect()
    writer_transaction = writer_connection.begin()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        writer_connection.execute(
            text(
                "INSERT INTO application_versions "
                "(id, application_id, package_name, version_name, version_code, "
                "has_native_libraries, apk_sha256_b64) VALUES "
                "(:id, :application_id, 'com.example.task7.race', '1', 1, false, "
                "repeat('A', 44))"
            ),
            {"id": version_id, "application_id": application_id},
        )
        downgrade_future = executor.submit(
            command.downgrade,
            _alembic_config("tenant", migration_databases.tenant_url),
            "0002_artifact_upload_slots",
        )

        lock_wait_query = text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks AS requested_lock "
            "JOIN pg_class AS locked_relation ON locked_relation.oid = requested_lock.relation "
            "WHERE locked_relation.relname = 'application_versions' "
            "AND requested_lock.database = "
            "(SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND requested_lock.mode = 'AccessExclusiveLock' "
            "AND requested_lock.granted = false)"
        )
        deadline = monotonic() + 5
        with migration_databases.tenant_engine.connect() as observer_connection:
            while not observer_connection.scalar(lock_wait_query):
                if monotonic() >= deadline:
                    pytest.fail("downgrade did not request the tenant metadata table lock")
                sleep(0.02)

        writer_transaction.commit()
        with pytest.raises(RuntimeError, match="must be exported before downgrade"):
            downgrade_future.result(timeout=5)
    finally:
        if writer_transaction.is_active:
            writer_transaction.rollback()
        writer_connection.close()
        executor.shutdown(wait=True, cancel_futures=True)

    assert "apk_sha256_b64" in {
        column["name"]
        for column in inspect(migration_databases.tenant_engine).get_columns("application_versions")
    }
    with migration_databases.tenant_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_memory_upload_mode"
        )


def test_tenant_task7_upgrade_backfills_native_library_metadata_for_existing_analyses(
    migration_databases: MigrationDatabases,
) -> None:
    config = _alembic_config("tenant", migration_databases.tenant_url)
    command.upgrade(config, "0002_artifact_upload_slots")
    application_id = UUID("95100000-0000-4000-8000-000000000001")
    version_id = UUID("96100000-0000-4000-8000-000000000001")
    analysis_id = UUID("97100000-0000-4000-8000-000000000001")
    with migration_databases.tenant_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO applications (id, name) VALUES (:id, 'Legacy app')"),
            {"id": application_id},
        )
        connection.execute(
            text(
                "INSERT INTO application_versions "
                "(id, application_id, package_name, version_name, version_code, "
                "supported_abis) VALUES "
                "(:id, :application_id, 'com.example.legacy', '1', 1, "
                "CAST('[\"arm64-v8a\"]' AS jsonb))"
            ),
            {"id": version_id, "application_id": application_id},
        )
        connection.execute(
            text(
                "INSERT INTO analyses "
                "(id, application_version_id, analysis_mode, state) "
                "VALUES (:id, :version_id, 'device', 'created')"
            ),
            {"id": analysis_id, "version_id": version_id},
        )

    command.upgrade(config, "head")

    columns = {
        column["name"]: column
        for column in inspect(migration_databases.tenant_engine).get_columns("application_versions")
    }
    with migration_databases.tenant_engine.connect() as connection:
        has_native_libraries = connection.scalar(
            text("SELECT has_native_libraries FROM application_versions WHERE id = :id"),
            {"id": version_id},
        )
    assert columns["has_native_libraries"]["nullable"] is False
    assert has_native_libraries is True


def test_tenant_task7_upgrade_rejects_one_legacy_application_with_multiple_packages(
    migration_databases: MigrationDatabases,
) -> None:
    config = _alembic_config("tenant", migration_databases.tenant_url)
    command.upgrade(config, "0002_artifact_upload_slots")
    application_id = UUID("95200000-0000-4000-8000-000000000001")
    with migration_databases.tenant_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO applications (id, name) VALUES (:id, 'Ambiguous app')"),
            {"id": application_id},
        )
        connection.execute(
            text(
                "INSERT INTO application_versions "
                "(id, application_id, package_name, version_name, version_code) VALUES "
                "(:first_id, :application_id, 'com.example.first', '1', 1), "
                "(:second_id, :application_id, 'com.example.second', '2', 2)"
            ),
            {
                "first_id": UUID("96200000-0000-4000-8000-000000000001"),
                "second_id": UUID("96200000-0000-4000-8000-000000000002"),
                "application_id": application_id,
            },
        )

    with pytest.raises(RuntimeError, match="one application owns multiple packages"):
        command.upgrade(config, "head")


def test_control_constraints_accept_valid_rows_and_reject_duplicates(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    first_user_id = UUID("10000000-0000-4000-8000-000000000001")
    second_user_id = UUID("10000000-0000-4000-8000-000000000002")

    with migration_databases.control_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (:id, :username, :password_hash)"
            ),
            {
                "id": first_user_id,
                "username": "migration-user",
                "password_hash": "argon2-test-hash",
            },
        )

    with pytest.raises(IntegrityError), migration_databases.control_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (:id, :username, :password_hash)"
            ),
            {
                "id": second_user_id,
                "username": "migration-user",
                "password_hash": "different-argon2-test-hash",
            },
        )


def test_tenant_constraints_accept_valid_rows_and_reject_duplicate_attempts(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("tenant", migration_databases.tenant_url)
    application_id = UUID("80000000-0000-4000-8000-000000000001")
    application_version_id = UUID("81000000-0000-4000-8000-000000000001")
    analysis_id = UUID("30000000-0000-4000-8000-000000000001")
    scenario_job_id = UUID("82000000-0000-4000-8000-000000000001")
    attempt_id = UUID("60000000-0000-4000-8000-000000000001")

    with migration_databases.tenant_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO applications (id, name) VALUES (:id, :name)"),
            {"id": application_id, "name": "Migration App"},
        )
        connection.execute(
            text(
                "INSERT INTO application_versions "
                "(id, application_id, package_name, version_name, version_code) "
                "VALUES (:id, :application_id, :package_name, :version_name, :version_code)"
            ),
            {
                "id": application_version_id,
                "application_id": application_id,
                "package_name": "com.example.migration",
                "version_name": "1.0",
                "version_code": 1,
            },
        )
        connection.execute(
            text(
                "INSERT INTO analyses (id, application_version_id, analysis_mode, state) "
                "VALUES (:id, :application_version_id, :analysis_mode, :state)"
            ),
            {
                "id": analysis_id,
                "application_version_id": application_version_id,
                "analysis_mode": "device",
                "state": "created",
            },
        )
        connection.execute(
            text(
                "INSERT INTO scenario_results (id, analysis_id, scenario_type, state) "
                "VALUES (:id, :analysis_id, :scenario_type, :state)"
            ),
            {
                "id": scenario_job_id,
                "analysis_id": analysis_id,
                "scenario_type": "cold_start",
                "state": "running",
            },
        )
        connection.execute(
            text(
                "INSERT INTO sample_attempts (id, scenario_job_id, attempt_no, state) "
                "VALUES (:id, :scenario_job_id, :attempt_no, :state)"
            ),
            {
                "id": attempt_id,
                "scenario_job_id": scenario_job_id,
                "attempt_no": 1,
                "state": "finalized",
            },
        )

    with pytest.raises(IntegrityError), migration_databases.tenant_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sample_attempts (id, scenario_job_id, attempt_no, state) "
                "VALUES (:id, :scenario_job_id, :attempt_no, :state)"
            ),
            {
                "id": uuid4(),
                "scenario_job_id": scenario_job_id,
                "attempt_no": 1,
                "state": "finalized",
            },
        )
