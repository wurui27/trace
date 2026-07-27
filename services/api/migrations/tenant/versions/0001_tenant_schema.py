"""Create the isolated tenant schema.

Revision ID: 0001_tenant_schema
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_tenant_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _record_columns(*, versioned: bool = False) -> list[sa.Column[object]]:
    columns: list[sa.Column[object]] = [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
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
    ]
    if versioned:
        columns.append(
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "applications",
        *_record_columns(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_applications"),
    )

    op.create_table(
        "application_versions",
        *_record_columns(),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_name", sa.String(length=255), nullable=False),
        sa.Column("version_name", sa.String(length=255), nullable=False),
        sa.Column("version_code", sa.Integer(), nullable=False),
        sa.Column("min_api_level", sa.Integer(), nullable=True),
        sa.Column(
            "supported_abis",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "min_api_level IS NULL OR min_api_level > 0",
            name="ck_application_versions_min_api",
        ),
        sa.CheckConstraint(
            "version_code >= 0", name="ck_application_versions_version_code"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_application_versions_application_id_applications",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_application_versions"),
        sa.UniqueConstraint(
            "application_id", "version_code", name="uq_application_versions_app_code"
        ),
    )
    op.create_index(
        "ix_application_versions_application_id",
        "application_versions",
        ["application_id"],
    )

    op.create_table(
        "scenario_recipes",
        *_record_columns(),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_type", sa.String(length=32), nullable=False),
        sa.Column("recipe_version", sa.Integer(), nullable=False),
        sa.Column("recipe_hash", sa.String(length=64), nullable=False),
        sa.Column("recipe", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_recipes_type",
        ),
        sa.CheckConstraint("recipe_version > 0", name="ck_scenario_recipes_version"),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_scenario_recipes_application_id_applications",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scenario_recipes"),
        sa.UniqueConstraint(
            "application_id",
            "scenario_type",
            "recipe_version",
            name="uq_scenario_recipes_app_type_version",
        ),
    )
    op.create_index(
        "ix_scenario_recipes_application_id", "scenario_recipes", ["application_id"]
    )

    op.create_table(
        "analyses",
        *_record_columns(versioned=True),
        sa.Column("application_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("analysis_mode", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.CheckConstraint(
            "analysis_mode IN ('device', 'trace_upload')", name="ck_analyses_mode"
        ),
        sa.CheckConstraint(
            "state IN ('creating', 'created', 'uploading', 'queued', 'scheduled', 'running', "
            "'analyzing', 'completed', 'partially_completed', 'failed', 'canceled', 'deleted')",
            name="ck_analyses_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_analyses_version_positive"),
        sa.ForeignKeyConstraint(
            ["application_version_id"],
            ["application_versions.id"],
            name="fk_analyses_application_version_id_application_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analyses"),
    )
    op.create_index(
        "ix_analyses_application_version_id", "analyses", ["application_version_id"]
    )
    op.create_index("ix_analyses_state_created", "analyses", ["state", "created_at"])

    op.create_table(
        "scenario_results",
        *_record_columns(versioned=True),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_type", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("device_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("validity", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_results_type",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'scheduled', 'running', 'analyzing', 'completed', "
            "'failed', 'canceled')",
            name="ck_scenario_results_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_scenario_results_version_positive"),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name="fk_scenario_results_analysis_id_analyses",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scenario_results"),
        sa.UniqueConstraint(
            "analysis_id", "scenario_type", name="uq_scenario_results_analysis_type"
        ),
    )
    op.create_index(
        "ix_scenario_results_state_created",
        "scenario_results",
        ["state", "created_at"],
    )

    op.create_table(
        "sample_attempts",
        *_record_columns(versioned=True),
        sa.Column("scenario_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("verdict_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invalid_reason", sa.String(length=96), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_no > 0 AND attempt_no <= 10",
            name="ck_sample_attempts_attempt_range",
        ),
        sa.CheckConstraint(
            "state IN ('uploading', 'finalized', 'validating', 'valid', 'invalid', "
            "'validation_error')",
            name="ck_sample_attempts_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_sample_attempts_version_positive"),
        sa.ForeignKeyConstraint(
            ["scenario_job_id"],
            ["scenario_results.id"],
            name="fk_sample_attempts_scenario_job_id_scenario_results",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sample_attempts"),
        sa.UniqueConstraint(
            "scenario_job_id", "attempt_no", name="uq_sample_attempts_job_attempt"
        ),
    )
    op.create_index(
        "ix_sample_attempts_state_created", "sample_attempts", ["state", "created_at"]
    )

    op.create_table(
        "artifacts",
        *_record_columns(versioned=True),
        sa.Column("application_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scenario_result_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sample_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_kind", sa.String(length=96), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256_b64", sa.String(length=44), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("version_id", sa.String(length=1024), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(application_version_id, analysis_id, scenario_result_id, "
            "sample_attempt_id) = 1",
            name="ck_artifacts_exactly_one_owner",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonnegative"),
        sa.CheckConstraint(
            "state IN ('pending', 'finalized', 'expired', 'deleted')",
            name="ck_artifacts_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_artifacts_version_positive"),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name="fk_artifacts_analysis_id_analyses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["application_version_id"],
            ["application_versions.id"],
            name="fk_artifacts_application_version_id_application_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sample_attempt_id"],
            ["sample_attempts.id"],
            name="fk_artifacts_sample_attempt_id_sample_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_result_id"],
            ["scenario_results.id"],
            name="fk_artifacts_scenario_result_id_scenario_results",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint("object_key", "version_id", name="uq_artifacts_object_version"),
        sa.UniqueConstraint("upload_id", name="uq_artifacts_upload_id"),
    )
    op.create_index(
        "ix_artifacts_application_version_id", "artifacts", ["application_version_id"]
    )
    op.create_index("ix_artifacts_analysis_id", "artifacts", ["analysis_id"])
    op.create_index(
        "ix_artifacts_scenario_result_id", "artifacts", ["scenario_result_id"]
    )
    op.create_index(
        "ix_artifacts_sample_attempt_id", "artifacts", ["sample_attempt_id"]
    )
    op.create_index(
        "ix_artifacts_state_expires", "artifacts", ["state", "expires_at"]
    )

    op.create_table(
        "report_versions",
        *_record_columns(),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tool_version", sa.String(length=128), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("source_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('complete', 'partial', 'failed')", name="ck_report_versions_state"
        ),
        sa.CheckConstraint("report_version > 0", name="ck_report_versions_version"),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name="fk_report_versions_analysis_id_analyses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["artifacts.id"],
            name="fk_report_versions_source_artifact_id_artifacts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_report_versions"),
        sa.UniqueConstraint(
            "analysis_id", "report_version", name="uq_report_versions_analysis_version"
        ),
    )
    op.create_index(
        "ix_report_versions_source_artifact_id",
        "report_versions",
        ["source_artifact_id"],
    )

    op.create_table(
        "metrics",
        *_record_columns(),
        sa.Column("report_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("numeric_value", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["report_version_id"],
            ["report_versions.id"],
            name="fk_metrics_report_version_id_report_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_result_id"],
            ["scenario_results.id"],
            name="fk_metrics_scenario_result_id_scenario_results",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metrics"),
        sa.UniqueConstraint(
            "report_version_id",
            "scenario_result_id",
            "metric_name",
            name="uq_metrics_report_scenario_name",
        ),
    )
    op.create_index("ix_metrics_scenario_result_id", "metrics", ["scenario_result_id"])

    op.create_table(
        "findings",
        *_record_columns(),
        sa.Column("report_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stable_code", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "location",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')",
            name="ck_findings_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_findings_status",
        ),
        sa.ForeignKeyConstraint(
            ["report_version_id"],
            ["report_versions.id"],
            name="fk_findings_report_version_id_report_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_result_id"],
            ["scenario_results.id"],
            name="fk_findings_scenario_result_id_scenario_results",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_findings"),
        sa.UniqueConstraint(
            "report_version_id",
            "scenario_result_id",
            "stable_code",
            name="uq_findings_report_scenario_code",
        ),
    )
    op.create_index(
        "ix_findings_scenario_result_id", "findings", ["scenario_result_id"]
    )

    op.create_table(
        "evidence",
        *_record_columns(),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metric_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_type", sa.String(length=96), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "num_nonnulls(artifact_id, metric_id) <= 1",
            name="ck_evidence_at_most_one_reference",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name="fk_evidence_artifact_id_artifacts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name="fk_evidence_finding_id_findings",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"],
            ["metrics.id"],
            name="fk_evidence_metric_id_metrics",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
    )
    op.create_index("ix_evidence_finding_id", "evidence", ["finding_id"])
    op.create_index("ix_evidence_artifact_id", "evidence", ["artifact_id"])
    op.create_index("ix_evidence_metric_id", "evidence", ["metric_id"])

    op.create_table(
        "recommendations",
        *_record_columns(),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(length=255), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.CheckConstraint("rank > 0", name="ck_recommendations_rank_positive"),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name="fk_recommendations_finding_id_findings",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recommendations"),
        sa.UniqueConstraint("finding_id", "rank", name="uq_recommendations_finding_rank"),
    )


def downgrade() -> None:
    op.drop_table("recommendations")
    op.drop_table("evidence")
    op.drop_table("findings")
    op.drop_table("metrics")
    op.drop_table("report_versions")
    op.drop_table("artifacts")
    op.drop_table("sample_attempts")
    op.drop_table("scenario_results")
    op.drop_table("analyses")
    op.drop_table("scenario_recipes")
    op.drop_table("application_versions")
    op.drop_table("applications")
