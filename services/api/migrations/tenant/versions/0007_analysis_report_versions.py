"""Add immutable analysis-level AI report storage.

Revision ID: 0007_analysis_report_versions
Revises: 0006_trace_execution_states
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_analysis_report_versions"
down_revision: str | None = "0006_trace_execution_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_versions",
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "report_versions",
        sa.Column("report_sha256_b64", sa.String(length=44), nullable=True),
    )
    op.add_column(
        "report_versions",
        sa.Column("ai_projection_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "report_versions",
        sa.Column("ai_synthesis_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_versions_ai_projection_artifact",
        "report_versions",
        "artifacts",
        ["ai_projection_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_report_versions_ai_synthesis_artifact",
        "report_versions",
        "artifacts",
        ["ai_synthesis_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_report_versions_ai_projection_artifact_id",
        "report_versions",
        ["ai_projection_artifact_id"],
    )
    op.create_index(
        "ix_report_versions_ai_synthesis_artifact_id",
        "report_versions",
        ["ai_synthesis_artifact_id"],
    )
    op.drop_constraint(
        "ck_report_versions_bundle_metadata",
        "report_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_report_versions_content_shape",
        "report_versions",
        "NOT (bundle IS NOT NULL AND report IS NOT NULL) AND "
        "((bundle IS NULL) = (bundle_sha256_b64 IS NULL)) AND "
        "((report IS NULL) = (report_sha256_b64 IS NULL)) AND "
        "(bundle IS NULL OR scenario_result_id IS NOT NULL) AND "
        "(report IS NULL OR scenario_result_id IS NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE report_versions IN ACCESS EXCLUSIVE MODE"))
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM report_versions WHERE report IS NOT NULL "
                "OR report_sha256_b64 IS NOT NULL "
                "OR ai_projection_artifact_id IS NOT NULL "
                "OR ai_synthesis_artifact_id IS NOT NULL LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError("AI report downgrade preflight failed")
    op.drop_constraint("ck_report_versions_content_shape", "report_versions", type_="check")
    op.create_check_constraint(
        "ck_report_versions_bundle_metadata",
        "report_versions",
        "(bundle IS NULL AND bundle_sha256_b64 IS NULL) OR "
        "(bundle IS NOT NULL AND bundle_sha256_b64 IS NOT NULL "
        "AND scenario_result_id IS NOT NULL)",
    )
    op.drop_index("ix_report_versions_ai_synthesis_artifact_id", table_name="report_versions")
    op.drop_index("ix_report_versions_ai_projection_artifact_id", table_name="report_versions")
    op.drop_constraint(
        "fk_report_versions_ai_synthesis_artifact",
        "report_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_report_versions_ai_projection_artifact",
        "report_versions",
        type_="foreignkey",
    )
    op.drop_column("report_versions", "ai_synthesis_artifact_id")
    op.drop_column("report_versions", "ai_projection_artifact_id")
    op.drop_column("report_versions", "report_sha256_b64")
    op.drop_column("report_versions", "report")
