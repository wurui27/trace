"""Serialize active direct-parent Trace worker claims.

Revision ID: 0007_trace_worker_claims
Revises: 0006_engine_tenant_version
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_trace_worker_claims"
down_revision: str | None = "0006_engine_tenant_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_worker_claims_active_global_job",
        "worker_claims",
        ["global_job_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active' AND global_job_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_worker_claims_active_global_job",
        table_name="worker_claims",
    )
