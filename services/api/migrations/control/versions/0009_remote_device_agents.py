"""Add team-scoped remote Agents, sanitized devices, and task leases.

Revision ID: 0009_remote_device_agents
Revises: 0008_ai_synthesis
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_remote_device_agents"
down_revision: str | None = "0008_ai_synthesis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_agent_scheduling_tables() -> None:
    op.get_bind().execute(
        sa.text(
            "LOCK TABLE agents, devices, agent_leases, global_jobs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )


def upgrade() -> None:
    _lock_agent_scheduling_tables()
    connection = op.get_bind()
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM agents "
                "UNION ALL SELECT 1 FROM devices LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "remote Agent migration preflight failed: export and remove legacy "
            "Agent/device identity rows before upgrading"
        )
    op.add_column(
        "agents",
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column(
        "agents",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column("agents", sa.Column("public_key_b64", sa.String(length=44)))
    op.add_column("agents", sa.Column("platform", sa.String(length=32)))
    op.add_column("agents", sa.Column("agent_version", sa.String(length=64)))
    op.add_column("agents", sa.Column("hostname", sa.String(length=200)))
    op.add_column("agents", sa.Column("os_version", sa.String(length=128)))
    op.add_column(
        "agents",
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "agents",
        sa.Column("refresh_token_digest", sa.String(length=64)),
    )
    op.add_column(
        "agents",
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True)),
    )
    op.drop_constraint("uq_agents_name", "agents", type_="unique")
    op.create_unique_constraint("uq_agents_team_name", "agents", ["team_id", "name"])
    op.create_unique_constraint("uq_agents_id_team", "agents", ["id", "team_id"])
    op.create_foreign_key(
        "fk_agents_team_id_teams",
        "agents",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_agents_owner_user_id_users",
        "agents",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_agents_platform",
        "agents",
        "platform IS NULL OR platform IN ('macos', 'windows', 'linux')",
    )
    op.create_check_constraint(
        "ck_agents_public_key_length",
        "agents",
        "public_key_b64 IS NULL OR length(public_key_b64) = 44",
    )
    op.create_index("ix_agents_owner_user_id", "agents", ["owner_user_id"])
    op.create_index("ix_agents_team_state", "agents", ["team_id", "state"])

    op.drop_constraint("fk_devices_agent_id_agents", "devices", type_="foreignkey")
    op.drop_constraint("uq_devices_serial", "devices", type_="unique")
    op.drop_constraint("ck_devices_state", "devices", type_="check")
    op.add_column(
        "devices",
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column(
        "devices", sa.Column("serial_digest", sa.String(length=64), nullable=False)
    )
    op.add_column(
        "devices", sa.Column("serial_suffix", sa.String(length=4), nullable=False)
    )
    op.add_column("devices", sa.Column("manufacturer", sa.String(length=128)))
    op.add_column("devices", sa.Column("model", sa.String(length=128)))
    op.add_column("devices", sa.Column("android_release", sa.String(length=64)))
    op.add_column(
        "devices",
        sa.Column(
            "connection_type",
            sa.String(length=16),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "adb_state",
            sa.String(length=32),
            server_default="offline",
            nullable=False,
        ),
    )
    op.add_column("devices", sa.Column("battery_percent", sa.Integer()))
    op.add_column(
        "devices",
        sa.Column("last_property_error_code", sa.String(length=96)),
    )
    op.drop_column("devices", "serial")
    op.create_unique_constraint(
        "uq_devices_serial_digest", "devices", ["serial_digest"]
    )
    op.create_unique_constraint("uq_devices_id_team", "devices", ["id", "team_id"])
    op.create_foreign_key(
        "fk_devices_team_id_teams",
        "devices",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_devices_agent_team",
        "devices",
        "agents",
        ["agent_id", "team_id"],
        ["id", "team_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_devices_state",
        "devices",
        "state IN ('ready', 'busy', 'unauthorized', 'booting', "
        "'quarantined', 'offline')",
    )
    op.create_check_constraint(
        "ck_devices_connection_type",
        "devices",
        "connection_type IN ('usb', 'wifi', 'unknown')",
    )
    op.create_check_constraint(
        "ck_devices_adb_state",
        "devices",
        "adb_state IN ('device', 'unauthorized', 'offline', 'booting')",
    )
    op.create_check_constraint(
        "ck_devices_serial_digest",
        "devices",
        "serial_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_devices_serial_suffix",
        "devices",
        "serial_suffix ~ '^[!-~]{1,4}$'",
    )
    op.create_check_constraint(
        "ck_devices_battery_percent",
        "devices",
        "battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_devices_property_error_code",
        "devices",
        "last_property_error_code IS NULL OR "
        "last_property_error_code ~ '^[a-z][a-z0-9_]{0,95}$'",
    )
    op.create_index(
        "ix_devices_agent_team", "devices", ["agent_id", "team_id"]
    )
    op.create_index("ix_devices_team_state", "devices", ["team_id", "state"])

    op.add_column(
        "global_jobs",
        sa.Column("selected_device_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_check_constraint(
        "ck_global_jobs_device_selection",
        "global_jobs",
        "selected_device_id IS NULL OR analysis_mode = 'device'",
    )
    op.create_foreign_key(
        "fk_global_jobs_selected_device_team",
        "global_jobs",
        "devices",
        ["selected_device_id", "team_id"],
        ["id", "team_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_global_jobs_selected_device_team",
        "global_jobs",
        ["selected_device_id", "team_id"],
    )

    op.drop_constraint("ck_agent_leases_state", "agent_leases", type_="check")
    op.add_column(
        "agent_leases",
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column(
        "agent_leases",
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "agent_leases",
        sa.Column("cancel_acknowledged_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "agent_leases",
        sa.Column("task_snapshot_digest", sa.String(length=64)),
    )
    op.create_unique_constraint(
        "uq_agent_leases_execution_id", "agent_leases", ["execution_id"]
    )
    op.create_check_constraint(
        "ck_agent_leases_state",
        "agent_leases",
        "state IN ('active', 'cancel_requested', 'released', 'expired', 'revoked')",
    )
    op.create_check_constraint(
        "ck_agent_leases_task_snapshot_digest",
        "agent_leases",
        "task_snapshot_digest IS NULL OR "
        "task_snapshot_digest ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    _lock_agent_scheduling_tables()
    connection = op.get_bind()
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM agents "
                "UNION ALL SELECT 1 FROM devices "
                "UNION ALL SELECT 1 FROM agent_leases "
                "UNION ALL SELECT 1 FROM global_jobs "
                "WHERE selected_device_id IS NOT NULL LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "remote Agent migration preflight failed: remote Agent state must be "
            "exported before downgrade"
        )

    op.drop_constraint(
        "ck_agent_leases_task_snapshot_digest", "agent_leases", type_="check"
    )
    op.drop_constraint("ck_agent_leases_state", "agent_leases", type_="check")
    op.drop_constraint(
        "uq_agent_leases_execution_id", "agent_leases", type_="unique"
    )
    op.drop_column("agent_leases", "task_snapshot_digest")
    op.drop_column("agent_leases", "cancel_acknowledged_at")
    op.drop_column("agent_leases", "renewed_at")
    op.drop_column("agent_leases", "execution_id")
    op.create_check_constraint(
        "ck_agent_leases_state",
        "agent_leases",
        "state IN ('active', 'released', 'expired', 'revoked')",
    )

    op.drop_index("ix_global_jobs_selected_device_team", table_name="global_jobs")
    op.drop_constraint(
        "fk_global_jobs_selected_device_team", "global_jobs", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_global_jobs_device_selection", "global_jobs", type_="check"
    )
    op.drop_column("global_jobs", "selected_device_id")

    op.drop_index("ix_devices_team_state", table_name="devices")
    op.drop_index("ix_devices_agent_team", table_name="devices")
    op.drop_constraint("ck_devices_property_error_code", "devices", type_="check")
    op.drop_constraint("ck_devices_battery_percent", "devices", type_="check")
    op.drop_constraint("ck_devices_serial_suffix", "devices", type_="check")
    op.drop_constraint("ck_devices_serial_digest", "devices", type_="check")
    op.drop_constraint("ck_devices_adb_state", "devices", type_="check")
    op.drop_constraint("ck_devices_connection_type", "devices", type_="check")
    op.drop_constraint("ck_devices_state", "devices", type_="check")
    op.drop_constraint("fk_devices_agent_team", "devices", type_="foreignkey")
    op.drop_constraint("fk_devices_team_id_teams", "devices", type_="foreignkey")
    op.drop_constraint("uq_devices_id_team", "devices", type_="unique")
    op.drop_constraint("uq_devices_serial_digest", "devices", type_="unique")
    op.add_column(
        "devices", sa.Column("serial", sa.String(length=255), nullable=False)
    )
    op.drop_column("devices", "last_property_error_code")
    op.drop_column("devices", "battery_percent")
    op.drop_column("devices", "adb_state")
    op.drop_column("devices", "connection_type")
    op.drop_column("devices", "android_release")
    op.drop_column("devices", "model")
    op.drop_column("devices", "manufacturer")
    op.drop_column("devices", "serial_suffix")
    op.drop_column("devices", "serial_digest")
    op.drop_column("devices", "team_id")
    op.create_foreign_key(
        "fk_devices_agent_id_agents",
        "devices",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("uq_devices_serial", "devices", ["serial"])
    op.create_check_constraint(
        "ck_devices_state",
        "devices",
        "state IN ('healthy', 'busy', 'quarantined', 'offline')",
    )

    op.drop_index("ix_agents_team_state", table_name="agents")
    op.drop_index("ix_agents_owner_user_id", table_name="agents")
    op.drop_constraint("ck_agents_public_key_length", "agents", type_="check")
    op.drop_constraint("ck_agents_platform", "agents", type_="check")
    op.drop_constraint("fk_agents_owner_user_id_users", "agents", type_="foreignkey")
    op.drop_constraint("fk_agents_team_id_teams", "agents", type_="foreignkey")
    op.drop_constraint("uq_agents_id_team", "agents", type_="unique")
    op.drop_constraint("uq_agents_team_name", "agents", type_="unique")
    op.drop_column("agents", "refresh_token_expires_at")
    op.drop_column("agents", "refresh_token_digest")
    op.drop_column("agents", "access_token_expires_at")
    op.drop_column("agents", "os_version")
    op.drop_column("agents", "hostname")
    op.drop_column("agents", "agent_version")
    op.drop_column("agents", "platform")
    op.drop_column("agents", "public_key_b64")
    op.drop_column("agents", "owner_user_id")
    op.drop_column("agents", "team_id")
    op.create_unique_constraint("uq_agents_name", "agents", ["name"])
