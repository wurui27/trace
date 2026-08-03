"""Persist Trace upload profile and declared immutable inputs.

Revision ID: 0005_trace_upload_inputs
Revises: 0004_memory_upload_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_trace_upload_inputs"
down_revision: str | None = "0004_memory_upload_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE analyses IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text("SELECT 1 FROM analyses WHERE analysis_mode = 'trace_upload' LIMIT 1")
        )
        is not None
    ):
        raise RuntimeError("trace upload input migration preflight failed")
    op.add_column(
        "analyses",
        sa.Column("analysis_profile", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "analyses",
        sa.Column("input_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_analyses_trace_input_metadata",
        "analyses",
        "(analysis_mode = 'trace_upload' "
        "AND analysis_profile IN ('auto', 'startup', 'scroll') "
        "AND input_manifest IS NOT NULL "
        "AND jsonb_typeof(input_manifest) = 'array') OR "
        "(analysis_mode <> 'trace_upload' "
        "AND analysis_profile IS NULL AND input_manifest IS NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE analyses IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text("SELECT 1 FROM analyses WHERE analysis_mode = 'trace_upload' LIMIT 1")
        )
        is not None
    ):
        raise RuntimeError("trace upload input downgrade preflight failed")
    op.drop_constraint("ck_analyses_trace_input_metadata", "analyses", type_="check")
    op.drop_column("analyses", "input_manifest")
    op.drop_column("analyses", "analysis_profile")
