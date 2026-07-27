"""Create the isolated control-plane schema.

Revision ID: 0001_control_schema
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_control_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _record_columns(*, versioned: bool = False) -> list[sa.Column[object]]:
    columns: list[sa.Column[object]] = [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]
    if versioned:
        columns.append(
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "users",
        *_record_columns(),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), server_default="active", nullable=False),
        sa.CheckConstraint("state IN ('active', 'disabled')", name="ck_users_state"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "teams",
        *_record_columns(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="active", nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'deleting', 'deleted')", name="ck_teams_state"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_teams"),
    )

    op.create_table(
        "memberships",
        *_record_columns(),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "role IN ('team_owner', 'team_member', 'team_viewer')",
            name="ck_memberships_role",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_memberships_team_id_teams", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_memberships_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memberships"),
        sa.UniqueConstraint("team_id", "user_id", name="uq_memberships_team_user"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])

    op.create_table(
        "tenant_resources",
        *_record_columns(versioned=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "resource_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("state", sa.String(length=32), server_default="requested", nullable=False),
        sa.Column("database_name", sa.String(length=255), nullable=True),
        sa.Column("database_secret_ref", sa.String(length=512), nullable=True),
        sa.Column("bucket_name", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "resource_version > 0", name="ck_tenant_resources_resource_version"
        ),
        sa.CheckConstraint(
            "state IN ('requested', 'provisioning', 'active', 'cleanup_pending', 'migrating')",
            name="ck_tenant_resources_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_tenant_resources_version_positive"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_tenant_resources_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_resources"),
        sa.UniqueConstraint(
            "team_id", "resource_version", name="uq_tenant_resources_team_version"
        ),
    )
    op.create_index(
        "ix_tenant_resources_team_state", "tenant_resources", ["team_id", "state"]
    )

    op.create_table(
        "agents",
        *_record_columns(versioned=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("registration_code_digest", sa.String(length=64), nullable=True),
        sa.Column("registration_code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_code_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_digest", sa.String(length=64), nullable=True),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'online', 'offline', 'revoked')", name="ck_agents_state"
        ),
        sa.CheckConstraint("token_version > 0", name="ck_agents_token_version_positive"),
        sa.CheckConstraint("version > 0", name="ck_agents_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("name", name="uq_agents_name"),
    )
    op.create_index(
        "ix_agents_state_last_heartbeat", "agents", ["state", "last_heartbeat_at"]
    )

    op.create_table(
        "devices",
        *_record_columns(versioned=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("serial", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="offline", nullable=False),
        sa.Column("api_level", sa.Integer(), nullable=True),
        sa.Column("abi", sa.String(length=64), nullable=True),
        sa.Column("build_fingerprint", sa.String(length=512), nullable=True),
        sa.Column(
            "display_modes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("temperature_c", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("thermal_state", sa.String(length=64), nullable=True),
        sa.Column("storage_available_bytes", sa.BigInteger(), nullable=True),
        sa.Column("is_rooted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "is_profileable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "perfetto_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "api_level IS NULL OR api_level > 0", name="ck_devices_api_level"
        ),
        sa.CheckConstraint(
            "state IN ('healthy', 'busy', 'quarantined', 'offline')",
            name="ck_devices_state",
        ),
        sa.CheckConstraint(
            "storage_available_bytes IS NULL OR storage_available_bytes >= 0",
            name="ck_devices_storage_nonnegative",
        ),
        sa.CheckConstraint("version > 0", name="ck_devices_version_positive"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_devices_agent_id_agents", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
        sa.UniqueConstraint("serial", name="uq_devices_serial"),
    )
    op.create_index("ix_devices_agent_id", "devices", ["agent_id"])
    op.create_index("ix_devices_state_last_seen", "devices", ["state", "last_seen_at"])

    op.create_table(
        "global_jobs",
        *_record_columns(versioned=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("analysis_mode", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("input_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("required_abi", sa.String(length=64), nullable=True),
        sa.Column("min_api_level", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "valid_sample_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "invalid_sample_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column(
            "device_migration_allowed",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.CheckConstraint(
            "analysis_mode IN ('device', 'trace_upload')",
            name="ck_global_jobs_analysis_mode",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND valid_sample_count >= 0 AND invalid_sample_count >= 0 "
            "AND retry_count >= 0 AND valid_sample_count + invalid_sample_count <= attempt_count",
            name="ck_global_jobs_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "state IN ('creating', 'created', 'uploading', 'queued', 'scheduled', "
            "'running', 'analyzing', 'completed', 'partially_completed', 'failed', 'canceled')",
            name="ck_global_jobs_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_global_jobs_version_positive"),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_global_jobs_team_id_teams", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_global_jobs"),
        sa.UniqueConstraint(
            "team_id", "idempotency_key", name="uq_global_jobs_team_idempotency"
        ),
    )
    op.create_index(
        "ix_global_jobs_team_state_created",
        "global_jobs",
        ["team_id", "state", "created_at"],
    )

    op.create_table(
        "scenario_jobs",
        *_record_columns(versioned=True),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_type", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("input_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("required_abi", sa.String(length=64), nullable=True),
        sa.Column("min_api_level", sa.Integer(), nullable=True),
        sa.Column("device_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "valid_sample_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "invalid_sample_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0 AND valid_sample_count >= 0 AND invalid_sample_count >= 0 "
            "AND retry_count >= 0 AND max_attempts > 0 "
            "AND valid_sample_count + invalid_sample_count <= attempt_count "
            "AND attempt_count <= max_attempts",
            name="ck_scenario_jobs_sample_counts",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'scheduled', 'running', 'analyzing', 'completed', "
            "'failed', 'canceled')",
            name="ck_scenario_jobs_state",
        ),
        sa.CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_jobs_type",
        ),
        sa.CheckConstraint("version > 0", name="ck_scenario_jobs_version_positive"),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["global_jobs.id"],
            name="fk_scenario_jobs_analysis_id_global_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scenario_jobs"),
        sa.UniqueConstraint(
            "analysis_id", "scenario_type", name="uq_scenario_jobs_analysis_type"
        ),
    )
    op.create_index(
        "ix_scenario_jobs_state_created", "scenario_jobs", ["state", "created_at"]
    )

    op.create_table(
        "agent_leases",
        *_record_columns(versioned=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("global_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_token_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'released', 'expired', 'revoked')",
            name="ck_agent_leases_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_agent_leases_version_positive"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_agent_leases_agent_id_agents", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name="fk_agent_leases_device_id_devices",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["global_job_id"],
            ["global_jobs.id"],
            name="fk_agent_leases_global_job_id_global_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_leases"),
    )
    op.create_index("ix_agent_leases_agent_id", "agent_leases", ["agent_id"])
    op.create_index("ix_agent_leases_global_job_id", "agent_leases", ["global_job_id"])
    op.create_index(
        "ix_agent_leases_state_expires", "agent_leases", ["state", "expires_at"]
    )
    op.create_index(
        "uq_agent_leases_active_device",
        "agent_leases",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )

    op.create_table(
        "outbox_events",
        *_record_columns(versioned=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("global_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scenario_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("retry_count >= 0", name="ck_outbox_events_retry_count"),
        sa.CheckConstraint("version > 0", name="ck_outbox_events_version_positive"),
        sa.ForeignKeyConstraint(
            ["global_job_id"],
            ["global_jobs.id"],
            name="fk_outbox_events_global_job_id_global_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_job_id"],
            ["scenario_jobs.id"],
            name="fk_outbox_events_scenario_job_id_scenario_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_outbox_events_team_id_teams", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.create_index("ix_outbox_events_team_id", "outbox_events", ["team_id"])
    op.create_index("ix_outbox_events_global_job_id", "outbox_events", ["global_job_id"])
    op.create_index(
        "ix_outbox_events_scenario_job_id", "outbox_events", ["scenario_job_id"]
    )
    op.create_index(
        "ix_outbox_events_ready_unpublished",
        "outbox_events",
        ["ready_at", "created_at"],
        postgresql_where=sa.text("ready_at IS NOT NULL AND published_at IS NULL"),
    )

    op.create_table(
        "inbox_events",
        *_record_columns(versioned=True),
        sa.Column("consumer_name", sa.String(length=255), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="received", nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('received', 'processed')", name="ck_inbox_events_state"
        ),
        sa.CheckConstraint("version > 0", name="ck_inbox_events_version_positive"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["outbox_events.id"],
            name="fk_inbox_events_event_id_outbox_events",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inbox_events"),
        sa.UniqueConstraint(
            "consumer_name", "event_id", name="uq_inbox_events_consumer_event"
        ),
    )
    op.create_index("ix_inbox_events_event_id", "inbox_events", ["event_id"])
    op.create_index(
        "ix_inbox_events_state_received", "inbox_events", ["state", "received_at"]
    )

    op.create_table(
        "sample_validation_claims",
        *_record_columns(versioned=True),
        sa.Column("scenario_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sample_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer_id", sa.String(length=255), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("verdict_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "retry_count >= 0", name="ck_sample_validation_claims_retry_count"
        ),
        sa.CheckConstraint(
            "state IN ('active', 'completed', 'expired', 'revoked')",
            name="ck_sample_validation_claims_state",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_sample_validation_claims_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["scenario_job_id"],
            ["scenario_jobs.id"],
            name="fk_sample_validation_claims_scenario_job_id_scenario_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sample_validation_claims"),
        sa.UniqueConstraint("sample_id", name="uq_sample_validation_claims_sample_id"),
    )
    op.create_index(
        "ix_sample_validation_claims_scenario_job_id",
        "sample_validation_claims",
        ["scenario_job_id"],
    )
    op.create_index(
        "ix_sample_validation_claims_state_expires",
        "sample_validation_claims",
        ["state", "expires_at"],
    )

    op.create_table(
        "worker_claims",
        *_record_columns(versioned=True),
        sa.Column("global_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scenario_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer_id", sa.String(length=255), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(global_job_id, scenario_job_id) = 1",
            name="ck_worker_claims_exactly_one_subject",
        ),
        sa.CheckConstraint("retry_count >= 0", name="ck_worker_claims_retry_count"),
        sa.CheckConstraint(
            "state IN ('active', 'completed', 'expired', 'revoked')",
            name="ck_worker_claims_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_worker_claims_version_positive"),
        sa.ForeignKeyConstraint(
            ["global_job_id"],
            ["global_jobs.id"],
            name="fk_worker_claims_global_job_id_global_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_job_id"],
            ["scenario_jobs.id"],
            name="fk_worker_claims_scenario_job_id_scenario_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_worker_claims"),
    )
    op.create_index("ix_worker_claims_global_job_id", "worker_claims", ["global_job_id"])
    op.create_index(
        "ix_worker_claims_scenario_job_id", "worker_claims", ["scenario_job_id"]
    )
    op.create_index(
        "ix_worker_claims_state_expires", "worker_claims", ["state", "expires_at"]
    )

    op.create_table(
        "idempotency_keys",
        *_record_columns(versioned=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("response_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')", name="ck_idempotency_keys_state"
        ),
        sa.CheckConstraint("version > 0", name="ck_idempotency_keys_version_positive"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_idempotency_keys_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        sa.UniqueConstraint("team_id", "key", name="uq_idempotency_keys_team_key"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    op.create_table(
        "sessions",
        *_record_columns(versioned=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("csrf_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('pre_auth', 'authenticated')", name="ck_sessions_kind"
        ),
        sa.CheckConstraint("version > 0", name="ck_sessions_version_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_digest", name="uq_sessions_token_digest"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index(
        "ix_sessions_absolute_expires_at", "sessions", ["absolute_expires_at"]
    )

    op.create_table(
        "tenant_quotas",
        *_record_columns(versioned=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "active_device_limit", sa.Integer(), server_default=sa.text("2"), nullable=False
        ),
        sa.Column(
            "queued_device_limit", sa.Integer(), server_default=sa.text("20"), nullable=False
        ),
        sa.CheckConstraint(
            "active_device_limit >= 0 AND queued_device_limit >= 0",
            name="ck_tenant_quotas_nonnegative",
        ),
        sa.CheckConstraint("version > 0", name="ck_tenant_quotas_version_positive"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_tenant_quotas_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_quotas"),
        sa.UniqueConstraint("team_id", name="uq_tenant_quotas_team_id"),
    )

    op.create_table(
        "audit_events",
        *_record_columns(),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_audit_events_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_audit_events_team_id_teams",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_team_id", "audit_events", ["team_id"])
    op.create_index(
        "ix_audit_events_event_type_created_at",
        "audit_events",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("tenant_quotas")
    op.drop_table("sessions")
    op.drop_table("idempotency_keys")
    op.drop_table("worker_claims")
    op.drop_table("sample_validation_claims")
    op.drop_table("inbox_events")
    op.drop_table("outbox_events")
    op.drop_table("agent_leases")
    op.drop_table("scenario_jobs")
    op.drop_table("global_jobs")
    op.drop_table("devices")
    op.drop_table("agents")
    op.drop_table("tenant_resources")
    op.drop_table("memberships")
    op.drop_table("teams")
    op.drop_table("users")
