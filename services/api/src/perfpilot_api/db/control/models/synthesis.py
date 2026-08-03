from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    ControlBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


_TIMESTAMP_STATE_CHECK = "(" \
    "(state = 'pending' AND started_at IS NULL AND completed_at IS NULL) OR " \
    "(state = 'running' AND started_at IS NOT NULL AND completed_at IS NULL) OR " \
    "(state IN ('succeeded', 'failed') AND started_at IS NOT NULL " \
    "AND completed_at IS NOT NULL AND completed_at >= started_at)" \
")"
_TOKEN_TOTAL_CHECK = "(" \
    "prompt_tokens IS NULL OR completion_tokens IS NULL OR total_tokens IS NULL " \
    "OR total_tokens = prompt_tokens + completion_tokens" \
")"


class SynthesisExecution(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "synthesis_executions"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "source_execution_id",
            "generation",
            name="uq_synthesis_executions_source_generation",
        ),
        ForeignKeyConstraint(
            ("analysis_id", "team_id"),
            ("global_jobs.id", "global_jobs.team_id"),
            name="fk_synthesis_executions_analysis_team",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "tenant_resource_version > 0",
            name="ck_synthesis_executions_tenant_resource_version_positive",
        ),
        CheckConstraint("generation > 0", name="ck_synthesis_executions_generation_positive"),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 2",
            name="ck_synthesis_executions_attempt_count_range",
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_synthesis_executions_state",
        ),
        CheckConstraint(_TIMESTAMP_STATE_CHECK, name="ck_synthesis_executions_timestamps"),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_synthesis_executions_request_fingerprint",
        ),
        CheckConstraint(
            "report_worker_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_synthesis_executions_worker_image_digest",
        ),
        CheckConstraint(
            "stable_error_code IS NULL OR stable_error_code ~ '^[a-z][a-z0-9_]{0,95}$'",
            name="ck_synthesis_executions_stable_error_code",
        ),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_synthesis_executions_prompt_tokens_nonnegative",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_synthesis_executions_completion_tokens_nonnegative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_synthesis_executions_total_tokens_nonnegative",
        ),
        CheckConstraint(_TOKEN_TOTAL_CHECK, name="ck_synthesis_executions_token_total"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_synthesis_executions_latency_nonnegative",
        ),
        CheckConstraint("version > 0", name="ck_synthesis_executions_version_positive"),
        Index("ix_synthesis_executions_analysis_team", "analysis_id", "team_id"),
        Index("ix_synthesis_executions_source_execution_id", "source_execution_id"),
        Index("ix_synthesis_executions_state_created", "state", "created_at"),
    )

    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    source_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("engine_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    report_worker_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    projection_sha256_b64: Mapped[str] = mapped_column(String(44), nullable=False)
    projection_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    provider_protocol: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_template_sha256_b64: Mapped[str] = mapped_column(String(44), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    stable_error_code: Mapped[str | None] = mapped_column(String(96))
    candidate_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    candidate_sha256_b64: Mapped[str | None] = mapped_column(String(44))
    report_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_version_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIInvocation(UUIDPrimaryKeyMixin, TimestampMixin, ControlBase):
    __tablename__ = "ai_invocations"
    __table_args__ = (
        UniqueConstraint(
            "synthesis_execution_id",
            "attempt_number",
            name="uq_ai_invocations_execution_attempt",
        ),
        ForeignKeyConstraint(
            ("analysis_id", "team_id"),
            ("global_jobs.id", "global_jobs.team_id"),
            name="fk_ai_invocations_analysis_team",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "attempt_number IN (1, 2)",
            name="ck_ai_invocations_attempt_number",
        ),
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_ai_invocations_state",
        ),
        CheckConstraint(_TIMESTAMP_STATE_CHECK, name="ck_ai_invocations_timestamps"),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_ai_invocations_request_fingerprint",
        ),
        CheckConstraint(
            "stable_error_code IS NULL OR stable_error_code ~ '^[a-z][a-z0-9_]{0,95}$'",
            name="ck_ai_invocations_stable_error_code",
        ),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_ai_invocations_prompt_tokens_nonnegative",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_ai_invocations_completion_tokens_nonnegative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_ai_invocations_total_tokens_nonnegative",
        ),
        CheckConstraint(_TOKEN_TOTAL_CHECK, name="ck_ai_invocations_token_total"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_ai_invocations_latency_nonnegative",
        ),
        Index("ix_ai_invocations_analysis_team", "analysis_id", "team_id"),
        Index("ix_ai_invocations_synthesis_execution_id", "synthesis_execution_id"),
    )

    synthesis_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("synthesis_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_protocol: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    stable_error_code: Mapped[str | None] = mapped_column(String(96))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
