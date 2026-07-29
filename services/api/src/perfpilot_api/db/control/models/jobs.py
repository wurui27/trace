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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    ControlBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class GlobalJob(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "global_jobs"
    __table_args__ = (
        UniqueConstraint("team_id", "idempotency_key", name="uq_global_jobs_team_idempotency"),
        UniqueConstraint("id", "team_id", name="uq_global_jobs_id_team"),
        CheckConstraint(
            "analysis_mode IN ('device', 'trace_upload', 'memory_upload')",
            name="ck_global_jobs_analysis_mode",
        ),
        CheckConstraint(
            "state IN ('creating', 'created', 'uploading', 'queued', 'scheduled', "
            "'running', 'analyzing', 'completed', 'partially_completed', 'failed', 'canceled')",
            name="ck_global_jobs_state",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND valid_sample_count >= 0 AND invalid_sample_count >= 0 "
            "AND retry_count >= 0 AND valid_sample_count + invalid_sample_count <= attempt_count",
            name="ck_global_jobs_counts_nonnegative",
        ),
        CheckConstraint("version > 0", name="ck_global_jobs_version_positive"),
        Index("ix_global_jobs_team_state_created", "team_id", "state", "created_at"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    analysis_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    input_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    required_abi: Mapped[str | None] = mapped_column(String(64))
    supported_abis: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    min_api_level: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    valid_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    invalid_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    device_migration_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))


class ScenarioJob(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "scenario_jobs"
    __table_args__ = (
        UniqueConstraint("analysis_id", "scenario_type", name="uq_scenario_jobs_analysis_type"),
        CheckConstraint(
            "scenario_type IN ('cold_start', 'scroll', 'memory_cycle')",
            name="ck_scenario_jobs_type",
        ),
        CheckConstraint(
            "state IN ('queued', 'scheduled', 'running', 'analyzing', 'completed', "
            "'failed', 'canceled')",
            name="ck_scenario_jobs_state",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND valid_sample_count >= 0 AND invalid_sample_count >= 0 "
            "AND retry_count >= 0 AND max_attempts > 0 "
            "AND valid_sample_count + invalid_sample_count <= attempt_count "
            "AND attempt_count <= max_attempts",
            name="ck_scenario_jobs_sample_counts",
        ),
        CheckConstraint("version > 0", name="ck_scenario_jobs_version_positive"),
        CheckConstraint(
            "(scenario_recipe_id IS NULL AND recipe_version IS NULL AND recipe_hash IS NULL) OR "
            "(scenario_recipe_id IS NOT NULL AND recipe_version IS NOT NULL "
            "AND recipe_version > 0 AND recipe_hash IS NOT NULL)",
            name="ck_scenario_jobs_recipe_binding",
        ),
        Index("ix_scenario_jobs_state_created", "state", "created_at"),
    )

    analysis_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("global_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_recipe_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    recipe_version: Mapped[int | None] = mapped_column(Integer)
    recipe_hash: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    input_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    required_abi: Mapped[str | None] = mapped_column(String(64))
    supported_abis: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    min_api_level: Mapped[int | None] = mapped_column(Integer)
    device_group_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    valid_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    invalid_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default=text("10")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))


class SampleValidationClaim(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "sample_validation_claims"
    __table_args__ = (
        UniqueConstraint("sample_id", name="uq_sample_validation_claims_sample_id"),
        CheckConstraint(
            "state IN ('active', 'completed', 'expired', 'revoked')",
            name="ck_sample_validation_claims_state",
        ),
        CheckConstraint("retry_count >= 0", name="ck_sample_validation_claims_retry_count"),
        CheckConstraint("version > 0", name="ck_sample_validation_claims_version_positive"),
        Index("ix_sample_validation_claims_scenario_job_id", "scenario_job_id"),
        Index("ix_sample_validation_claims_state_expires", "state", "expires_at"),
    )

    scenario_job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sample_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    consumer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    verdict_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))


class WorkerClaim(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "worker_claims"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(global_job_id, scenario_job_id) = 1",
            name="ck_worker_claims_exactly_one_subject",
        ),
        CheckConstraint(
            "state IN ('active', 'completed', 'expired', 'revoked')",
            name="ck_worker_claims_state",
        ),
        CheckConstraint("retry_count >= 0", name="ck_worker_claims_retry_count"),
        CheckConstraint("version > 0", name="ck_worker_claims_version_positive"),
        Index("ix_worker_claims_global_job_id", "global_job_id"),
        Index("ix_worker_claims_scenario_job_id", "scenario_job_id"),
        Index("ix_worker_claims_state_expires", "state", "expires_at"),
    )

    global_job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("global_jobs.id", ondelete="CASCADE"),
    )
    scenario_job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_jobs.id", ondelete="CASCADE"),
    )
    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    consumer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    report_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
