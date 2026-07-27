from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class ApplicationVersion(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "application_versions"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "version_code", name="uq_application_versions_app_code"
        ),
        CheckConstraint("version_code >= 0", name="ck_application_versions_version_code"),
        CheckConstraint(
            "min_api_level IS NULL OR min_api_level > 0",
            name="ck_application_versions_min_api",
        ),
        Index("ix_application_versions_application_id", "application_id"),
    )

    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version_code: Mapped[int] = mapped_column(Integer, nullable=False)
    min_api_level: Mapped[int | None] = mapped_column(Integer)
    supported_abis: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))


class ScenarioRecipe(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "scenario_recipes"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "scenario_type",
            "recipe_version",
            name="uq_scenario_recipes_app_type_version",
        ),
        CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_recipes_type",
        ),
        CheckConstraint("recipe_version > 0", name="ck_scenario_recipes_version"),
        Index("ix_scenario_recipes_application_id", "application_id"),
    )

    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
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
            "analysis_mode IN ('device', 'trace_upload')",
            name="ck_analyses_mode",
        ),
        CheckConstraint(
            "state IN ('creating', 'created', 'uploading', 'queued', 'scheduled', 'running', "
            "'analyzing', 'completed', 'partially_completed', 'failed', 'canceled', 'deleted')",
            name="ck_analyses_state",
        ),
        CheckConstraint("version > 0", name="ck_analyses_version_positive"),
        Index("ix_analyses_application_version_id", "application_version_id"),
        Index("ix_analyses_state_created", "state", "created_at"),
    )

    application_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("application_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    analysis_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))


class ScenarioResult(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    TenantBase,
):
    __tablename__ = "scenario_results"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "scenario_type", name="uq_scenario_results_analysis_type"
        ),
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
        Index("ix_scenario_results_state_created", "state", "created_at"),
    )

    analysis_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    device_group_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
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
        UniqueConstraint(
            "scenario_job_id", "attempt_no", name="uq_sample_attempts_job_attempt"
        ),
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
