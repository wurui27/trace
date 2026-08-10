from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import ControlBase, TimestampMixin, UUIDPrimaryKeyMixin, VersionedMixin


class SourceTask(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, ControlBase):
    """Metadata-only source lease; ``id`` is the protocol ``execution_id``."""

    __tablename__ = "source_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('source_context', 'patch_verification')",
            name="ck_source_tasks_type",
        ),
        CheckConstraint(
            "state IN ('queued', 'leased', 'running', 'cancel_requested', "
            "'completed', 'failed', 'canceled', 'expired')",
            name="ck_source_tasks_state",
        ),
        CheckConstraint("lease_version > 0", name="ck_source_tasks_lease_version_positive"),
        CheckConstraint("version > 0", name="ck_source_tasks_version_positive"),
        CheckConstraint(
            "lease_token_digest IS NULL OR lease_token_digest ~ '^[0-9a-f]{64}$'",
            name="ck_source_tasks_lease_token_digest",
        ),
        CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_tasks_request_sha256",
        ),
        CheckConstraint(
            "completion_sha256 IS NULL OR completion_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_tasks_completion_sha256",
        ),
        CheckConstraint(
            "(state = 'queued' AND lease_token_digest IS NULL AND expires_at IS NULL) OR "
            "(state <> 'queued' AND (lease_token_digest IS NOT NULL OR state = 'canceled'))",
            name="ck_source_tasks_lease_shape",
        ),
        CheckConstraint(
            "(task_type = 'source_context' AND NOT (request_document ? 'fix_id')) OR "
            "(task_type = 'patch_verification' AND "
            "(request_document ->> 'fix_id') ~ "
            "'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')",
            name="ck_source_tasks_request_kind",
        ),
        ForeignKeyConstraint(
            ["analysis_id", "team_id"],
            ["global_jobs.id", "global_jobs.team_id"],
            name="fk_source_tasks_analysis_team",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["agent_id", "team_id"],
            ["agents.id", "agents.team_id"],
            name="fk_source_tasks_agent_team",
            ondelete="RESTRICT",
        ),
        Index("ix_source_tasks_agent_state_created", "agent_id", "state", "created_at"),
        Index("ix_source_tasks_agent_team", "agent_id", "team_id"),
        Index("ix_source_tasks_analysis", "analysis_id"),
        Index("ix_source_tasks_analysis_team", "analysis_id", "team_id"),
        Index(
            "uq_source_tasks_active_agent",
            "agent_id",
            unique=True,
            postgresql_where=text(
                "state IN ('leased', 'running', 'cancel_requested')"
            ),
        ),
        Index(
            "uq_source_tasks_active_context",
            "analysis_id",
            unique=True,
            postgresql_where=text(
                "task_type = 'source_context' AND state IN "
                "('queued', 'leased', 'running', 'cancel_requested')"
            ),
        ),
        Index(
            "uq_source_tasks_active_patch_fix",
            "analysis_id",
            text("((request_document ->> 'fix_id'))"),
            unique=True,
            postgresql_where=text(
                "task_type = 'patch_verification' AND state IN "
                "('queued', 'leased', 'running', 'cancel_requested')"
            ),
        ),
    )

    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_document: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    completion_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    completion_sha256: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(96))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["SourceTask"]
