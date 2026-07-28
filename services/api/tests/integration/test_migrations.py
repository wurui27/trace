from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from pathlib import Path
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
    assert ("analysis_id", "scenario_type") in _unique_column_sets(
        control_inspector, "scenario_jobs"
    )
    assert ("serial",) in _unique_column_sets(control_inspector, "devices")
    assert ("consumer_name", "event_id") in _unique_column_sets(control_inspector, "inbox_events")


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
