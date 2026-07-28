"""Add durable idempotency and immutable-state constraints to artifact uploads.

Revision ID: 0002_artifact_upload_slots
Revises: 0001_tenant_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_artifact_upload_slots"
down_revision: str | None = "0001_tenant_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _preflight() -> None:
    connection = op.get_bind()
    duplicate_object_key = connection.scalar(
        sa.text("SELECT 1 FROM artifacts GROUP BY object_key HAVING count(*) > 1 LIMIT 1")
    )
    if duplicate_object_key is not None:
        raise RuntimeError("artifact object-key uniqueness preflight failed")
    inconsistent_state = connection.scalar(
        sa.text(
            "SELECT 1 FROM artifacts WHERE "
            "(state = 'pending' AND (version_id IS NOT NULL OR finalized_at IS NOT NULL)) OR "
            "(state = 'finalized' AND (version_id IS NULL OR finalized_at IS NULL)) "
            "LIMIT 1"
        )
    )
    if inconsistent_state is not None:
        raise RuntimeError("artifact state-metadata preflight failed")


def upgrade() -> None:
    _preflight()
    op.add_column(
        "artifacts",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "artifacts",
        sa.Column("request_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_artifacts_idempotency_pair",
        "artifacts",
        "(idempotency_key IS NULL AND request_hash IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_artifacts_request_hash",
        "artifacts",
        "request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_artifacts_state_metadata",
        "artifacts",
        "(state <> 'pending' OR (version_id IS NULL AND finalized_at IS NULL)) AND "
        "(state <> 'finalized' OR (version_id IS NOT NULL AND finalized_at IS NOT NULL))",
    )
    op.create_unique_constraint(
        "uq_artifacts_object_key",
        "artifacts",
        ["object_key"],
    )
    op.create_index(
        "uq_artifacts_analysis_idempotency",
        "artifacts",
        ["analysis_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("analysis_id IS NOT NULL AND idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_artifacts_analysis_idempotency", table_name="artifacts")
    op.drop_constraint("uq_artifacts_object_key", "artifacts", type_="unique")
    op.drop_constraint("ck_artifacts_state_metadata", "artifacts", type_="check")
    op.drop_constraint("ck_artifacts_request_hash", "artifacts", type_="check")
    op.drop_constraint("ck_artifacts_idempotency_pair", "artifacts", type_="check")
    op.drop_column("artifacts", "request_hash")
    op.drop_column("artifacts", "idempotency_key")
