"""Persist idempotent Agent execution completion manifests.

Revision ID: 0011_agent_execution_completion
Revises: 0010_device_task_leases
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_agent_execution_completion"
down_revision: str | None = "0010_device_task_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_leases",
        sa.Column("completion_manifest_digest", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_agent_leases_completion_manifest_digest",
        "agent_leases",
        "completion_manifest_digest IS NULL OR completion_manifest_digest ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_leases_completion_manifest_digest",
        "agent_leases",
        type_="check",
    )
    op.drop_column("agent_leases", "completion_manifest_digest")
