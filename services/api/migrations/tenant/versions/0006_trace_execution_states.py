"""Allow trace-upload analyses to advance without an application version.

Revision ID: 0006_trace_execution_states
Revises: 0005_trace_upload_inputs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_trace_execution_states"
down_revision: str | None = "0005_trace_upload_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_analyses_application_version_ready",
        "analyses",
        type_="check",
    )
    op.create_check_constraint(
        "ck_analyses_application_version_ready",
        "analyses",
        "analysis_mode = 'trace_upload' OR application_version_id IS NOT NULL OR state IN "
        "('creating', 'created', 'uploading', 'failed', 'canceled', 'deleted')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE analyses IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM analyses WHERE analysis_mode = 'trace_upload' "
                "AND application_version_id IS NULL "
                "AND state NOT IN "
                "('creating', 'created', 'uploading', 'failed', 'canceled', 'deleted') "
                "LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError("trace execution state downgrade preflight failed")
    op.drop_constraint(
        "ck_analyses_application_version_ready",
        "analyses",
        type_="check",
    )
    op.create_check_constraint(
        "ck_analyses_application_version_ready",
        "analyses",
        "application_version_id IS NOT NULL OR state IN "
        "('creating', 'created', 'uploading', 'failed', 'canceled', 'deleted')",
    )
