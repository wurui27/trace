from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
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
        UniqueConstraint("object_key", name="uq_artifacts_object_key"),
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
        CheckConstraint(
            "(idempotency_key IS NULL AND request_hash IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_hash IS NOT NULL)",
            name="ck_artifacts_idempotency_pair",
        ),
        CheckConstraint(
            "request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_artifacts_request_hash",
        ),
        CheckConstraint(
            "(state <> 'pending' OR (version_id IS NULL AND finalized_at IS NULL)) AND "
            "(state <> 'finalized' OR (version_id IS NOT NULL AND finalized_at IS NOT NULL))",
            name="ck_artifacts_state_metadata",
        ),
        CheckConstraint("version > 0", name="ck_artifacts_version_positive"),
        Index("ix_artifacts_application_version_id", "application_version_id"),
        Index("ix_artifacts_analysis_id", "analysis_id"),
        Index("ix_artifacts_scenario_result_id", "scenario_result_id"),
        Index("ix_artifacts_sample_attempt_id", "sample_attempt_id"),
        Index("ix_artifacts_state_expires", "state", "expires_at"),
        Index(
            "uq_artifacts_analysis_idempotency",
            "analysis_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("analysis_id IS NOT NULL AND idempotency_key IS NOT NULL"),
        ),
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
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    request_hash: Mapped[str | None] = mapped_column(String(64))
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


class ArtifactMultipartUpload(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    VersionedMixin,
    TenantBase,
):
    __tablename__ = "artifact_multipart_uploads"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_artifact_multipart_uploads_artifact"),
        UniqueConstraint(
            "storage_upload_id",
            name="uq_artifact_multipart_uploads_storage_upload",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'aborted', 'expired')",
            name="ck_artifact_multipart_uploads_state",
        ),
        CheckConstraint(
            "part_size_bytes > 0",
            name="ck_artifact_multipart_uploads_part_size_positive",
        ),
        CheckConstraint(
            "part_count >= 1 AND part_count <= 10000",
            name="ck_artifact_multipart_uploads_part_count_range",
        ),
        CheckConstraint(
            "jsonb_typeof(completed_parts) = 'array'",
            name="ck_artifact_multipart_uploads_completed_parts_array",
        ),
        CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL) OR "
            "(state <> 'completed' AND completed_at IS NULL)",
            name="ck_artifact_multipart_uploads_completion_state",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_artifact_multipart_uploads_version_positive",
        ),
        Index(
            "ix_artifact_multipart_uploads_execution_id",
            "execution_id",
        ),
        Index(
            "ix_artifact_multipart_uploads_state_expires",
            "state",
            "expires_at",
        ),
    )

    artifact_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    storage_upload_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    part_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_parts: Mapped[list[object]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
