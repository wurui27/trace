from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class TeamEngineWorkspace(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "team_engine_workspaces"
    __table_args__ = (
        UniqueConstraint("team_id", "engine_id", name="uq_team_engine_workspaces_team_engine"),
        UniqueConstraint(
            "engine_id",
            "external_workspace_id",
            name="uq_team_engine_workspaces_external",
        ),
        CheckConstraint(
            "state IN ('provisioning', 'active', 'deleting', 'deleted', 'failed')",
            name="ck_team_engine_workspaces_state",
        ),
        CheckConstraint("version > 0", name="ck_team_engine_workspaces_version_positive"),
        ForeignKeyConstraint(
            ("team_id",),
            ("teams.id",),
            name="fk_team_engine_workspaces_team_id_teams",
            ondelete="CASCADE",
        ),
    )

    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_workspace_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class EngineExecution(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "engine_executions"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "engine_id",
            "attempt_number",
            name="uq_engine_executions_analysis_engine_attempt",
        ),
        ForeignKeyConstraint(
            ("analysis_id", "team_id"),
            ("global_jobs.id", "global_jobs.team_id"),
            name="fk_engine_executions_analysis_team",
            ondelete="CASCADE",
        ),
        CheckConstraint("attempt_number > 0", name="ck_engine_executions_attempt_positive"),
        CheckConstraint(
            "tenant_resource_version > 0",
            name="ck_engine_executions_tenant_resource_version_positive",
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'awaiting_user', 'completed', "
            "'insufficient_data', 'failed', 'canceled')",
            name="ck_engine_executions_state",
        ),
        CheckConstraint(
            "engine_commit_sha ~ '^[0-9a-f]{40}$'",
            name="ck_engine_executions_commit_sha",
        ),
        CheckConstraint(
            "engine_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_engine_executions_image_digest",
        ),
        CheckConstraint(
            "input_manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_engine_executions_input_manifest_hash",
        ),
        CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_engine_executions_config_hash",
        ),
        CheckConstraint("version > 0", name="ck_engine_executions_version_positive"),
        Index("ix_engine_executions_state_created", "state", "created_at"),
        Index("ix_engine_executions_team_analysis", "team_id", "analysis_id"),
    )

    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    engine_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    input_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_workspace_id: Mapped[str | None] = mapped_column(String(255))
    external_session_id: Mapped[str | None] = mapped_column(String(255))
    external_run_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    last_event_cursor: Mapped[str | None] = mapped_column(String(255))
    stable_error_code: Mapped[str | None] = mapped_column(String(96))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_result_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    normalized_report_version_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
