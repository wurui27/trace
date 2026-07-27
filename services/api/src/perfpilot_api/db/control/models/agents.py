from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    ControlBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class Agent(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("name", name="uq_agents_name"),
        CheckConstraint(
            "state IN ('pending', 'online', 'offline', 'revoked')",
            name="ck_agents_state",
        ),
        CheckConstraint("token_version > 0", name="ck_agents_token_version_positive"),
        CheckConstraint("version > 0", name="ck_agents_version_positive"),
        Index("ix_agents_state_last_heartbeat", "state", "last_heartbeat_at"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    registration_code_digest: Mapped[str | None] = mapped_column(String(64))
    registration_code_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    registration_code_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    token_digest: Mapped[str | None] = mapped_column(String(64))
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capabilities: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class Device(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("serial", name="uq_devices_serial"),
        CheckConstraint(
            "state IN ('healthy', 'busy', 'quarantined', 'offline')",
            name="ck_devices_state",
        ),
        CheckConstraint("api_level IS NULL OR api_level > 0", name="ck_devices_api_level"),
        CheckConstraint(
            "storage_available_bytes IS NULL OR storage_available_bytes >= 0",
            name="ck_devices_storage_nonnegative",
        ),
        CheckConstraint("version > 0", name="ck_devices_version_positive"),
        Index("ix_devices_agent_id", "agent_id"),
        Index("ix_devices_state_last_seen", "state", "last_seen_at"),
    )

    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    serial: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="offline", server_default="offline"
    )
    api_level: Mapped[int | None] = mapped_column(Integer)
    abi: Mapped[str | None] = mapped_column(String(64))
    build_fingerprint: Mapped[str | None] = mapped_column(String(512))
    display_modes: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    temperature_c: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    thermal_state: Mapped[str | None] = mapped_column(String(64))
    storage_available_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_rooted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_profileable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    perfetto_capabilities: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentLease(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "agent_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'released', 'expired', 'revoked')",
            name="ck_agent_leases_state",
        ),
        CheckConstraint("version > 0", name="ck_agent_leases_version_positive"),
        Index("ix_agent_leases_agent_id", "agent_id"),
        Index("ix_agent_leases_global_job_id", "global_job_id"),
        Index("ix_agent_leases_state_expires", "state", "expires_at"),
        Index(
            "uq_agent_leases_active_device",
            "device_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
    )

    device_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    global_job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("global_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    lease_token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
