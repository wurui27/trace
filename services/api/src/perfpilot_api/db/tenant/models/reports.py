from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import TenantBase, TimestampMixin, UUIDPrimaryKeyMixin


class ReportVersion(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "report_versions"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "report_version", name="uq_report_versions_analysis_version"
        ),
        CheckConstraint("report_version > 0", name="ck_report_versions_version"),
        CheckConstraint(
            "state IN ('complete', 'partial', 'failed')",
            name="ck_report_versions_state",
        ),
        Index("ix_report_versions_source_artifact_id", "source_artifact_id"),
    )

    analysis_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_artifact_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="SET NULL"),
    )
    provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class Metric(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "metrics"
    __table_args__ = (
        UniqueConstraint(
            "report_version_id",
            "scenario_result_id",
            "metric_name",
            name="uq_metrics_report_scenario_name",
        ),
        Index("ix_metrics_scenario_result_id", "scenario_result_id"),
    )

    report_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("report_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    scenario_result_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64))
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    value: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class Finding(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint(
            "report_version_id",
            "scenario_result_id",
            "stable_code",
            name="uq_findings_report_scenario_code",
        ),
        CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')",
            name="ck_findings_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_findings_status",
        ),
        Index("ix_findings_scenario_result_id", "scenario_result_id"),
    )

    report_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("report_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    scenario_result_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("scenario_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    stable_code: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class Evidence(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(artifact_id, metric_id) <= 1",
            name="ck_evidence_at_most_one_reference",
        ),
        Index("ix_evidence_finding_id", "finding_id"),
        Index("ix_evidence_artifact_id", "artifact_id"),
        Index("ix_evidence_metric_id", "metric_id"),
    )

    finding_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="SET NULL"),
    )
    metric_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("metrics.id", ondelete="SET NULL"),
    )
    evidence_type: Mapped[str] = mapped_column(String(96), nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class Recommendation(UUIDPrimaryKeyMixin, TimestampMixin, TenantBase):
    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint(
            "finding_id", "rank", name="uq_recommendations_finding_rank"
        ),
        CheckConstraint("rank > 0", name="ck_recommendations_rank_positive"),
    )

    finding_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
