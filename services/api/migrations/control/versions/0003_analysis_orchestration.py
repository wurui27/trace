"""Persist opaque recipe bindings and scheduling requirements.

Revision ID: 0003_analysis_orchestration
Revises: 0002_tenant_provisioning_state
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_analysis_orchestration"
down_revision: str | None = "0002_tenant_provisioning_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM outbox_events "
                "WHERE event_type = 'analysis_queued' AND subject_type = 'analysis' "
                "GROUP BY subject_id HAVING count(*) > 1 LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "analysis orchestration upgrade preflight failed: "
            "duplicate analysis_queued events exist"
        )
    op.add_column(
        "global_jobs",
        sa.Column(
            "supported_abis",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "scenario_jobs",
        sa.Column(
            "supported_abis",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "scenario_jobs",
        sa.Column("scenario_recipe_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scenario_jobs",
        sa.Column("recipe_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scenario_jobs",
        sa.Column("recipe_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_scenario_jobs_recipe_binding",
        "scenario_jobs",
        "(scenario_recipe_id IS NULL AND recipe_version IS NULL AND recipe_hash IS NULL) OR "
        "(scenario_recipe_id IS NOT NULL AND recipe_version IS NOT NULL "
        "AND recipe_version > 0 AND recipe_hash IS NOT NULL)",
    )
    op.create_index(
        "uq_outbox_events_analysis_queued_subject",
        "outbox_events",
        ["subject_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'analysis_queued' AND subject_type = 'analysis'"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE global_jobs, outbox_events, scenario_jobs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM global_jobs WHERE cardinality(supported_abis) > 0 "
                "UNION ALL "
                "SELECT 1 FROM scenario_jobs WHERE cardinality(supported_abis) > 0 "
                "OR scenario_recipe_id IS NOT NULL OR recipe_version IS NOT NULL "
                "OR recipe_hash IS NOT NULL LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "analysis orchestration downgrade preflight failed: "
            "Task 7 scheduling data must be exported before downgrade"
        )
    op.drop_index(
        "uq_outbox_events_analysis_queued_subject",
        table_name="outbox_events",
    )
    op.drop_constraint(
        "ck_scenario_jobs_recipe_binding",
        "scenario_jobs",
        type_="check",
    )
    op.drop_column("scenario_jobs", "recipe_hash")
    op.drop_column("scenario_jobs", "recipe_version")
    op.drop_column("scenario_jobs", "scenario_recipe_id")
    op.drop_column("scenario_jobs", "supported_abis")
    op.drop_column("global_jobs", "supported_abis")
