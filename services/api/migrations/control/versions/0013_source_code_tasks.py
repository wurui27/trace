"""Add independently leased source analysis tasks.

Revision ID: 0013_source_code_tasks
Revises: 0012_source_bindings
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_source_code_tasks"
down_revision: str | None = "0012_source_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("lease_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("request_document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("completion_artifact_id", postgresql.UUID(as_uuid=True)),
        sa.Column("completion_sha256", sa.String(64)),
        sa.Column("failure_code", sa.String(96)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("task_type IN ('source_context', 'patch_verification')", name="ck_source_tasks_type"),
        sa.CheckConstraint("state IN ('queued', 'leased', 'running', 'cancel_requested', 'completed', 'failed', 'canceled', 'expired')", name="ck_source_tasks_state"),
        sa.CheckConstraint("lease_version > 0", name="ck_source_tasks_lease_version_positive"),
        sa.CheckConstraint("version > 0", name="ck_source_tasks_version_positive"),
        sa.CheckConstraint("lease_token_digest IS NULL OR lease_token_digest ~ '^[0-9a-f]{64}$'", name="ck_source_tasks_lease_token_digest"),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_tasks_request_sha256"),
        sa.CheckConstraint("completion_sha256 IS NULL OR completion_sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_tasks_completion_sha256"),
        sa.CheckConstraint("(state = 'queued' AND lease_token_digest IS NULL AND expires_at IS NULL) OR (state <> 'queued' AND (lease_token_digest IS NOT NULL OR state = 'canceled'))", name="ck_source_tasks_lease_shape"),
        sa.CheckConstraint("(task_type = 'source_context' AND NOT (request_document ? 'fix_id')) OR (task_type = 'patch_verification' AND (request_document ->> 'fix_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')", name="ck_source_tasks_request_kind"),
        sa.ForeignKeyConstraint(["analysis_id", "team_id"], ["global_jobs.id", "global_jobs.team_id"], name="fk_source_tasks_analysis_team", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id", "team_id"], ["agents.id", "agents.team_id"], name="fk_source_tasks_agent_team", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_tasks_agent_state_created", "source_tasks", ["agent_id", "state", "created_at"])
    op.create_index("ix_source_tasks_agent_team", "source_tasks", ["agent_id", "team_id"])
    op.create_index("ix_source_tasks_analysis", "source_tasks", ["analysis_id"])
    op.create_index("ix_source_tasks_analysis_team", "source_tasks", ["analysis_id", "team_id"])
    op.create_index("uq_source_tasks_active_agent", "source_tasks", ["agent_id"], unique=True, postgresql_where=sa.text("state IN ('leased', 'running', 'cancel_requested')"))
    op.create_index("uq_source_tasks_active_context", "source_tasks", ["analysis_id"], unique=True, postgresql_where=sa.text("task_type = 'source_context' AND state IN ('queued', 'leased', 'running', 'cancel_requested')"))
    op.create_index("uq_source_tasks_active_patch_fix", "source_tasks", ["analysis_id", sa.text("((request_document ->> 'fix_id'))")], unique=True, postgresql_where=sa.text("task_type = 'patch_verification' AND state IN ('queued', 'leased', 'running', 'cancel_requested')"))


def downgrade() -> None:
    op.drop_table("source_tasks")
