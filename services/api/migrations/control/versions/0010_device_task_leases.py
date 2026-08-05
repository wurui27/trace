"""Require selected devices once device capture scheduling begins.

Revision ID: 0010_device_task_leases
Revises: 0009_remote_device_agents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_device_task_leases"
down_revision: str | None = "0009_remote_device_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE global_jobs IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM global_jobs "
                "WHERE analysis_mode = 'device' "
                "AND state IN ('scheduled', 'running') "
                "AND selected_device_id IS NULL LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "device task lease migration preflight failed: scheduled device jobs "
            "must be bound to a selected device"
        )
    op.drop_constraint(
        "ck_global_jobs_device_selection",
        "global_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_global_jobs_device_selection",
        "global_jobs",
        "(analysis_mode <> 'device' AND selected_device_id IS NULL) OR "
        "(analysis_mode = 'device' AND "
        "(state NOT IN ('scheduled', 'running') OR "
        "selected_device_id IS NOT NULL))",
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("LOCK TABLE global_jobs IN ACCESS EXCLUSIVE MODE"))
    op.drop_constraint(
        "ck_global_jobs_device_selection",
        "global_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_global_jobs_device_selection",
        "global_jobs",
        "selected_device_id IS NULL OR analysis_mode = 'device'",
    )
