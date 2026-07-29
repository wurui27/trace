"""Persist opaque external engine workspaces and execution authority.

Revision ID: 0004_external_engine_foundation
Revises: 0003_analysis_orchestration
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_external_engine_foundation"
down_revision: str | None = "0003_analysis_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _record_columns() -> list[sa.Column[object]]:
    return [
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
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    ]


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_global_jobs_id_team",
        "global_jobs",
        ["id", "team_id"],
    )
    op.create_table(
        "team_engine_workspaces",
        *_record_columns(),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine_id", sa.String(length=64), nullable=False),
        sa.Column("external_workspace_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "state IN ('provisioning', 'active', 'deleting', 'deleted', 'failed')",
            name="ck_team_engine_workspaces_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_team_engine_workspaces_version_positive"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_team_engine_workspaces_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_team_engine_workspaces"),
        sa.UniqueConstraint("team_id", "engine_id", name="uq_team_engine_workspaces_team_engine"),
        sa.UniqueConstraint(
            "engine_id",
            "external_workspace_id",
            name="uq_team_engine_workspaces_external",
        ),
    )
    op.create_table(
        "engine_executions",
        *_record_columns(),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("engine_commit_sha", sa.String(length=40), nullable=False),
        sa.Column("engine_image_digest", sa.String(length=71), nullable=False),
        sa.Column("input_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("external_workspace_id", sa.String(length=255), nullable=True),
        sa.Column("external_session_id", sa.String(length=255), nullable=True),
        sa.Column("external_run_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("last_event_cursor", sa.String(length=255), nullable=True),
        sa.Column("stable_error_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_result_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("normalized_report_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_engine_executions_attempt_positive"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'awaiting_user', 'completed', "
            "'insufficient_data', 'failed', 'canceled')",
            name="ck_engine_executions_state",
        ),
        sa.CheckConstraint(
            "engine_commit_sha ~ '^[0-9a-f]{40}$'",
            name="ck_engine_executions_commit_sha",
        ),
        sa.CheckConstraint(
            "engine_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_engine_executions_image_digest",
        ),
        sa.CheckConstraint(
            "input_manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_engine_executions_input_manifest_hash",
        ),
        sa.CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_engine_executions_config_hash",
        ),
        sa.CheckConstraint("version > 0", name="ck_engine_executions_version_positive"),
        sa.ForeignKeyConstraint(
            ["analysis_id", "team_id"],
            ["global_jobs.id", "global_jobs.team_id"],
            name="fk_engine_executions_analysis_team",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_engine_executions"),
        sa.UniqueConstraint(
            "analysis_id",
            "engine_id",
            "attempt_number",
            name="uq_engine_executions_analysis_engine_attempt",
        ),
    )
    op.create_index(
        "ix_engine_executions_state_created",
        "engine_executions",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_engine_executions_team_analysis",
        "engine_executions",
        ["team_id", "analysis_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if (
        connection.scalar(sa.text("SELECT 1 FROM engine_executions LIMIT 1")) is not None
        or connection.scalar(sa.text("SELECT 1 FROM team_engine_workspaces LIMIT 1")) is not None
    ):
        raise RuntimeError(
            "external engine foundation downgrade preflight failed: "
            "engine metadata must be exported before downgrade"
        )
    op.drop_index("ix_engine_executions_team_analysis", table_name="engine_executions")
    op.drop_index("ix_engine_executions_state_created", table_name="engine_executions")
    op.drop_table("engine_executions")
    op.drop_table("team_engine_workspaces")
    op.drop_constraint("uq_global_jobs_id_team", "global_jobs", type_="unique")
