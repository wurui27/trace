"""Allow memory upload analyses and persist their user question.

Revision ID: 0004_memory_upload_mode
Revises: 0003_analysis_orchestration
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_memory_upload_mode"
down_revision: str | None = "0003_analysis_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_analyses_mode", "analyses", type_="check")
    op.create_check_constraint(
        "ck_analyses_mode",
        "analyses",
        "analysis_mode IN ('device', 'trace_upload', 'memory_upload')",
    )
    op.add_column("analyses", sa.Column("question", sa.String(length=2000), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE analyses IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text("SELECT 1 FROM analyses WHERE analysis_mode = 'memory_upload' LIMIT 1")
        )
        is not None
    ):
        raise RuntimeError("memory upload downgrade preflight failed")
    op.drop_constraint("ck_analyses_mode", "analyses", type_="check")
    op.create_check_constraint(
        "ck_analyses_mode",
        "analyses",
        "analysis_mode IN ('device', 'trace_upload')",
    )
    op.drop_column("analyses", "question")
