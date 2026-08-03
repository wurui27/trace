"""Persist non-secret AI synthesis execution audit metadata.

Revision ID: 0008_ai_synthesis
Revises: 0007_trace_worker_claims
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_ai_synthesis"
down_revision: str | None = "0007_trace_worker_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def _record_columns(*, versioned: bool = False) -> tuple[sa.Column[object], ...]:
    columns: list[sa.Column[object]] = [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]
    if versioned:
        columns.insert(
            1,
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
    return tuple(columns)


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("subject_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_outbox_events_subject_version_positive",
        "outbox_events",
        "subject_version IS NULL OR subject_version > 0",
    )
    op.create_index(
        "uq_outbox_events_engine_result_ready_subject",
        "outbox_events",
        ["subject_id"],
        unique=True,
        postgresql_where=sa.text(
            "event_type = 'engine_result_ready' AND subject_type = 'engine_execution'"
        ),
    )
    op.create_index(
        "uq_outbox_events_analysis_synthesis_requested_subject",
        "outbox_events",
        ["subject_id"],
        unique=True,
        postgresql_where=sa.text(
            "event_type = 'analysis_synthesis_requested' AND subject_type = 'synthesis_execution'"
        ),
    )

    op.create_table(
        "synthesis_executions",
        *_record_columns(versioned=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_resource_version", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("normalizer_version", sa.String(length=128), nullable=False),
        sa.Column("report_worker_image_digest", sa.String(length=71), nullable=False),
        sa.Column("projection_sha256_b64", sa.String(length=44), nullable=False),
        sa.Column("projection_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_protocol", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=False),
        sa.Column("provider_model", sa.String(length=255), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=128), nullable=False),
        sa.Column("prompt_template_sha256_b64", sa.String(length=44), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("stable_error_code", sa.String(length=96), nullable=True),
        sa.Column("candidate_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_sha256_b64", sa.String(length=44), nullable=True),
        sa.Column("report_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "tenant_resource_version > 0",
            name="ck_synthesis_executions_tenant_resource_version_positive",
        ),
        sa.CheckConstraint("generation > 0", name="ck_synthesis_executions_generation_positive"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 2",
            name="ck_synthesis_executions_attempt_count_range",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_synthesis_executions_state",
        ),
        sa.CheckConstraint(_TIMESTAMP_STATE_CHECK, name="ck_synthesis_executions_timestamps"),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_synthesis_executions_request_fingerprint",
        ),
        sa.CheckConstraint(
            "report_worker_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_synthesis_executions_worker_image_digest",
        ),
        sa.CheckConstraint(
            "stable_error_code IS NULL OR stable_error_code ~ '^[a-z][a-z0-9_]{0,95}$'",
            name="ck_synthesis_executions_stable_error_code",
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_synthesis_executions_prompt_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_synthesis_executions_completion_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_synthesis_executions_total_tokens_nonnegative",
        ),
        sa.CheckConstraint(_TOKEN_TOTAL_CHECK, name="ck_synthesis_executions_token_total"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_synthesis_executions_latency_nonnegative",
        ),
        sa.CheckConstraint("version > 0", name="ck_synthesis_executions_version_positive"),
        sa.ForeignKeyConstraint(
            ["analysis_id", "team_id"],
            ["global_jobs.id", "global_jobs.team_id"],
            name="fk_synthesis_executions_analysis_team",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id"],
            ["engine_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_synthesis_executions"),
        sa.UniqueConstraint(
            "analysis_id",
            "source_execution_id",
            "generation",
            name="uq_synthesis_executions_source_generation",
        ),
    )
    op.create_index(
        "ix_synthesis_executions_analysis_team",
        "synthesis_executions",
        ["analysis_id", "team_id"],
    )
    op.create_index(
        "ix_synthesis_executions_source_execution_id",
        "synthesis_executions",
        ["source_execution_id"],
    )
    op.create_index(
        "ix_synthesis_executions_state_created",
        "synthesis_executions",
        ["state", "created_at"],
    )

    op.create_table(
        "ai_invocations",
        *_record_columns(),
        sa.Column("synthesis_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_protocol", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=False),
        sa.Column("provider_model", sa.String(length=255), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("stable_error_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_number IN (1, 2)",
            name="ck_ai_invocations_attempt_number",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_ai_invocations_state",
        ),
        sa.CheckConstraint(_TIMESTAMP_STATE_CHECK, name="ck_ai_invocations_timestamps"),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_ai_invocations_request_fingerprint",
        ),
        sa.CheckConstraint(
            "stable_error_code IS NULL OR stable_error_code ~ '^[a-z][a-z0-9_]{0,95}$'",
            name="ck_ai_invocations_stable_error_code",
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_ai_invocations_prompt_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_ai_invocations_completion_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_ai_invocations_total_tokens_nonnegative",
        ),
        sa.CheckConstraint(_TOKEN_TOTAL_CHECK, name="ck_ai_invocations_token_total"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_ai_invocations_latency_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id", "team_id"],
            ["global_jobs.id", "global_jobs.team_id"],
            name="fk_ai_invocations_analysis_team",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["synthesis_execution_id"],
            ["synthesis_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_invocations"),
        sa.UniqueConstraint(
            "synthesis_execution_id",
            "attempt_number",
            name="uq_ai_invocations_execution_attempt",
        ),
    )
    op.create_index(
        "ix_ai_invocations_analysis_team",
        "ai_invocations",
        ["analysis_id", "team_id"],
    )
    op.create_index(
        "ix_ai_invocations_synthesis_execution_id",
        "ai_invocations",
        ["synthesis_execution_id"],
    )


def downgrade() -> None:
    op.drop_table("ai_invocations")
    op.drop_table("synthesis_executions")
    op.drop_index(
        "uq_outbox_events_analysis_synthesis_requested_subject",
        table_name="outbox_events",
    )
    op.drop_index("uq_outbox_events_engine_result_ready_subject", table_name="outbox_events")
    op.drop_constraint(
        "ck_outbox_events_subject_version_positive",
        "outbox_events",
        type_="check",
    )
    op.drop_column("outbox_events", "subject_version")
