"""Persist authoritative source-context analysis state.

Revision ID: 0009_source_context_state
Revises: 0008_agent_multipart_uploads
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_source_context_state"
down_revision: str | None = "0008_agent_multipart_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analyses",
        sa.Column(
            "source_context_state",
            sa.String(length=32),
            server_default="not_requested",
            nullable=False,
        ),
    )
    op.add_column(
        "analyses",
        sa.Column(
            "source_match_summary",
            sa.String(length=16),
            server_default="none",
            nullable=False,
        ),
    )
    op.add_column("analyses", sa.Column("source_failure_code", sa.String(length=96)))
    op.add_column(
        "analyses",
        sa.Column("source_context_artifact_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "analyses", sa.Column("source_context_checksum", sa.String(length=64))
    )
    op.create_check_constraint(
        "ck_analyses_source_context_state",
        "analyses",
        "source_context_state IN ('not_requested', 'waiting_for_agent', 'extracting', "
        "'available', 'unavailable')",
    )
    op.create_check_constraint(
        "ck_analyses_source_match_summary",
        "analyses",
        "source_match_summary IN ('strong', 'weak', 'none')",
    )
    op.create_check_constraint(
        "ck_analyses_source_context_shape",
        "analyses",
        "(source_context_state = 'available' AND source_context_artifact_id IS NOT NULL "
        "AND source_context_checksum IS NOT NULL AND source_failure_code IS NULL) OR "
        "(source_context_state = 'unavailable' AND source_context_artifact_id IS NULL "
        "AND source_context_checksum IS NULL AND source_failure_code IS NOT NULL) OR "
        "(source_context_state NOT IN ('available', 'unavailable') "
        "AND source_context_artifact_id IS NULL AND source_context_checksum IS NULL "
        "AND source_failure_code IS NULL)",
    )
    op.create_foreign_key(
        "fk_analyses_source_context_artifact_id_artifacts",
        "analyses",
        "artifacts",
        ["source_context_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_analyses_source_context_artifact_id_artifacts",
        "analyses",
        type_="foreignkey",
    )
    op.drop_constraint("ck_analyses_source_context_shape", "analyses", type_="check")
    op.drop_constraint("ck_analyses_source_match_summary", "analyses", type_="check")
    op.drop_constraint("ck_analyses_source_context_state", "analyses", type_="check")
    op.drop_column("analyses", "source_context_checksum")
    op.drop_column("analyses", "source_context_artifact_id")
    op.drop_column("analyses", "source_failure_code")
    op.drop_column("analyses", "source_match_summary")
    op.drop_column("analyses", "source_context_state")
