from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
        UniqueConstraint("team_id", "name", name="uq_agents_team_name"),
        UniqueConstraint("id", "team_id", name="uq_agents_id_team"),
        CheckConstraint(
            "state IN ('pending', 'online', 'offline', 'revoked')",
            name="ck_agents_state",
        ),
        CheckConstraint(
            "platform IS NULL OR platform IN ('macos', 'windows', 'linux')",
            name="ck_agents_platform",
        ),
        CheckConstraint(
            "public_key_b64 IS NULL OR length(public_key_b64) = 44",
            name="ck_agents_public_key_length",
        ),
        CheckConstraint("token_version > 0", name="ck_agents_token_version_positive"),
        CheckConstraint("version > 0", name="ck_agents_version_positive"),
        Index("ix_agents_owner_user_id", "owner_user_id"),
        Index("ix_agents_team_state", "team_id", "state"),
        Index("ix_agents_state_last_heartbeat", "state", "last_heartbeat_at"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
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
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    refresh_token_digest: Mapped[str | None] = mapped_column(String(64))
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    public_key_b64: Mapped[str | None] = mapped_column(String(44))
    platform: Mapped[str | None] = mapped_column(String(32))
    agent_version: Mapped[str | None] = mapped_column(String(64))
    hostname: Mapped[str | None] = mapped_column(String(200))
    os_version: Mapped[str | None] = mapped_column(String(128))
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
        UniqueConstraint("serial_digest", name="uq_devices_serial_digest"),
        UniqueConstraint("id", "team_id", name="uq_devices_id_team"),
        ForeignKeyConstraint(
            ["agent_id", "team_id"],
            ["agents.id", "agents.team_id"],
            name="fk_devices_agent_team",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('ready', 'busy', 'unauthorized', 'booting', "
            "'quarantined', 'offline')",
            name="ck_devices_state",
        ),
        CheckConstraint(
            "connection_type IN ('usb', 'wifi', 'unknown')",
            name="ck_devices_connection_type",
        ),
        CheckConstraint(
            "adb_state IN ('device', 'unauthorized', 'offline', 'booting')",
            name="ck_devices_adb_state",
        ),
        CheckConstraint(
            "serial_digest ~ '^[0-9a-f]{64}$'",
            name="ck_devices_serial_digest",
        ),
        CheckConstraint(
            "serial_suffix ~ '^[!-~]{1,4}$'",
            name="ck_devices_serial_suffix",
        ),
        CheckConstraint("api_level IS NULL OR api_level > 0", name="ck_devices_api_level"),
        CheckConstraint(
            "battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100",
            name="ck_devices_battery_percent",
        ),
        CheckConstraint(
            "last_property_error_code IS NULL OR "
            "last_property_error_code ~ '^[a-z][a-z0-9_]{0,95}$'",
            name="ck_devices_property_error_code",
        ),
        CheckConstraint(
            "storage_available_bytes IS NULL OR storage_available_bytes >= 0",
            name="ck_devices_storage_nonnegative",
        ),
        CheckConstraint("version > 0", name="ck_devices_version_positive"),
        Index("ix_devices_agent_team", "agent_id", "team_id"),
        Index("ix_devices_team_state", "team_id", "state"),
        Index("ix_devices_agent_id", "agent_id"),
        Index("ix_devices_state_last_seen", "state", "last_seen_at"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    serial_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    serial_suffix: Mapped[str] = mapped_column(String(4), nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128))
    android_release: Mapped[str | None] = mapped_column(String(64))
    connection_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    adb_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="offline", server_default="offline"
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="offline", server_default="offline"
    )
    api_level: Mapped[int | None] = mapped_column(Integer)
    battery_percent: Mapped[int | None] = mapped_column(Integer)
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
    last_property_error_code: Mapped[str | None] = mapped_column(String(96))
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
            "state IN ('active', 'cancel_requested', 'released', 'expired', 'revoked')",
            name="ck_agent_leases_state",
        ),
        CheckConstraint(
            "task_snapshot_digest IS NULL OR "
            "task_snapshot_digest ~ '^[0-9a-f]{64}$'",
            name="ck_agent_leases_task_snapshot_digest",
        ),
        CheckConstraint("version > 0", name="ck_agent_leases_version_positive"),
        UniqueConstraint("execution_id", name="uq_agent_leases_execution_id"),
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
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    lease_token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    task_snapshot_digest: Mapped[str | None] = mapped_column(String(64))
