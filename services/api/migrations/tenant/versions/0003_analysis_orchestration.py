"""Allow preflight analyses and bind report versions to scenarios.

Revision ID: 0003_analysis_orchestration
Revises: 0002_artifact_upload_slots
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_analysis_orchestration"
down_revision: str | None = "0002_artifact_upload_slots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM application_versions "
                "GROUP BY package_name HAVING count(DISTINCT application_id) > 1 LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "analysis orchestration upgrade preflight failed: "
            "one package belongs to multiple applications"
        )
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM application_versions "
                "GROUP BY application_id HAVING count(DISTINCT package_name) > 1 LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "analysis orchestration upgrade preflight failed: "
            "one application owns multiple packages"
        )
    op.add_column(
        "applications",
        sa.Column("package_name", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE applications AS application SET package_name = candidate.package_name "
            "FROM (SELECT application_id, min(package_name) AS package_name "
            "FROM application_versions GROUP BY application_id "
            "HAVING count(DISTINCT package_name) = 1) AS candidate "
            "WHERE candidate.application_id = application.id"
        )
    )
    op.create_unique_constraint(
        "uq_applications_package_name",
        "applications",
        ["package_name"],
    )

    op.alter_column(
        "application_versions",
        "version_name",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column(
        "application_versions",
        sa.Column("target_api_level", sa.Integer(), nullable=True),
    )
    op.add_column(
        "application_versions",
        sa.Column("launch_activity", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "application_versions",
        sa.Column("has_native_libraries", sa.Boolean(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE application_versions SET has_native_libraries = "
            "jsonb_array_length(supported_abis) > 0"
        )
    )
    op.alter_column(
        "application_versions",
        "has_native_libraries",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )
    op.add_column(
        "application_versions",
        sa.Column("apk_sha256_b64", sa.String(length=44), nullable=True),
    )
    op.drop_constraint(
        "uq_application_versions_app_code",
        "application_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_application_versions_app_code_apk",
        "application_versions",
        ["application_id", "version_code", "apk_sha256_b64"],
    )
    op.create_unique_constraint(
        "uq_application_versions_id_application",
        "application_versions",
        ["id", "application_id"],
    )
    op.create_index(
        "uq_application_versions_legacy_app_code",
        "application_versions",
        ["application_id", "version_code"],
        unique=True,
        postgresql_where=sa.text("apk_sha256_b64 IS NULL"),
    )
    op.create_check_constraint(
        "ck_application_versions_target_api",
        "application_versions",
        "target_api_level IS NULL OR target_api_level > 0",
    )

    op.add_column(
        "scenario_recipes",
        sa.Column(
            "application_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_scenario_recipes_app_version_application",
        "scenario_recipes",
        "application_versions",
        ["application_version_id", "application_id"],
        ["id", "application_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_scenario_recipes_app_version_application",
        "scenario_recipes",
        ["application_version_id", "application_id"],
    )
    op.drop_constraint(
        "uq_scenario_recipes_app_type_version",
        "scenario_recipes",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_scenario_recipes_app_version_type_version",
        "scenario_recipes",
        ["application_version_id", "scenario_type", "recipe_version"],
    )
    op.create_index(
        "uq_scenario_recipes_legacy_app_type_version",
        "scenario_recipes",
        ["application_id", "scenario_type", "recipe_version"],
        unique=True,
        postgresql_where=sa.text("application_version_id IS NULL"),
    )

    op.create_unique_constraint(
        "uq_scenario_results_id_analysis",
        "scenario_results",
        ["id", "analysis_id"],
    )
    op.add_column(
        "scenario_results",
        sa.Column(
            "scenario_recipe_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "scenario_results",
        sa.Column("recipe_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scenario_results",
        sa.Column("recipe_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "scenario_results",
        sa.Column(
            "recipe_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_scenario_results_scenario_recipe_id_scenario_recipes",
        "scenario_results",
        "scenario_recipes",
        ["scenario_recipe_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_scenario_results_scenario_recipe_id",
        "scenario_results",
        ["scenario_recipe_id"],
    )
    op.create_check_constraint(
        "ck_scenario_results_recipe_snapshot",
        "scenario_results",
        "(scenario_recipe_id IS NULL AND recipe_version IS NULL "
        "AND recipe_hash IS NULL AND recipe_snapshot IS NULL) OR "
        "(scenario_recipe_id IS NOT NULL AND recipe_version IS NOT NULL "
        "AND recipe_version > 0 AND recipe_hash IS NOT NULL "
        "AND recipe_snapshot IS NOT NULL)",
    )
    op.add_column(
        "scenario_results",
        sa.Column("device_group_reason", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_scenario_results_device_group_reason",
        "scenario_results",
        "device_group_reason IS NULL OR device_group_reason IN "
        "('not_applicable', 'not_provided', 'device_unavailable', "
        "'canceled_before_assignment')",
    )
    op.create_check_constraint(
        "ck_scenario_results_device_group_exclusive",
        "scenario_results",
        "device_group_id IS NULL OR device_group_reason IS NULL",
    )

    op.alter_column(
        "analyses",
        "application_version_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_analyses_application_version_ready",
        "analyses",
        "application_version_id IS NOT NULL OR state IN "
        "('creating', 'created', 'uploading', 'failed', 'canceled', 'deleted')",
    )
    op.add_column(
        "analyses",
        sa.Column(
            "apk_inspection_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "analyses",
        sa.Column("apk_inspection_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_analyses_apk_inspection_claim",
        "analyses",
        "(apk_inspection_token IS NULL AND apk_inspection_claimed_at IS NULL) OR "
        "(apk_inspection_token IS NOT NULL AND apk_inspection_claimed_at IS NOT NULL)",
    )
    op.create_index(
        "ix_analyses_apk_inspection_claimed",
        "analyses",
        ["apk_inspection_claimed_at"],
    )

    op.add_column(
        "report_versions",
        sa.Column(
            "scenario_result_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_report_versions_scenario_analysis",
        "report_versions",
        "scenario_results",
        ["scenario_result_id", "analysis_id"],
        ["id", "analysis_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_report_versions_scenario_analysis",
        "report_versions",
        ["scenario_result_id", "analysis_id"],
    )
    op.drop_constraint(
        "uq_report_versions_analysis_version",
        "report_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_report_versions_scenario_version",
        "report_versions",
        ["scenario_result_id", "report_version"],
    )
    op.create_index(
        "uq_report_versions_legacy_analysis_version",
        "report_versions",
        ["analysis_id", "report_version"],
        unique=True,
        postgresql_where=sa.text("scenario_result_id IS NULL"),
    )
    op.add_column(
        "report_versions",
        sa.Column("bundle", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "report_versions",
        sa.Column("bundle_sha256_b64", sa.String(length=44), nullable=True),
    )
    op.create_check_constraint(
        "ck_report_versions_bundle_metadata",
        "report_versions",
        "(bundle IS NULL AND bundle_sha256_b64 IS NULL) OR "
        "(bundle IS NOT NULL AND bundle_sha256_b64 IS NOT NULL "
        "AND scenario_result_id IS NOT NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    data_loss_row = connection.scalar(
        sa.text(
            "SELECT 1 FROM report_versions WHERE scenario_result_id IS NOT NULL "
            "OR bundle IS NOT NULL OR bundle_sha256_b64 IS NOT NULL "
            "UNION ALL "
            "SELECT 1 FROM scenario_results WHERE scenario_recipe_id IS NOT NULL "
            "OR recipe_version IS NOT NULL OR recipe_hash IS NOT NULL "
            "OR recipe_snapshot IS NOT NULL OR device_group_reason IS NOT NULL "
            "UNION ALL "
            "SELECT 1 FROM scenario_recipes WHERE application_version_id IS NOT NULL "
            "UNION ALL "
            "SELECT 1 FROM application_versions WHERE target_api_level IS NOT NULL "
            "OR launch_activity IS NOT NULL OR apk_sha256_b64 IS NOT NULL LIMIT 1"
        )
    )
    if data_loss_row is not None:
        raise RuntimeError(
            "analysis orchestration downgrade preflight failed: "
            "Task 7 tenant data must be exported before downgrade"
        )
    compatibility_checks = (
        (
            "SELECT 1 FROM analyses WHERE application_version_id IS NULL LIMIT 1",
            "application_version_id is still null",
        ),
        (
            "SELECT 1 FROM analyses WHERE apk_inspection_token IS NOT NULL "
            "OR apk_inspection_claimed_at IS NOT NULL LIMIT 1",
            "APK inspection claims are still active",
        ),
        (
            "SELECT 1 FROM application_versions WHERE version_name IS NULL LIMIT 1",
            "version_name is still null",
        ),
        (
            "SELECT 1 FROM report_versions GROUP BY analysis_id, report_version "
            "HAVING count(*) > 1 LIMIT 1",
            "report versions cannot satisfy the legacy uniqueness rule",
        ),
        (
            "SELECT 1 FROM application_versions GROUP BY application_id, version_code "
            "HAVING count(*) > 1 LIMIT 1",
            "application versions cannot satisfy the legacy uniqueness rule",
        ),
        (
            "SELECT 1 FROM scenario_recipes "
            "GROUP BY application_id, scenario_type, recipe_version "
            "HAVING count(*) > 1 LIMIT 1",
            "scenario recipes cannot satisfy the legacy uniqueness rule",
        ),
    )
    for query, detail in compatibility_checks:
        if connection.scalar(sa.text(query)) is not None:
            raise RuntimeError(f"analysis orchestration downgrade preflight failed: {detail}")
    op.drop_constraint(
        "ck_report_versions_bundle_metadata",
        "report_versions",
        type_="check",
    )
    op.drop_column("report_versions", "bundle_sha256_b64")
    op.drop_column("report_versions", "bundle")
    op.drop_index(
        "uq_report_versions_legacy_analysis_version",
        table_name="report_versions",
    )
    op.drop_constraint(
        "uq_report_versions_scenario_version",
        "report_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_report_versions_analysis_version",
        "report_versions",
        ["analysis_id", "report_version"],
    )
    op.drop_index(
        "ix_report_versions_scenario_analysis",
        table_name="report_versions",
    )
    op.drop_constraint(
        "fk_report_versions_scenario_analysis",
        "report_versions",
        type_="foreignkey",
    )
    op.drop_column("report_versions", "scenario_result_id")

    op.drop_constraint(
        "ck_scenario_results_recipe_snapshot",
        "scenario_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_scenario_results_device_group_exclusive",
        "scenario_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_scenario_results_device_group_reason",
        "scenario_results",
        type_="check",
    )
    op.drop_column("scenario_results", "device_group_reason")
    op.drop_constraint(
        "fk_scenario_results_scenario_recipe_id_scenario_recipes",
        "scenario_results",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_scenario_results_scenario_recipe_id",
        table_name="scenario_results",
    )
    op.drop_column("scenario_results", "recipe_snapshot")
    op.drop_column("scenario_results", "recipe_hash")
    op.drop_column("scenario_results", "recipe_version")
    op.drop_column("scenario_results", "scenario_recipe_id")
    op.drop_constraint(
        "uq_scenario_results_id_analysis",
        "scenario_results",
        type_="unique",
    )

    op.drop_index(
        "ix_analyses_apk_inspection_claimed",
        table_name="analyses",
    )
    op.drop_constraint(
        "ck_analyses_apk_inspection_claim",
        "analyses",
        type_="check",
    )
    op.drop_column("analyses", "apk_inspection_claimed_at")
    op.drop_column("analyses", "apk_inspection_token")
    op.drop_constraint(
        "ck_analyses_application_version_ready",
        "analyses",
        type_="check",
    )
    op.alter_column(
        "analyses",
        "application_version_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    op.drop_constraint(
        "ck_application_versions_target_api",
        "application_versions",
        type_="check",
    )
    op.drop_column("application_versions", "has_native_libraries")
    op.drop_column("application_versions", "launch_activity")
    op.drop_column("application_versions", "target_api_level")
    op.drop_constraint(
        "uq_application_versions_app_code_apk",
        "application_versions",
        type_="unique",
    )
    op.drop_index(
        "uq_application_versions_legacy_app_code",
        table_name="application_versions",
    )
    op.create_unique_constraint(
        "uq_application_versions_app_code",
        "application_versions",
        ["application_id", "version_code"],
    )
    op.drop_column("application_versions", "apk_sha256_b64")
    op.drop_index(
        "uq_scenario_recipes_legacy_app_type_version",
        table_name="scenario_recipes",
    )
    op.drop_constraint(
        "uq_scenario_recipes_app_version_type_version",
        "scenario_recipes",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_scenario_recipes_app_type_version",
        "scenario_recipes",
        ["application_id", "scenario_type", "recipe_version"],
    )
    op.drop_index(
        "ix_scenario_recipes_app_version_application",
        table_name="scenario_recipes",
    )
    op.drop_constraint(
        "fk_scenario_recipes_app_version_application",
        "scenario_recipes",
        type_="foreignkey",
    )
    op.drop_column("scenario_recipes", "application_version_id")
    op.drop_constraint(
        "uq_application_versions_id_application",
        "application_versions",
        type_="unique",
    )
    op.alter_column(
        "application_versions",
        "version_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_constraint(
        "uq_applications_package_name",
        "applications",
        type_="unique",
    )
    op.drop_column("applications", "package_name")
