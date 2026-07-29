"""Allow memory upload analyses.

Revision ID: 0005_memory_upload_mode
Revises: 0004_external_engine_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_memory_upload_mode"
down_revision: str | None = "0004_external_engine_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_global_jobs_analysis_mode",
        "global_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_global_jobs_analysis_mode",
        "global_jobs",
        "analysis_mode IN ('device', 'trace_upload', 'memory_upload')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE global_jobs IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM global_jobs "
                "WHERE analysis_mode = 'memory_upload' LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError("memory upload downgrade preflight failed")
    op.drop_constraint(
        "ck_global_jobs_analysis_mode",
        "global_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_global_jobs_analysis_mode",
        "global_jobs",
        "analysis_mode IN ('device', 'trace_upload')",
    )
