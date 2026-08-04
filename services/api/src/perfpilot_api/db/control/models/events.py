from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    ControlBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class OutboxEvent(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("retry_count >= 0", name="ck_outbox_events_retry_count"),
        CheckConstraint(
            "subject_version IS NULL OR subject_version > 0",
            name="ck_outbox_events_subject_version_positive",
        ),
        CheckConstraint("version > 0", name="ck_outbox_events_version_positive"),
        Index("ix_outbox_events_team_id", "team_id"),
        Index("ix_outbox_events_global_job_id", "global_job_id"),
        Index("ix_outbox_events_scenario_job_id", "scenario_job_id"),
        Index(
            "ix_outbox_events_ready_unpublished",
            "ready_at",
            "created_at",
            postgresql_where=text("ready_at IS NOT NULL AND published_at IS NULL"),
        ),
        Index(
            "uq_outbox_events_engine_result_ready_subject",
            "subject_id",
            unique=True,
            postgresql_where=text(
                "event_type = 'engine_result_ready' "
                "AND subject_type = 'engine_execution'"
            ),
        ),
        Index(
            "uq_outbox_events_analysis_synthesis_requested_subject",
            "subject_id",
            unique=True,
            postgresql_where=text(
                "event_type = 'analysis_synthesis_requested' "
                "AND subject_type = 'synthesis_execution'"
            ),
        ),
        Index(
            "uq_outbox_events_analysis_queued_subject",
            "subject_id",
            unique=True,
            postgresql_where=text("event_type = 'analysis_queued' AND subject_type = 'analysis'"),
        ),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    global_job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("global_jobs.id", ondelete="CASCADE"),
    )
    scenario_job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_jobs.id", ondelete="CASCADE"),
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    subject_version: Mapped[int | None] = mapped_column(Integer)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class InboxEvent(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint("consumer_name", "event_id", name="uq_inbox_events_consumer_event"),
        CheckConstraint(
            "state IN ('received', 'processed')",
            name="ck_inbox_events_state",
        ),
        CheckConstraint("version > 0", name="ck_inbox_events_version_positive"),
        Index("ix_inbox_events_event_id", "event_id"),
        Index("ix_inbox_events_state_received", "state", "received_at"),
    )

    consumer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("outbox_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", server_default="received"
    )
    claim_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
