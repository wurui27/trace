from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    TenantBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class Artifact(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    TenantBase,
):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("upload_id", name="uq_artifacts_upload_id"),
        UniqueConstraint("object_key", "version_id", name="uq_artifacts_object_version"),
        CheckConstraint(
            "num_nonnulls(application_version_id, analysis_id, scenario_result_id, "
            "sample_attempt_id) = 1",
            name="ck_artifacts_exactly_one_owner",
        ),
        CheckConstraint(
            "state IN ('pending', 'finalized', 'expired', 'deleted')",
            name="ck_artifacts_state",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonnegative"),
        CheckConstraint("version > 0", name="ck_artifacts_version_positive"),
        Index("ix_artifacts_application_version_id", "application_version_id"),
        Index("ix_artifacts_analysis_id", "analysis_id"),
        Index("ix_artifacts_scenario_result_id", "scenario_result_id"),
        Index("ix_artifacts_sample_attempt_id", "sample_attempt_id"),
        Index("ix_artifacts_state_expires", "state", "expires_at"),
    )

    application_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("application_versions.id", ondelete="CASCADE"),
    )
    analysis_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("analyses.id", ondelete="CASCADE"),
    )
    scenario_result_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_results.id", ondelete="CASCADE"),
    )
    sample_attempt_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sample_attempts.id", ondelete="CASCADE"),
    )
    upload_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(96), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_b64: Mapped[str] = mapped_column(String(44), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(1024))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
