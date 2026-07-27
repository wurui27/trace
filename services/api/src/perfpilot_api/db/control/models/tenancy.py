from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    ControlBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class Team(UUIDPrimaryKeyMixin, TimestampMixin, ControlBase):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'deleting', 'deleted')",
            name="ck_teams_state",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, ControlBase):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_memberships_team_user"),
        CheckConstraint(
            "role IN ('team_owner', 'team_member', 'team_viewer')",
            name="ck_memberships_role",
        ),
        Index("ix_memberships_user_id", "user_id"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)


class TenantResource(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "tenant_resources"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "resource_version",
            name="uq_tenant_resources_team_version",
        ),
        CheckConstraint("resource_version > 0", name="ck_tenant_resources_resource_version"),
        CheckConstraint("version > 0", name="ck_tenant_resources_version_positive"),
        CheckConstraint(
            "state IN ('requested', 'provisioning', 'active', 'cleanup_pending', 'migrating')",
            name="ck_tenant_resources_state",
        ),
        Index("ix_tenant_resources_team_state", "team_id", "state"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="requested", server_default="requested"
    )
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    database_secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bucket_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class IdempotencyKey(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("team_id", "key", name="uq_idempotency_keys_team_key"),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_idempotency_keys_state",
        ),
        CheckConstraint("version > 0", name="ck_idempotency_keys_version_positive"),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    response_resource_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantQuota(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    ControlBase,
):
    __tablename__ = "tenant_quotas"
    __table_args__ = (
        UniqueConstraint("team_id", name="uq_tenant_quotas_team_id"),
        CheckConstraint(
            "active_device_limit >= 0 AND queued_device_limit >= 0",
            name="ck_tenant_quotas_nonnegative",
        ),
        CheckConstraint("version > 0", name="ck_tenant_quotas_version_positive"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    active_device_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    queued_device_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=text("20")
    )
