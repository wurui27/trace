"""Persist fenced tenant provisioning and actor-scoped idempotency.

Revision ID: 0002_tenant_provisioning_state
Revises: 0001_control_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_tenant_provisioning_state"
down_revision: str | None = "0001_control_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_STRING_COLUMNS = (
    ("database_owner_role_name", 255),
    ("database_migration_role_name", 255),
    ("database_migration_secret_ref", 512),
    ("database_role_name", 255),
    ("database_migration_revision", 255),
    ("database_ownership_receipt", 255),
    ("role_ownership_receipt", 255),
    ("bucket_ownership_receipt", 255),
    ("last_error_code", 96),
    ("worker_lease_owner", 255),
    ("transition_kind", 32),
    ("transition_step", 64),
    ("pending_database_name", 255),
    ("pending_database_owner_role_name", 255),
    ("pending_database_migration_role_name", 255),
    ("pending_database_migration_secret_ref", 512),
    ("pending_database_role_name", 255),
    ("pending_database_secret_ref", 512),
    ("pending_database_migration_revision", 255),
    ("pending_bucket_name", 255),
    ("pending_database_ownership_receipt", 255),
    ("pending_role_ownership_receipt", 255),
    ("pending_bucket_ownership_receipt", 255),
    ("previous_database_name", 255),
    ("previous_database_owner_role_name", 255),
    ("previous_database_migration_role_name", 255),
    ("previous_database_migration_secret_ref", 512),
    ("previous_database_role_name", 255),
    ("previous_database_secret_ref", 512),
    ("previous_database_migration_revision", 255),
    ("previous_bucket_name", 255),
    ("previous_database_ownership_receipt", 255),
    ("previous_role_ownership_receipt", 255),
    ("previous_bucket_ownership_receipt", 255),
)
_TENANT_INTEGER_COLUMNS = (
    "pending_resource_version",
    "pending_credential_version",
    "previous_resource_version",
    "previous_credential_version",
)


def upgrade() -> None:
    op.add_column(
        "tenant_resources",
        sa.Column(
            "requested_owner_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_tenant_resources_requested_owner_user_id_users",
        "tenant_resources",
        "users",
        ["requested_owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_tenant_resources_requested_owner_user_id",
        "tenant_resources",
        ["requested_owner_user_id"],
    )
    op.add_column(
        "tenant_resources",
        sa.Column(
            "provisioning_step",
            sa.String(length=32),
            server_default="requested",
            nullable=False,
        ),
    )
    for column_name, length in _TENANT_STRING_COLUMNS:
        op.add_column(
            "tenant_resources",
            sa.Column(column_name, sa.String(length=length), nullable=True),
        )
    op.add_column(
        "tenant_resources",
        sa.Column(
            "credential_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    for column_name in _TENANT_INTEGER_COLUMNS:
        op.add_column(
            "tenant_resources",
            sa.Column(column_name, sa.Integer(), nullable=True),
        )
    op.add_column(
        "tenant_resources",
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "tenant_resources",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_resources",
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_resources",
        sa.Column(
            "fencing_token",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "tenant_resources",
        sa.Column(
            "write_paused",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_tenant_resources_provisioning_step",
        "tenant_resources",
        "provisioning_step IN ('requested', 'database_allocated', 'database_created', "
        "'roles_allocated', 'roles_created', 'migration_credential_stored', "
        "'credentials_stored', 'tenant_migrated', "
        "'bucket_allocated', 'bucket_created', 'route_validated', 'active', 'cleanup')",
    )
    op.create_check_constraint(
        "ck_tenant_resources_credential_version",
        "tenant_resources",
        "credential_version > 0",
    )
    op.create_check_constraint(
        "ck_tenant_resources_retry_fence",
        "tenant_resources",
        "retry_count >= 0 AND fencing_token >= 0",
    )
    op.create_check_constraint(
        "ck_tenant_resources_transition_kind",
        "tenant_resources",
        "transition_kind IS NULL OR transition_kind IN "
        "('credential_rotation', 'resource_migration')",
    )
    op.create_index(
        "ix_tenant_resources_retry_lease",
        "tenant_resources",
        ["next_retry_at", "worker_lease_expires_at"],
    )
    op.create_index(
        "uq_tenant_resources_team_serving",
        "tenant_resources",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('active', 'migrating')"),
    )

    op.alter_column("idempotency_keys", "team_id", nullable=True)
    op.add_column(
        "idempotency_keys",
        sa.Column(
            "operation",
            sa.String(length=64),
            server_default="team_request",
            nullable=False,
        ),
    )
    op.add_column(
        "idempotency_keys",
        sa.Column(
            "scope_type",
            sa.String(length=32),
            server_default="team",
            nullable=False,
        ),
    )
    op.add_column(
        "idempotency_keys",
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(sa.text("UPDATE idempotency_keys SET scope_id = team_id"))
    op.alter_column("idempotency_keys", "scope_id", nullable=False)
    op.drop_constraint(
        "uq_idempotency_keys_team_key",
        "idempotency_keys",
        type_="unique",
    )
    op.create_check_constraint(
        "ck_idempotency_keys_scope_type",
        "idempotency_keys",
        "scope_type IN ('actor', 'team')",
    )
    op.create_check_constraint(
        "ck_idempotency_keys_team_scope_requires_team",
        "idempotency_keys",
        "scope_type = 'actor' OR team_id IS NOT NULL",
    )
    op.create_unique_constraint(
        "uq_idempotency_keys_operation_scope_key",
        "idempotency_keys",
        ["operation", "scope_type", "scope_id", "key"],
    )
    op.create_index(
        "ix_idempotency_keys_team_id",
        "idempotency_keys",
        ["team_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_team_id", table_name="idempotency_keys")
    op.drop_constraint(
        "uq_idempotency_keys_operation_scope_key",
        "idempotency_keys",
        type_="unique",
    )
    op.drop_constraint(
        "ck_idempotency_keys_team_scope_requires_team",
        "idempotency_keys",
        type_="check",
    )
    op.drop_constraint(
        "ck_idempotency_keys_scope_type",
        "idempotency_keys",
        type_="check",
    )
    op.execute(sa.text("DELETE FROM idempotency_keys WHERE team_id IS NULL"))
    op.execute(
        sa.text(
            "WITH ranked AS ("
            "SELECT id, row_number() OVER (PARTITION BY team_id, key ORDER BY created_at, id) "
            "AS duplicate_number FROM idempotency_keys"
            ") DELETE FROM idempotency_keys USING ranked "
            "WHERE idempotency_keys.id = ranked.id AND ranked.duplicate_number > 1"
        )
    )
    op.create_unique_constraint(
        "uq_idempotency_keys_team_key",
        "idempotency_keys",
        ["team_id", "key"],
    )
    op.drop_column("idempotency_keys", "scope_id")
    op.drop_column("idempotency_keys", "scope_type")
    op.drop_column("idempotency_keys", "operation")
    op.alter_column("idempotency_keys", "team_id", nullable=False)

    op.drop_index("uq_tenant_resources_team_serving", table_name="tenant_resources")
    op.drop_index("ix_tenant_resources_retry_lease", table_name="tenant_resources")
    op.drop_constraint(
        "ck_tenant_resources_transition_kind",
        "tenant_resources",
        type_="check",
    )
    op.drop_constraint(
        "ck_tenant_resources_retry_fence",
        "tenant_resources",
        type_="check",
    )
    op.drop_constraint(
        "ck_tenant_resources_credential_version",
        "tenant_resources",
        type_="check",
    )
    op.drop_constraint(
        "ck_tenant_resources_provisioning_step",
        "tenant_resources",
        type_="check",
    )
    op.drop_column("tenant_resources", "write_paused")
    op.drop_column("tenant_resources", "fencing_token")
    op.drop_column("tenant_resources", "worker_lease_expires_at")
    op.drop_column("tenant_resources", "next_retry_at")
    op.drop_column("tenant_resources", "retry_count")
    for column_name in reversed(_TENANT_INTEGER_COLUMNS):
        op.drop_column("tenant_resources", column_name)
    op.drop_column("tenant_resources", "credential_version")
    for column_name, _ in reversed(_TENANT_STRING_COLUMNS):
        op.drop_column("tenant_resources", column_name)
    op.drop_column("tenant_resources", "provisioning_step")
    op.drop_index(
        "ix_tenant_resources_requested_owner_user_id",
        table_name="tenant_resources",
    )
    op.drop_constraint(
        "fk_tenant_resources_requested_owner_user_id_users",
        "tenant_resources",
        type_="foreignkey",
    )
    op.drop_column("tenant_resources", "requested_owner_user_id")
