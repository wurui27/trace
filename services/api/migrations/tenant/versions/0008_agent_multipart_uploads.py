"""Persist resumable Agent artifact multipart uploads.

Revision ID: 0008_agent_multipart_uploads
Revises: 0007_analysis_report_versions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_agent_multipart_uploads"
down_revision: str | None = "0007_analysis_report_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_multipart_uploads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_upload_id", sa.String(length=1024), nullable=False),
        sa.Column("part_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column(
            "completed_parts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'aborted', 'expired')",
            name="ck_artifact_multipart_uploads_state",
        ),
        sa.CheckConstraint(
            "part_size_bytes > 0",
            name="ck_artifact_multipart_uploads_part_size_positive",
        ),
        sa.CheckConstraint(
            "part_count >= 1 AND part_count <= 10000",
            name="ck_artifact_multipart_uploads_part_count_range",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(completed_parts) = 'array'",
            name="ck_artifact_multipart_uploads_completed_parts_array",
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL) OR "
            "(state <> 'completed' AND completed_at IS NULL)",
            name="ck_artifact_multipart_uploads_completion_state",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_artifact_multipart_uploads_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name="fk_artifact_multipart_uploads_artifact_id_artifacts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_multipart_uploads"),
        sa.UniqueConstraint(
            "artifact_id", name="uq_artifact_multipart_uploads_artifact"
        ),
        sa.UniqueConstraint(
            "storage_upload_id",
            name="uq_artifact_multipart_uploads_storage_upload",
        ),
    )
    op.create_index(
        "ix_artifact_multipart_uploads_execution_id",
        "artifact_multipart_uploads",
        ["execution_id"],
    )
    op.create_index(
        "ix_artifact_multipart_uploads_state_expires",
        "artifact_multipart_uploads",
        ["state", "expires_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE artifact_multipart_uploads IN ACCESS EXCLUSIVE MODE")
    )
    if (
        connection.scalar(
            sa.text("SELECT 1 FROM artifact_multipart_uploads LIMIT 1")
        )
        is not None
    ):
        raise RuntimeError(
            "multipart upload downgrade preflight failed: upload state must be "
            "exported before downgrade"
        )
    op.drop_table("artifact_multipart_uploads")
