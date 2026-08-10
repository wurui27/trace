"""Bind analyses to public Agent source workspaces.

Revision ID: 0012_source_bindings
Revises: 0011_agent_execution_completion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_source_bindings"
down_revision: str | None = "0011_agent_execution_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("global_jobs", sa.Column("source_provider_kind", sa.String(32)))
    op.add_column(
        "global_jobs", sa.Column("source_agent_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "global_jobs", sa.Column("source_workspace_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column("global_jobs", sa.Column("source_snapshot_policy", sa.String(32)))
    op.add_column(
        "global_jobs",
        sa.Column("source_validation_profile_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_check_constraint(
        "ck_global_jobs_source_binding_group",
        "global_jobs",
        "num_nonnulls(source_provider_kind, source_agent_id, source_workspace_id, "
        "source_snapshot_policy) IN (0, 4)",
    )
    op.create_check_constraint(
        "ck_global_jobs_source_provider_kind",
        "global_jobs",
        "source_provider_kind IS NULL OR source_provider_kind = 'agent_workspace'",
    )
    op.create_check_constraint(
        "ck_global_jobs_source_snapshot_policy",
        "global_jobs",
        "source_snapshot_policy IS NULL OR source_snapshot_policy = 'tracked_worktree'",
    )
    op.create_check_constraint(
        "ck_global_jobs_source_profile_binding",
        "global_jobs",
        "source_provider_kind IS NOT NULL OR source_validation_profile_id IS NULL",
    )
    op.create_foreign_key(
        "fk_global_jobs_source_agent_team",
        "global_jobs",
        "agents",
        ["source_agent_id", "team_id"],
        ["id", "team_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_global_jobs_source_agent_team",
        "global_jobs",
        ["source_agent_id", "team_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_global_jobs_source_agent_team", table_name="global_jobs")
    op.drop_constraint(
        "fk_global_jobs_source_agent_team", "global_jobs", type_="foreignkey"
    )
    for name in (
        "ck_global_jobs_source_profile_binding",
        "ck_global_jobs_source_snapshot_policy",
        "ck_global_jobs_source_provider_kind",
        "ck_global_jobs_source_binding_group",
    ):
        op.drop_constraint(name, "global_jobs", type_="check")
    for name in (
        "source_validation_profile_id",
        "source_snapshot_policy",
        "source_workspace_id",
        "source_agent_id",
        "source_provider_kind",
    ):
        op.drop_column("global_jobs", name)
