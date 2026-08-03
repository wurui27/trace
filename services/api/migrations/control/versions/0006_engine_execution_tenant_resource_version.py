"""Fence engine executions to their input authorization tenant version.

Revision ID: 0006_engine_tenant_version
Revises: 0005_memory_upload_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_engine_tenant_version"
down_revision: str | None = "0005_memory_upload_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_and_require_empty() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE engine_executions IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM engine_executions LIMIT 1")) is not None:
        raise RuntimeError("engine execution tenant version migration preflight failed")


def upgrade() -> None:
    _lock_and_require_empty()
    op.add_column(
        "engine_executions",
        sa.Column("tenant_resource_version", sa.Integer(), nullable=False),
    )
    op.create_check_constraint(
        "ck_engine_executions_tenant_resource_version_positive",
        "engine_executions",
        "tenant_resource_version > 0",
    )


def downgrade() -> None:
    _lock_and_require_empty()
    op.drop_constraint(
        "ck_engine_executions_tenant_resource_version_positive",
        "engine_executions",
        type_="check",
    )
    op.drop_column("engine_executions", "tenant_resource_version")
