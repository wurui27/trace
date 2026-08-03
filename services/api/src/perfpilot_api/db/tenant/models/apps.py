from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    TenantBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class Application(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("package_name", name="uq_applications_package_name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    package_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text)


class ApplicationVersion(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "application_versions"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "version_code",
            "apk_sha256_b64",
            name="uq_application_versions_app_code_apk",
        ),
        UniqueConstraint(
            "id",
            "application_id",
            name="uq_application_versions_id_application",
        ),
        CheckConstraint("version_code >= 0", name="ck_application_versions_version_code"),
        CheckConstraint(
            "min_api_level IS NULL OR min_api_level > 0",
            name="ck_application_versions_min_api",
        ),
        CheckConstraint(
            "target_api_level IS NULL OR target_api_level > 0",
            name="ck_application_versions_target_api",
        ),
        Index("ix_application_versions_application_id", "application_id"),
        Index(
            "uq_application_versions_legacy_app_code",
            "application_id",
            "version_code",
            unique=True,
            postgresql_where=text("apk_sha256_b64 IS NULL"),
        ),
    )

    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version_code: Mapped[int] = mapped_column(Integer, nullable=False)
    min_api_level: Mapped[int | None] = mapped_column(Integer)
    target_api_level: Mapped[int | None] = mapped_column(Integer)
    launch_activity: Mapped[str | None] = mapped_column(String(512))
    supported_abis: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    has_native_libraries: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    apk_sha256_b64: Mapped[str | None] = mapped_column(String(44))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))


class ScenarioRecipe(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "scenario_recipes"
    __table_args__ = (
        UniqueConstraint(
            "application_version_id",
            "scenario_type",
            "recipe_version",
            name="uq_scenario_recipes_app_version_type_version",
        ),
        ForeignKeyConstraint(
            ("application_version_id", "application_id"),
            ("application_versions.id", "application_versions.application_id"),
            name="fk_scenario_recipes_app_version_application",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_recipes_type",
        ),
        CheckConstraint("recipe_version > 0", name="ck_scenario_recipes_version"),
        Index("ix_scenario_recipes_application_id", "application_id"),
        Index(
            "ix_scenario_recipes_app_version_application",
            "application_version_id",
            "application_id",
        ),
        Index(
            "uq_scenario_recipes_legacy_app_type_version",
            "application_id",
            "scenario_type",
            "recipe_version",
            unique=True,
            postgresql_where=text("application_version_id IS NULL"),
        ),
    )

    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
    )
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)
    recipe_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Analysis(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    TenantBase,
):
    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "analysis_mode IN ('device', 'trace_upload', 'memory_upload')",
            name="ck_analyses_mode",
        ),
        CheckConstraint(
            "application_version_id IS NOT NULL OR state IN "
            "('creating', 'created', 'uploading', 'failed', 'canceled', 'deleted')",
            name="ck_analyses_application_version_ready",
        ),
        CheckConstraint(
            "state IN ('creating', 'created', 'uploading', 'queued', 'scheduled', 'running', "
            "'analyzing', 'completed', 'partially_completed', 'failed', 'canceled', 'deleted')",
            name="ck_analyses_state",
        ),
        CheckConstraint(
            "(apk_inspection_token IS NULL AND apk_inspection_claimed_at IS NULL) OR "
            "(apk_inspection_token IS NOT NULL AND apk_inspection_claimed_at IS NOT NULL)",
            name="ck_analyses_apk_inspection_claim",
        ),
        CheckConstraint("version > 0", name="ck_analyses_version_positive"),
        Index("ix_analyses_application_version_id", "application_version_id"),
        Index("ix_analyses_state_created", "state", "created_at"),
        Index("ix_analyses_apk_inspection_claimed", "apk_inspection_claimed_at"),
    )

    application_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("application_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    analysis_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    question: Mapped[str | None] = mapped_column(String(2000))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))
    apk_inspection_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    apk_inspection_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScenarioResult(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    TenantBase,
):
    __tablename__ = "scenario_results"
    __table_args__ = (
        UniqueConstraint("id", "analysis_id", name="uq_scenario_results_id_analysis"),
        UniqueConstraint("analysis_id", "scenario_type", name="uq_scenario_results_analysis_type"),
        CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_results_type",
        ),
        CheckConstraint(
            "state IN ('queued', 'scheduled', 'running', 'analyzing', 'completed', "
            "'failed', 'canceled')",
            name="ck_scenario_results_state",
        ),
        CheckConstraint("version > 0", name="ck_scenario_results_version_positive"),
        CheckConstraint(
            "(scenario_recipe_id IS NULL AND recipe_version IS NULL "
            "AND recipe_hash IS NULL AND recipe_snapshot IS NULL) OR "
            "(scenario_recipe_id IS NOT NULL AND recipe_version IS NOT NULL "
            "AND recipe_version > 0 AND recipe_hash IS NOT NULL "
            "AND recipe_snapshot IS NOT NULL)",
            name="ck_scenario_results_recipe_snapshot",
        ),
        CheckConstraint(
            "device_group_reason IS NULL OR device_group_reason IN "
            "('not_applicable', 'not_provided', 'device_unavailable', "
            "'canceled_before_assignment')",
            name="ck_scenario_results_device_group_reason",
        ),
        CheckConstraint(
            "device_group_id IS NULL OR device_group_reason IS NULL",
            name="ck_scenario_results_device_group_exclusive",
        ),
        Index("ix_scenario_results_state_created", "state", "created_at"),
        Index("ix_scenario_results_scenario_recipe_id", "scenario_recipe_id"),
    )

    analysis_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_recipe_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_recipes.id", ondelete="RESTRICT"),
    )
    recipe_version: Mapped[int | None] = mapped_column(Integer)
    recipe_hash: Mapped[str | None] = mapped_column(String(64))
    recipe_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    device_group_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    device_group_reason: Mapped[str | None] = mapped_column(String(64))
    validity: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))


class SampleAttempt(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    TenantBase,
):
    __tablename__ = "sample_attempts"
    __table_args__ = (
        UniqueConstraint("scenario_job_id", "attempt_no", name="uq_sample_attempts_job_attempt"),
        CheckConstraint(
            "state IN ('uploading', 'finalized', 'validating', 'valid', 'invalid', "
            "'validation_error')",
            name="ck_sample_attempts_state",
        ),
        CheckConstraint(
            "attempt_no > 0 AND attempt_no <= 10",
            name="ck_sample_attempts_attempt_range",
        ),
        CheckConstraint("version > 0", name="ck_sample_attempts_version_positive"),
        Index("ix_sample_attempts_state_created", "state", "created_at"),
    )

    scenario_job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    invalid_reason: Mapped[str | None] = mapped_column(String(96))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
