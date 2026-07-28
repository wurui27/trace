from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from psycopg import sql as psycopg_sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import (
    AuditEvent,
    AuthSession,
    IdempotencyKey,
    Membership,
    Team,
    TenantQuota,
    TenantResource,
    User,
)
from perfpilot_api.db.tenant.router import TenantRoute
from perfpilot_api.security.csrf import verify_csrf_token
from perfpilot_api.security.sessions import digest_session_token
from perfpilot_api.secrets.base import SecretContext, SecretNotFoundError, SecretStore

_T = TypeVar("_T")
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32,128}\Z")
_DEFAULT_LEASE_DURATION = timedelta(seconds=60)
_DEFAULT_RETIREMENT_GRACE = timedelta(minutes=5)
_RAW_RETENTION_DAYS = 30
_POSTGRES_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_OWNERSHIP_COMMENT_PREFIX = "perfpilot-owned:"


class ProvisioningLeaseLost(RuntimeError):
    def __init__(self) -> None:
        super().__init__("tenant provisioning lease was lost")


class ProvisioningInterrupted(RuntimeError):
    def __init__(self) -> None:
        super().__init__("tenant provisioning was interrupted")


@dataclass(frozen=True, slots=True)
class BucketPolicy:
    block_public_acls: bool
    ignore_public_acls: bool
    block_public_policy: bool
    restrict_public_buckets: bool
    versioning_enabled: bool
    sse_algorithm: str
    raw_prefix: str
    expire_current_days: int
    expire_noncurrent_days: int
    abort_multipart_days: int
    cors_origins: tuple[str, ...]
    cors_methods: tuple[str, ...]
    cors_headers: tuple[str, ...]
    allow_credentials: bool

    @classmethod
    def raw_artifacts(
        cls,
        *,
        sites_origin: str,
        retention_days: int = _RAW_RETENTION_DAYS,
    ) -> BucketPolicy:
        parsed = urlsplit(sites_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "*" in sites_origin
        ):
            raise ValueError("Sites origin must be one canonical HTTPS origin")
        if retention_days < 1:
            raise ValueError("raw artifact retention must be positive")
        canonical_origin = f"https://{parsed.netloc}".rstrip("/")
        return cls(
            block_public_acls=True,
            ignore_public_acls=True,
            block_public_policy=True,
            restrict_public_buckets=True,
            versioning_enabled=True,
            sse_algorithm="AES256",
            raw_prefix="raw/",
            expire_current_days=retention_days,
            expire_noncurrent_days=retention_days,
            abort_multipart_days=1,
            cors_origins=(canonical_origin,),
            cors_methods=("PUT", "HEAD"),
            cors_headers=("content-type", "x-amz-checksum-sha256"),
            allow_credentials=False,
        )


@dataclass(frozen=True, slots=True)
class TenantResourceRecord:
    id: UUID
    team_id: UUID
    requested_owner_user_id: UUID | None = None
    version: int = 1
    resource_version: int = 1
    state: str = "requested"
    provisioning_step: str = "requested"
    database_name: str | None = None
    database_owner_role_name: str | None = None
    database_migration_role_name: str | None = None
    database_migration_secret_ref: str | None = None
    database_role_name: str | None = None
    database_secret_ref: str | None = None
    credential_version: int = 1
    database_migration_revision: str | None = None
    bucket_name: str | None = None
    database_ownership_receipt: str | None = None
    role_ownership_receipt: str | None = None
    bucket_ownership_receipt: str | None = None
    last_error_code: str | None = None
    retry_count: int = 0
    next_retry_at: datetime | None = None
    worker_lease_owner: str | None = None
    worker_lease_expires_at: datetime | None = None
    fencing_token: int = 0
    write_paused: bool = False
    transition_kind: str | None = None
    transition_step: str | None = None
    pending_resource_version: int | None = None
    pending_database_name: str | None = None
    pending_database_owner_role_name: str | None = None
    pending_database_migration_role_name: str | None = None
    pending_database_migration_secret_ref: str | None = None
    pending_database_role_name: str | None = None
    pending_database_secret_ref: str | None = None
    pending_credential_version: int | None = None
    pending_database_migration_revision: str | None = None
    pending_bucket_name: str | None = None
    pending_database_ownership_receipt: str | None = None
    pending_role_ownership_receipt: str | None = None
    pending_bucket_ownership_receipt: str | None = None
    previous_resource_version: int | None = None
    previous_database_name: str | None = None
    previous_database_owner_role_name: str | None = None
    previous_database_migration_role_name: str | None = None
    previous_database_migration_secret_ref: str | None = None
    previous_database_role_name: str | None = None
    previous_database_secret_ref: str | None = None
    previous_credential_version: int | None = None
    previous_database_migration_revision: str | None = None
    previous_bucket_name: str | None = None
    previous_database_ownership_receipt: str | None = None
    previous_role_ownership_receipt: str | None = None
    previous_bucket_ownership_receipt: str | None = None

    @classmethod
    def requested(
        cls,
        *,
        team_id: UUID,
        resource_id: UUID | None = None,
        requested_owner_user_id: UUID | None = None,
    ) -> TenantResourceRecord:
        return cls(
            id=resource_id or uuid4(),
            team_id=team_id,
            requested_owner_user_id=requested_owner_user_id,
        )


@dataclass(frozen=True, slots=True)
class ProvisioningLease:
    resource_id: UUID
    team_id: UUID
    worker_id: str
    fencing_token: int


class ProvisioningRepository(Protocol):
    async def claim_for_request(
        self,
        *,
        team_id: UUID,
        idempotency_key: str,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]: ...

    async def claim_team(
        self,
        *,
        team_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]: ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord] | None: ...

    async def read(self, lease: ProvisioningLease) -> TenantResourceRecord: ...

    async def save(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord: ...

    async def activate(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord: ...

    async def assert_lease(self, lease: ProvisioningLease) -> None: ...

    async def renew(
        self,
        lease: ProvisioningLease,
        *,
        lease_duration: timedelta,
    ) -> None: ...

    async def release(self, lease: ProvisioningLease) -> None: ...


class PostgresTenantAdmin(Protocol):
    async def ensure_database(self, database_name: str, ownership_receipt: str) -> None: ...

    async def ensure_role_set(
        self,
        *,
        database_name: str,
        owner_role_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def ensure_runtime_role(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def set_role_password(
        self,
        *,
        database_name: str,
        role_name: str,
        password: bytes,
        ownership_receipt: str,
    ) -> None: ...

    async def delete_role_set(
        self,
        *,
        database_name: str,
        owner_role_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def revoke_runtime_role(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def pause_runtime_writes(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def resume_runtime_writes(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def make_database_read_only(
        self,
        *,
        database_name: str,
        ownership_receipt: str,
    ) -> None: ...

    async def delete_database(self, database_name: str, ownership_receipt: str) -> None: ...


class PsycopgTenantAdmin:
    """Manage tenant databases and distinct roles through one fixed admin cluster."""

    def __init__(self, *, admin_conninfo: str) -> None:
        try:
            parameters = conninfo_to_dict(admin_conninfo)
        except Exception:
            raise ValueError("tenant admin connection configuration is invalid") from None
        username = parameters.get("user")
        database = parameters.get("dbname")
        if not username or not database or not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(username):
            raise ValueError("tenant admin connection identity is invalid")
        self._parameters = parameters
        self._admin_username = username

    @staticmethod
    def _identifier(value: str) -> str:
        if not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(value):
            raise ProvisioningInterrupted
        return value

    @staticmethod
    def _comment(receipt: str) -> str:
        if not _TOKEN_PATTERN.fullmatch(receipt):
            raise ProvisioningInterrupted
        return f"{_OWNERSHIP_COMMENT_PREFIX}{receipt}"

    async def _connection(
        self,
        *,
        database_name: str | None = None,
        autocommit: bool = True,
    ) -> Any:
        try:
            parameters = dict(self._parameters)
            if database_name is not None:
                parameters["dbname"] = self._identifier(database_name)
            return await psycopg.AsyncConnection.connect(
                make_conninfo(**parameters),
                autocommit=autocommit,
                cursor_factory=psycopg.AsyncClientCursor,
            )
        except ProvisioningInterrupted:
            raise
        except Exception:
            raise ProvisioningInterrupted from None

    async def _database_metadata(
        self,
        database_name: str,
    ) -> tuple[bool, str | None, str | None]:
        try:
            async with await self._connection() as connection:
                row = await (
                    await connection.execute(
                        "SELECT shobj_description(database.oid, 'pg_database'), owner.rolname "
                        "FROM pg_database AS database "
                        "JOIN pg_roles AS owner ON owner.oid = database.datdba "
                        "WHERE database.datname = %s",
                        (self._identifier(database_name),),
                    )
                ).fetchone()
        except ProvisioningInterrupted:
            raise
        except Exception:
            raise ProvisioningInterrupted from None
        if row is None:
            return False, None, None
        return True, row[0], row[1]

    async def _role_metadata(self, role_name: str) -> tuple[bool, str | None]:
        try:
            async with await self._connection() as connection:
                row = await (
                    await connection.execute(
                        "SELECT shobj_description(oid, 'pg_authid') "
                        "FROM pg_roles WHERE rolname = %s",
                        (self._identifier(role_name),),
                    )
                ).fetchone()
        except ProvisioningInterrupted:
            raise
        except Exception:
            raise ProvisioningInterrupted from None
        if row is None:
            return False, None
        return True, row[0]

    @staticmethod
    def _database_name_matches_receipt(database_name: str, ownership_receipt: str) -> bool:
        return database_name == f"pp_t_{ownership_receipt[-20:]}"

    @staticmethod
    def _role_name_matches_receipt(role_name: str, ownership_receipt: str) -> bool:
        return role_name in {
            f"pp_o_{ownership_receipt[-20:]}",
            f"pp_m_{ownership_receipt[-20:]}",
            f"pp_r_{ownership_receipt[-20:]}",
        }

    async def _require_database_owner(
        self,
        database_name: str,
        ownership_receipt: str,
    ) -> None:
        exists, comment, _ = await self._database_metadata(database_name)
        if (
            not exists
            or comment is None
            or not secrets.compare_digest(str(comment), self._comment(ownership_receipt))
        ):
            raise ProvisioningInterrupted

    async def _require_role_owner(
        self,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        exists, comment = await self._role_metadata(role_name)
        if (
            not exists
            or comment is None
            or not secrets.compare_digest(str(comment), self._comment(ownership_receipt))
        ):
            raise ProvisioningInterrupted

    async def ensure_database(self, database_name: str, ownership_receipt: str) -> None:
        database_name = self._identifier(database_name)
        expected_comment = self._comment(ownership_receipt)
        exists, existing_comment, owner_name = await self._database_metadata(database_name)
        if exists:
            if existing_comment is not None:
                if not secrets.compare_digest(str(existing_comment), expected_comment):
                    raise ProvisioningInterrupted
                return
            if owner_name != self._admin_username or not self._database_name_matches_receipt(
                database_name, ownership_receipt
            ):
                raise ProvisioningInterrupted
            try:
                async with await self._connection() as connection:
                    await connection.execute(
                        psycopg_sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                            psycopg_sql.Identifier(database_name),
                            psycopg_sql.Literal(expected_comment),
                        )
                    )
            except Exception:
                raise ProvisioningInterrupted from None
            return
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                        psycopg_sql.Identifier(database_name)
                    )
                )
                await connection.execute(
                    psycopg_sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                        psycopg_sql.Identifier(database_name),
                        psycopg_sql.Literal(expected_comment),
                    )
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def _ensure_role(
        self,
        *,
        role_name: str,
        can_login: bool,
        ownership_receipt: str,
    ) -> None:
        role_name = self._identifier(role_name)
        expected_comment = self._comment(ownership_receipt)
        exists, existing_comment = await self._role_metadata(role_name)
        if exists:
            if existing_comment is not None:
                if not secrets.compare_digest(str(existing_comment), expected_comment):
                    raise ProvisioningInterrupted
                return
            if not self._role_name_matches_receipt(role_name, ownership_receipt):
                raise ProvisioningInterrupted
            try:
                async with await self._connection() as connection:
                    await connection.execute(
                        psycopg_sql.SQL("COMMENT ON ROLE {} IS {}").format(
                            psycopg_sql.Identifier(role_name),
                            psycopg_sql.Literal(expected_comment),
                        )
                    )
            except Exception:
                raise ProvisioningInterrupted from None
            return
        login_clause = psycopg_sql.SQL("LOGIN") if can_login else psycopg_sql.SQL("NOLOGIN")
        try:
            async with await self._connection(autocommit=False) as connection:
                await connection.execute(
                    psycopg_sql.SQL("CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                        psycopg_sql.Identifier(role_name),
                        login_clause,
                    )
                )
                await connection.execute(
                    psycopg_sql.SQL("COMMENT ON ROLE {} IS {}").format(
                        psycopg_sql.Identifier(role_name),
                        psycopg_sql.Literal(expected_comment),
                    )
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def ensure_role_set(
        self,
        *,
        database_name: str,
        owner_role_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        ownership_receipt: str,
    ) -> None:
        await self._require_database_owner(database_name, ownership_receipt)
        await self._ensure_role(
            role_name=owner_role_name,
            can_login=False,
            ownership_receipt=ownership_receipt,
        )
        await self._ensure_role(
            role_name=migration_role_name,
            can_login=True,
            ownership_receipt=ownership_receipt,
        )
        await self._ensure_role(
            role_name=runtime_role_name,
            can_login=True,
            ownership_receipt=ownership_receipt,
        )
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                        psycopg_sql.Identifier(database_name)
                    )
                )
                await connection.execute(
                    psycopg_sql.SQL("GRANT {} TO {}").format(
                        psycopg_sql.Identifier(owner_role_name),
                        psycopg_sql.Identifier(migration_role_name),
                    )
                )
                for role_name in (migration_role_name, runtime_role_name):
                    await connection.execute(
                        psycopg_sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            psycopg_sql.Identifier(database_name),
                            psycopg_sql.Identifier(role_name),
                        )
                    )
            async with await self._connection(database_name=database_name) as connection:
                await connection.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC")
                await connection.execute(
                    psycopg_sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
                        psycopg_sql.Identifier(owner_role_name)
                    )
                )
                await connection.execute(
                    psycopg_sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        psycopg_sql.Identifier(runtime_role_name)
                    )
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def ensure_runtime_role(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        await self._require_database_owner(database_name, ownership_receipt)
        await self._ensure_role(
            role_name=role_name,
            can_login=True,
            ownership_receipt=ownership_receipt,
        )
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        psycopg_sql.Identifier(self._identifier(database_name)),
                        psycopg_sql.Identifier(self._identifier(role_name)),
                    )
                )
            async with await self._connection(database_name=database_name) as connection:
                await connection.execute(
                    psycopg_sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        psycopg_sql.Identifier(role_name)
                    )
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(psycopg_sql.Identifier(role_name))
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(psycopg_sql.Identifier(role_name))
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def set_role_password(
        self,
        *,
        database_name: str,
        role_name: str,
        password: bytes,
        ownership_receipt: str,
    ) -> None:
        await self._require_database_owner(database_name, ownership_receipt)
        await self._require_role_owner(role_name, ownership_receipt)
        try:
            decoded_password = password.decode("ascii")
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("ALTER ROLE {} PASSWORD %s").format(
                        psycopg_sql.Identifier(self._identifier(role_name))
                    ),
                    (decoded_password,),
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def delete_role_set(
        self,
        *,
        database_name: str,
        owner_role_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        ownership_receipt: str,
    ) -> None:
        role_names = (runtime_role_name, migration_role_name, owner_role_name)
        existing_roles: list[str] = []
        for role_name in role_names:
            exists, comment = await self._role_metadata(role_name)
            if not exists:
                continue
            if comment is None or not secrets.compare_digest(
                str(comment), self._comment(ownership_receipt)
            ):
                raise ProvisioningInterrupted
            existing_roles.append(role_name)
        if not existing_roles:
            return
        try:
            database_exists, database_comment, _ = await self._database_metadata(database_name)
            if database_exists:
                if database_comment is None or not secrets.compare_digest(
                    str(database_comment), self._comment(ownership_receipt)
                ):
                    raise ProvisioningInterrupted
                async with await self._connection(database_name=database_name) as connection:
                    for role_name in existing_roles:
                        await connection.execute(
                            psycopg_sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                                psycopg_sql.Identifier(role_name),
                                psycopg_sql.Identifier(self._admin_username),
                            )
                        )
                        await connection.execute(
                            psycopg_sql.SQL("DROP OWNED BY {}").format(
                                psycopg_sql.Identifier(role_name)
                            )
                        )
            async with await self._connection() as connection:
                for role_name in existing_roles:
                    await connection.execute(
                        psycopg_sql.SQL("DROP ROLE {}").format(psycopg_sql.Identifier(role_name))
                    )
        except Exception:
            raise ProvisioningInterrupted from None

    async def revoke_runtime_role(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        exists, _ = await self._role_metadata(role_name)
        if not exists:
            return
        await self._require_database_owner(database_name, ownership_receipt)
        await self._require_role_owner(role_name, ownership_receipt)
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = %s AND pid <> pg_backend_pid()",
                    (role_name,),
                )
            async with await self._connection(database_name=database_name) as connection:
                await connection.execute(
                    psycopg_sql.SQL("DROP OWNED BY {}").format(psycopg_sql.Identifier(role_name))
                )
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("DROP ROLE {}").format(psycopg_sql.Identifier(role_name))
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def pause_runtime_writes(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        await self._require_database_owner(database_name, ownership_receipt)
        await self._require_role_owner(role_name, ownership_receipt)
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                        psycopg_sql.Identifier(role_name)
                    )
                )
            async with await self._connection(database_name=database_name) as connection:
                await connection.execute(
                    psycopg_sql.SQL(
                        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
                        "ON ALL TABLES IN SCHEMA public FROM {}"
                    ).format(psycopg_sql.Identifier(role_name))
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "REVOKE USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public FROM {}"
                    ).format(psycopg_sql.Identifier(role_name))
                )
            async with await self._connection() as connection:
                await connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = %s AND pid <> pg_backend_pid()",
                    (role_name,),
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def resume_runtime_writes(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        await self._require_database_owner(database_name, ownership_receipt)
        await self._require_role_owner(role_name, ownership_receipt)
        try:
            async with await self._connection(database_name=database_name) as connection:
                await connection.execute(
                    psycopg_sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(psycopg_sql.Identifier(role_name))
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(psycopg_sql.Identifier(role_name))
                )
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL("ALTER ROLE {} RESET default_transaction_read_only").format(
                        psycopg_sql.Identifier(role_name)
                    )
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def make_database_read_only(
        self,
        *,
        database_name: str,
        ownership_receipt: str,
    ) -> None:
        await self._require_database_owner(database_name, ownership_receipt)
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    psycopg_sql.SQL(
                        "ALTER DATABASE {} SET default_transaction_read_only = on"
                    ).format(psycopg_sql.Identifier(database_name))
                )
        except Exception:
            raise ProvisioningInterrupted from None

    async def delete_database(self, database_name: str, ownership_receipt: str) -> None:
        exists, comment, owner_name = await self._database_metadata(database_name)
        if not exists:
            return
        expected_comment = self._comment(ownership_receipt)
        if comment is None:
            if owner_name != self._admin_username or not self._database_name_matches_receipt(
                database_name, ownership_receipt
            ):
                raise ProvisioningInterrupted
        elif not secrets.compare_digest(str(comment), expected_comment):
            raise ProvisioningInterrupted
        try:
            async with await self._connection() as connection:
                await connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                await connection.execute(
                    psycopg_sql.SQL("DROP DATABASE {}").format(
                        psycopg_sql.Identifier(database_name)
                    )
                )
        except Exception:
            raise ProvisioningInterrupted from None


class TenantMigrator(Protocol):
    async def migrate(
        self,
        *,
        database_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        password: bytes,
    ) -> str: ...


class AlembicTenantMigrator:
    """Run the tenant migration tree against one fixed cluster endpoint."""

    def __init__(
        self,
        *,
        migration_root: Path,
        cluster_host: str,
        cluster_port: int = 5432,
        sslmode: str = "verify-full",
    ) -> None:
        if not migration_root.is_dir() or not (migration_root / "alembic.ini").is_file():
            raise ValueError("tenant migration root is invalid")
        if not cluster_host or any(character in cluster_host for character in "/?#@"):
            raise ValueError("tenant migration cluster host is invalid")
        if not 1 <= cluster_port <= 65535:
            raise ValueError("tenant migration cluster port is invalid")
        if sslmode not in {
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise ValueError("tenant migration sslmode is invalid")
        self._migration_root = migration_root.resolve()
        self._cluster_host = cluster_host
        self._cluster_port = cluster_port
        self._sslmode = sslmode

    async def migrate(
        self,
        *,
        database_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        password: bytes,
    ) -> str:
        for identifier in (database_name, migration_role_name, runtime_role_name):
            if not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(identifier):
                raise ProvisioningInterrupted
        try:
            decoded_password = password.decode("ascii")
            database_url = URL.create(
                "postgresql+psycopg",
                username=migration_role_name,
                password=decoded_password,
                host=self._cluster_host,
                port=self._cluster_port,
                database=database_name,
                query={"sslmode": self._sslmode},
            )
            config = AlembicConfig(str(self._migration_root / "alembic.ini"))
            config.set_main_option("script_location", str(self._migration_root))
            config.attributes["sqlalchemy_url"] = database_url
            await asyncio.to_thread(alembic_command.upgrade, config, "head")
            conninfo = database_url.set(drivername="postgresql").render_as_string(
                hide_password=False
            )
            async with await psycopg.AsyncConnection.connect(conninfo) as connection:
                await connection.execute(
                    psycopg_sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(psycopg_sql.Identifier(runtime_role_name))
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(psycopg_sql.Identifier(runtime_role_name))
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                    ).format(
                        psycopg_sql.Identifier(migration_role_name),
                        psycopg_sql.Identifier(runtime_role_name),
                    )
                )
                await connection.execute(
                    psycopg_sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                    ).format(
                        psycopg_sql.Identifier(migration_role_name),
                        psycopg_sql.Identifier(runtime_role_name),
                    )
                )
                row = await (
                    await connection.execute("SELECT version_num FROM alembic_version")
                ).fetchone()
                await connection.commit()
            if row is None or not isinstance(row[0], str) or not row[0]:
                raise ProvisioningInterrupted
            return row[0]
        except ProvisioningInterrupted:
            raise
        except Exception:
            raise ProvisioningInterrupted from None


class BucketAdmin(Protocol):
    async def ensure_bucket(
        self,
        bucket_name: str,
        ownership_receipt: str,
        policy: BucketPolicy,
    ) -> None: ...

    async def delete_bucket(self, bucket_name: str, ownership_receipt: str) -> None: ...


class S3BucketAdmin:
    """Apply and remove an exact S3 bucket policy guarded by an ownership tag."""

    _OWNERSHIP_TAG = "perfpilot:ownership-receipt"

    def __init__(self, *, client: Any) -> None:
        self._client = client

    @staticmethod
    def _error_code(error: Exception) -> str | None:
        response = getattr(error, "response", None)
        if not isinstance(response, dict):
            return None
        error_details = response.get("Error")
        if not isinstance(error_details, dict):
            return None
        code = error_details.get("Code")
        return str(code) if code is not None else None

    async def _call(self, method: str, **kwargs: object) -> dict[str, Any]:
        function = getattr(self._client, method)
        result = await asyncio.to_thread(function, **kwargs)
        return result if isinstance(result, dict) else {}

    async def _ownership(self, bucket_name: str) -> tuple[bool, str | None]:
        try:
            response = await self._call("get_bucket_tagging", Bucket=bucket_name)
        except Exception as exc:
            if self._error_code(exc) in {"NoSuchBucket", "404", "NotFound"}:
                return False, None
            if self._error_code(exc) in {"NoSuchTagSet", "NoSuchTagging"}:
                return True, None
            raise ProvisioningInterrupted from None
        tag_set = response.get("TagSet")
        if not isinstance(tag_set, list):
            raise ProvisioningInterrupted
        tags = {
            str(item.get("Key")): str(item.get("Value"))
            for item in tag_set
            if isinstance(item, dict)
        }
        receipt = tags.get(self._OWNERSHIP_TAG)
        if not receipt:
            raise ProvisioningInterrupted
        return True, receipt

    async def _verify_configuration(
        self,
        bucket_name: str,
        policy: BucketPolicy,
    ) -> None:
        expected_public_access = {
            "BlockPublicAcls": policy.block_public_acls,
            "IgnorePublicAcls": policy.ignore_public_acls,
            "BlockPublicPolicy": policy.block_public_policy,
            "RestrictPublicBuckets": policy.restrict_public_buckets,
        }
        expected_encryption = {
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": policy.sse_algorithm}}
            ]
        }
        expected_lifecycle_rules = [
            {
                "ID": "raw-artifact-retention",
                "Status": "Enabled",
                "Filter": {"Prefix": policy.raw_prefix},
                "Expiration": {"Days": policy.expire_current_days},
                "NoncurrentVersionExpiration": {"NoncurrentDays": policy.expire_noncurrent_days},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": policy.abort_multipart_days
                },
            }
        ]
        expected_cors_rules = [
            {
                "AllowedOrigins": list(policy.cors_origins),
                "AllowedMethods": list(policy.cors_methods),
                "AllowedHeaders": list(policy.cors_headers),
            }
        ]
        try:
            public_access = await self._call("get_public_access_block", Bucket=bucket_name)
            versioning = await self._call("get_bucket_versioning", Bucket=bucket_name)
            encryption = await self._call("get_bucket_encryption", Bucket=bucket_name)
            lifecycle = await self._call(
                "get_bucket_lifecycle_configuration",
                Bucket=bucket_name,
            )
            cors = await self._call("get_bucket_cors", Bucket=bucket_name)
        except Exception:
            raise ProvisioningInterrupted from None
        if (
            public_access.get("PublicAccessBlockConfiguration") != expected_public_access
            or versioning.get("Status") != "Enabled"
            or encryption.get("ServerSideEncryptionConfiguration") != expected_encryption
            or lifecycle.get("Rules") != expected_lifecycle_rules
            or cors.get("CORSRules") != expected_cors_rules
        ):
            raise ProvisioningInterrupted

    async def ensure_bucket(
        self,
        bucket_name: str,
        ownership_receipt: str,
        policy: BucketPolicy,
    ) -> None:
        if not bucket_name or not ownership_receipt:
            raise ProvisioningInterrupted
        exists, existing_receipt = await self._ownership(bucket_name)
        if not exists:
            try:
                await self._call("create_bucket", Bucket=bucket_name)
                await self._call(
                    "put_bucket_tagging",
                    Bucket=bucket_name,
                    Tagging={
                        "TagSet": [
                            {
                                "Key": self._OWNERSHIP_TAG,
                                "Value": ownership_receipt,
                            }
                        ]
                    },
                )
            except Exception:
                raise ProvisioningInterrupted from None
        elif existing_receipt is None:
            if bucket_name != f"pp-{ownership_receipt[-32:]}":
                raise ProvisioningInterrupted
            try:
                await self._call(
                    "put_bucket_tagging",
                    Bucket=bucket_name,
                    Tagging={
                        "TagSet": [
                            {
                                "Key": self._OWNERSHIP_TAG,
                                "Value": ownership_receipt,
                            }
                        ]
                    },
                )
            except Exception:
                raise ProvisioningInterrupted from None
        elif not secrets.compare_digest(existing_receipt, ownership_receipt):
            raise ProvisioningInterrupted
        try:
            await self._call(
                "put_public_access_block",
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": policy.block_public_acls,
                    "IgnorePublicAcls": policy.ignore_public_acls,
                    "BlockPublicPolicy": policy.block_public_policy,
                    "RestrictPublicBuckets": policy.restrict_public_buckets,
                },
            )
            await self._call(
                "put_bucket_versioning",
                Bucket=bucket_name,
                VersioningConfiguration={
                    "Status": "Enabled" if policy.versioning_enabled else "Suspended"
                },
            )
            await self._call(
                "put_bucket_encryption",
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": policy.sse_algorithm
                            }
                        }
                    ]
                },
            )
            await self._call(
                "put_bucket_lifecycle_configuration",
                Bucket=bucket_name,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "raw-artifact-retention",
                            "Status": "Enabled",
                            "Filter": {"Prefix": policy.raw_prefix},
                            "Expiration": {"Days": policy.expire_current_days},
                            "NoncurrentVersionExpiration": {
                                "NoncurrentDays": policy.expire_noncurrent_days
                            },
                            "AbortIncompleteMultipartUpload": {
                                "DaysAfterInitiation": policy.abort_multipart_days
                            },
                        }
                    ]
                },
            )
            await self._call(
                "put_bucket_cors",
                Bucket=bucket_name,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedOrigins": list(policy.cors_origins),
                            "AllowedMethods": list(policy.cors_methods),
                            "AllowedHeaders": list(policy.cors_headers),
                        }
                    ]
                },
            )
            await self._verify_configuration(bucket_name, policy)
        except Exception as exc:
            if isinstance(exc, ProvisioningInterrupted):
                raise
            raise ProvisioningInterrupted from None

    async def delete_bucket(self, bucket_name: str, ownership_receipt: str) -> None:
        exists, existing_receipt = await self._ownership(bucket_name)
        if not exists:
            return
        if existing_receipt is None:
            if bucket_name != f"pp-{ownership_receipt[-32:]}":
                raise ProvisioningInterrupted
        elif not secrets.compare_digest(existing_receipt, ownership_receipt):
            raise ProvisioningInterrupted
        key_marker: str | None = None
        version_marker: str | None = None
        try:
            while True:
                request: dict[str, object] = {"Bucket": bucket_name}
                if key_marker is not None:
                    request["KeyMarker"] = key_marker
                if version_marker is not None:
                    request["VersionIdMarker"] = version_marker
                response = await self._call("list_object_versions", **request)
                objects = [
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for collection in ("Versions", "DeleteMarkers")
                    for item in response.get(collection, [])
                    if isinstance(item, dict) and "Key" in item and "VersionId" in item
                ]
                if objects:
                    await self._call(
                        "delete_objects",
                        Bucket=bucket_name,
                        Delete={"Objects": objects, "Quiet": True},
                    )
                if not response.get("IsTruncated"):
                    break
                key_marker = str(response["NextKeyMarker"])
                version_marker = str(response["NextVersionIdMarker"])
            await self._call("delete_bucket", Bucket=bucket_name)
        except Exception:
            raise ProvisioningInterrupted from None


class TenantRouterLifecycle(Protocol):
    async def validate_route(self, route: TenantRoute) -> None: ...

    async def dispose_team(
        self,
        team_id: UUID,
        *,
        keep_resource_version: int | None = None,
    ) -> int: ...


class TenantReplicator(Protocol):
    async def copy_and_validate(
        self,
        *,
        source_database_name: str,
        source_migration_role_name: str,
        source_password: bytes,
        target_database_name: str,
        target_migration_role_name: str,
        target_password: bytes,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _ReplicationTable:
    name: str
    columns: tuple[str, ...]
    copy_columns: tuple[str, ...]
    is_partition: bool


class PsycopgTenantReplicator:
    """Copy public tenant data between databases on one configured cluster."""

    def __init__(
        self,
        *,
        cluster_host: str,
        cluster_port: int = 5432,
        sslmode: str = "verify-full",
    ) -> None:
        if not cluster_host or any(character in cluster_host for character in "/?#@"):
            raise ValueError("tenant replication cluster host is invalid")
        if not 1 <= cluster_port <= 65535:
            raise ValueError("tenant replication cluster port is invalid")
        if sslmode not in {
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise ValueError("tenant replication SSL mode is invalid")
        self._cluster_host = cluster_host
        self._cluster_port = cluster_port
        self._sslmode = sslmode

    @staticmethod
    def _identifier(value: str) -> str:
        if not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(value):
            raise ProvisioningInterrupted
        return value

    async def _connection(
        self,
        *,
        database_name: str,
        role_name: str,
        password: bytes,
    ) -> Any:
        if not isinstance(password, bytes) or not password or b"\x00" in password:
            raise ProvisioningInterrupted
        try:
            decoded_password = password.decode("utf-8")
        except UnicodeDecodeError:
            raise ProvisioningInterrupted from None
        return await psycopg.AsyncConnection.connect(
            host=self._cluster_host,
            port=self._cluster_port,
            sslmode=self._sslmode,
            dbname=self._identifier(database_name),
            user=self._identifier(role_name),
            password=decoded_password,
            autocommit=False,
        )

    @staticmethod
    async def _tables(connection: Any) -> tuple[_ReplicationTable, ...]:
        rows = await (
            await connection.execute(
                "SELECT class.relname, attribute.attname, attribute.attgenerated, "
                "class.relispartition "
                "FROM pg_catalog.pg_class AS class "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = class.oid "
                "WHERE namespace.nspname = 'public' "
                "AND class.relkind IN ('r', 'p') "
                "AND attribute.attnum > 0 AND NOT attribute.attisdropped "
                'ORDER BY class.relname COLLATE "C", attribute.attnum'
            )
        ).fetchall()
        table_columns: dict[str, list[str]] = {}
        copy_columns: dict[str, list[str]] = {}
        partition_flags: dict[str, bool] = {}
        for table_name, column_name, generated, is_partition in rows:
            table_columns.setdefault(table_name, []).append(column_name)
            copy_columns.setdefault(table_name, [])
            if not generated:
                copy_columns[table_name].append(column_name)
            partition_flags[table_name] = bool(is_partition)
        return tuple(
            _ReplicationTable(
                name=table_name,
                columns=tuple(table_columns[table_name]),
                copy_columns=tuple(copy_columns[table_name]),
                is_partition=partition_flags[table_name],
            )
            for table_name in sorted(table_columns)
        )

    @staticmethod
    async def _copy_order(
        connection: Any, tables: tuple[_ReplicationTable, ...]
    ) -> tuple[str, ...]:
        copy_names = {table.name for table in tables if not table.is_partition}
        rows = await (
            await connection.execute(
                "SELECT child.relname, parent.relname "
                "FROM pg_catalog.pg_constraint AS foreign_key "
                "JOIN pg_catalog.pg_class AS child ON child.oid = foreign_key.conrelid "
                "JOIN pg_catalog.pg_namespace AS child_namespace "
                "ON child_namespace.oid = child.relnamespace "
                "JOIN pg_catalog.pg_class AS parent ON parent.oid = foreign_key.confrelid "
                "JOIN pg_catalog.pg_namespace AS parent_namespace "
                "ON parent_namespace.oid = parent.relnamespace "
                "WHERE foreign_key.contype = 'f' AND NOT foreign_key.condeferrable "
                "AND child_namespace.nspname = 'public' "
                "AND parent_namespace.nspname = 'public'"
            )
        ).fetchall()
        dependencies = {name: set() for name in copy_names}
        for child, parent in rows:
            if child in dependencies and parent in copy_names and child != parent:
                dependencies[child].add(parent)
        ordered: list[str] = []
        remaining = set(copy_names)
        while remaining:
            ready = sorted(name for name in remaining if not (dependencies[name] & remaining))
            if not ready:
                raise ProvisioningInterrupted
            ordered.extend(ready)
            remaining.difference_update(ready)
        return tuple(ordered)

    @staticmethod
    async def _sequences(connection: Any) -> tuple[str, ...]:
        rows = await (
            await connection.execute(
                "SELECT class.relname FROM pg_catalog.pg_class AS class "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = 'public' AND class.relkind = 'S' "
                'ORDER BY class.relname COLLATE "C"'
            )
        ).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    async def _truncate(connection: Any, table_names: tuple[str, ...]) -> None:
        if not table_names:
            return
        identifiers = psycopg_sql.SQL(", ").join(
            psycopg_sql.Identifier("public", table_name) for table_name in table_names
        )
        await connection.execute(
            psycopg_sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(identifiers)
        )

    @staticmethod
    async def _copy_table(
        source: Any,
        target: Any,
        table: _ReplicationTable,
    ) -> None:
        if not table.copy_columns:
            return
        columns = psycopg_sql.SQL(", ").join(
            psycopg_sql.Identifier(column) for column in table.copy_columns
        )
        source_statement = psycopg_sql.SQL(
            "COPY (SELECT {} FROM {}) TO STDOUT (FORMAT BINARY)"
        ).format(columns, psycopg_sql.Identifier("public", table.name))
        target_statement = psycopg_sql.SQL("COPY {} ({}) FROM STDIN (FORMAT BINARY)").format(
            psycopg_sql.Identifier("public", table.name), columns
        )
        async with source.cursor() as source_cursor, target.cursor() as target_cursor:
            async with source_cursor.copy(source_statement) as copy_out:
                async with target_cursor.copy(target_statement) as copy_in:
                    while chunk := await copy_out.read():
                        await copy_in.write(chunk)

    @staticmethod
    async def _copy_sequence(source: Any, target: Any, sequence_name: str) -> None:
        row = await (
            await source.execute(
                psycopg_sql.SQL("SELECT last_value, is_called FROM {}").format(
                    psycopg_sql.Identifier("public", sequence_name)
                )
            )
        ).fetchone()
        if row is None:
            raise ProvisioningInterrupted
        updated = await (
            await target.execute(
                "SELECT pg_catalog.setval(class.oid::regclass, %s, %s) "
                "FROM pg_catalog.pg_class AS class "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = 'public' AND class.relname = %s "
                "AND class.relkind = 'S'",
                (row[0], row[1], sequence_name),
            )
        ).fetchone()
        if updated is None:
            raise ProvisioningInterrupted

    @staticmethod
    async def _table_summary(connection: Any, table_name: str) -> tuple[int, bytes]:
        count_row = await (
            await connection.execute(
                psycopg_sql.SQL("SELECT count(*) FROM {}").format(
                    psycopg_sql.Identifier("public", table_name)
                )
            )
        ).fetchone()
        if count_row is None:
            raise ProvisioningInterrupted
        digest = hashlib.sha256()
        statement = psycopg_sql.SQL(
            "SELECT row_to_json(row_value)::text FROM (SELECT * FROM {}) AS row_value "
            'ORDER BY row_to_json(row_value)::text COLLATE "C"'
        ).format(psycopg_sql.Identifier("public", table_name))
        async with connection.cursor() as cursor:
            await cursor.execute(statement)
            async for row in cursor:
                encoded = row[0].encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        return int(count_row[0]), digest.digest()

    @classmethod
    async def _validate(
        cls,
        source: Any,
        target: Any,
        tables: tuple[_ReplicationTable, ...],
    ) -> None:
        if await cls._tables(target) != tables:
            raise ProvisioningInterrupted
        source_sequences = await cls._sequences(source)
        if await cls._sequences(target) != source_sequences:
            raise ProvisioningInterrupted
        for table in tables:
            source_summary = await cls._table_summary(source, table.name)
            target_summary = await cls._table_summary(target, table.name)
            if source_summary != target_summary:
                raise ProvisioningInterrupted

    async def copy_and_validate(
        self,
        *,
        source_database_name: str,
        source_migration_role_name: str,
        source_password: bytes,
        target_database_name: str,
        target_migration_role_name: str,
        target_password: bytes,
    ) -> None:
        identifiers = (
            source_database_name,
            source_migration_role_name,
            target_database_name,
            target_migration_role_name,
        )
        if any(not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(value) for value in identifiers):
            raise ProvisioningInterrupted
        if source_database_name == target_database_name:
            raise ProvisioningInterrupted
        try:
            async with await self._connection(
                database_name=source_database_name,
                role_name=source_migration_role_name,
                password=source_password,
            ) as source:
                async with await self._connection(
                    database_name=target_database_name,
                    role_name=target_migration_role_name,
                    password=target_password,
                ) as target:
                    await source.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    await target.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    await target.execute("SET CONSTRAINTS ALL DEFERRED")
                    source_tables = await self._tables(source)
                    if await self._tables(target) != source_tables:
                        raise ProvisioningInterrupted
                    source_sequences = await self._sequences(source)
                    if await self._sequences(target) != source_sequences:
                        raise ProvisioningInterrupted
                    copy_order = await self._copy_order(target, source_tables)
                    await self._truncate(
                        target,
                        tuple(table.name for table in source_tables),
                    )
                    table_by_name = {table.name: table for table in source_tables}
                    for table_name in copy_order:
                        await self._copy_table(source, target, table_by_name[table_name])
                    for sequence_name in source_sequences:
                        await self._copy_sequence(source, target, sequence_name)
                    await self._validate(source, target, source_tables)
        except ProvisioningInterrupted:
            raise
        except Exception:
            raise ProvisioningInterrupted from None


class InMemoryProvisioningRepository:
    """Explicit development/test repository; production wiring must use PostgreSQL."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records: dict[UUID, TenantResourceRecord] = {}
        self._requests: dict[tuple[UUID, str], UUID] = {}
        self._lock = asyncio.Lock()
        self.history: list[TenantResourceRecord] = []

    def add_requested(
        self,
        *,
        team_id: UUID,
        resource_id: UUID | None = None,
        idempotency_key: str,
    ) -> TenantResourceRecord:
        key = (team_id, idempotency_key)
        if key in self._requests:
            return self._records[team_id]
        if team_id in self._records:
            raise ValueError("team already has a tenant resource")
        record = TenantResourceRecord.requested(
            team_id=team_id,
            resource_id=resource_id,
        )
        self._records[team_id] = record
        self._requests[key] = record.id
        self.history.append(record)
        return record

    def current(self, team_id: UUID) -> TenantResourceRecord:
        return self._records[team_id]

    def steal_lease(self, team_id: UUID, *, new_owner: str) -> None:
        current = self._records[team_id]
        self._records[team_id] = replace(
            current,
            version=current.version + 1,
            fencing_token=current.fencing_token + 1,
            worker_lease_owner=new_owner,
            worker_lease_expires_at=self._clock() + _DEFAULT_LEASE_DURATION,
        )

    async def claim_for_request(
        self,
        *,
        team_id: UUID,
        idempotency_key: str,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]:
        if self._requests.get((team_id, idempotency_key)) is None:
            raise ProvisioningInterrupted
        return await self.claim_team(
            team_id=team_id,
            worker_id=worker_id,
            lease_duration=lease_duration,
        )

    async def claim_team(
        self,
        *,
        team_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]:
        async with self._lock:
            current = self._records.get(team_id)
            if current is None:
                raise ProvisioningInterrupted
            now = self._clock()
            if (
                current.worker_lease_owner is not None
                and current.worker_lease_expires_at is not None
                and current.worker_lease_expires_at > now
            ):
                raise ProvisioningInterrupted
            claimed = replace(
                current,
                version=current.version + 1,
                worker_lease_owner=worker_id,
                worker_lease_expires_at=now + lease_duration,
                fencing_token=current.fencing_token + 1,
            )
            self._records[team_id] = claimed
            lease = ProvisioningLease(
                resource_id=claimed.id,
                team_id=team_id,
                worker_id=worker_id,
                fencing_token=claimed.fencing_token,
            )
            return lease, claimed

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord] | None:
        now = self._clock()
        candidate = next(
            (
                record
                for record in sorted(
                    self._records.values(),
                    key=lambda item: (item.next_retry_at or now, str(item.id)),
                )
                if (
                    record.state in {"requested", "provisioning", "cleanup_pending"}
                    or record.transition_kind is not None
                )
                and (record.next_retry_at is None or record.next_retry_at <= now)
                and (
                    record.worker_lease_expires_at is None or record.worker_lease_expires_at <= now
                )
            ),
            None,
        )
        if candidate is None:
            return None
        return await self.claim_team(
            team_id=candidate.team_id,
            worker_id=worker_id,
            lease_duration=lease_duration,
        )

    def _lease_matches(self, lease: ProvisioningLease, record: TenantResourceRecord) -> bool:
        return (
            record.id == lease.resource_id
            and record.worker_lease_owner == lease.worker_id
            and record.fencing_token == lease.fencing_token
            and record.worker_lease_expires_at is not None
            and record.worker_lease_expires_at > self._clock()
        )

    async def read(self, lease: ProvisioningLease) -> TenantResourceRecord:
        async with self._lock:
            record = self._records.get(lease.team_id)
            if record is None or not self._lease_matches(lease, record):
                raise ProvisioningLeaseLost
            return record

    async def save(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        async with self._lock:
            current = self._records.get(lease.team_id)
            if (
                current is None
                or not self._lease_matches(lease, current)
                or current.version != record.version
            ):
                raise ProvisioningLeaseLost
            saved = replace(
                record,
                version=record.version + 1,
                worker_lease_owner=lease.worker_id,
                worker_lease_expires_at=current.worker_lease_expires_at,
                fencing_token=lease.fencing_token,
            )
            self._records[lease.team_id] = saved
            self.history.append(saved)
            return saved

    async def activate(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        return await self.save(lease, record)

    async def assert_lease(self, lease: ProvisioningLease) -> None:
        await self.read(lease)

    async def renew(
        self,
        lease: ProvisioningLease,
        *,
        lease_duration: timedelta,
    ) -> None:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease duration must be positive")
        async with self._lock:
            current = self._records.get(lease.team_id)
            if current is None or not self._lease_matches(lease, current):
                raise ProvisioningLeaseLost
            self._records[lease.team_id] = replace(
                current,
                worker_lease_expires_at=self._clock() + lease_duration,
            )

    async def release(self, lease: ProvisioningLease) -> None:
        async with self._lock:
            current = self._records.get(lease.team_id)
            if current is None or not self._lease_matches(lease, current):
                return
            self._records[lease.team_id] = replace(
                current,
                version=current.version + 1,
                worker_lease_owner=None,
                worker_lease_expires_at=None,
            )


_RECORD_FIELD_NAMES = tuple(field.name for field in fields(TenantResourceRecord))
_MUTABLE_RECORD_FIELD_NAMES = tuple(
    name for name in _RECORD_FIELD_NAMES if name not in {"id", "team_id", "version"}
)


class SqlAlchemyProvisioningRepository:
    """PostgreSQL repository with row-lock claims, fencing, and optimistic saves."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _record(row: TenantResource) -> TenantResourceRecord:
        return TenantResourceRecord(**{name: getattr(row, name) for name in _RECORD_FIELD_NAMES})

    @staticmethod
    def _lease(row: TenantResource, worker_id: str) -> ProvisioningLease:
        return ProvisioningLease(
            resource_id=row.id,
            team_id=row.team_id,
            worker_id=worker_id,
            fencing_token=row.fencing_token,
        )

    async def _claim_locked_row(
        self,
        session: AsyncSession,
        row: TenantResource,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]:
        now = self._clock()
        if (
            row.worker_lease_owner is not None
            and row.worker_lease_expires_at is not None
            and row.worker_lease_expires_at > now
        ):
            raise ProvisioningInterrupted
        row.worker_lease_owner = worker_id
        row.worker_lease_expires_at = now + lease_duration
        row.fencing_token += 1
        row.version += 1
        await session.flush()
        return self._lease(row, worker_id), self._record(row)

    async def claim_for_request(
        self,
        *,
        team_id: UUID,
        idempotency_key: str,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]:
        async with self._session_factory() as session, session.begin():
            resource_id = await session.scalar(
                select(IdempotencyKey.response_resource_id).where(
                    IdempotencyKey.team_id == team_id,
                    IdempotencyKey.key == idempotency_key,
                    IdempotencyKey.operation == "create_team",
                    IdempotencyKey.scope_type == "actor",
                    IdempotencyKey.state == "completed",
                )
            )
            if resource_id is None:
                raise ProvisioningInterrupted
            row = await session.scalar(
                select(TenantResource)
                .where(
                    TenantResource.id == resource_id,
                    TenantResource.team_id == team_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ProvisioningInterrupted
            return await self._claim_locked_row(
                session,
                row,
                worker_id=worker_id,
                lease_duration=lease_duration,
            )

    async def claim_team(
        self,
        *,
        team_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord]:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(TenantResource)
                .where(TenantResource.team_id == team_id)
                .order_by(TenantResource.resource_version.desc())
                .limit(1)
                .with_for_update()
            )
            if row is None:
                raise ProvisioningInterrupted
            return await self._claim_locked_row(
                session,
                row,
                worker_id=worker_id,
                lease_duration=lease_duration,
            )

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> tuple[ProvisioningLease, TenantResourceRecord] | None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(TenantResource)
                .where(
                    or_(
                        TenantResource.state.in_(("requested", "provisioning", "cleanup_pending")),
                        TenantResource.transition_kind.is_not(None),
                    ),
                    or_(
                        TenantResource.next_retry_at.is_(None),
                        TenantResource.next_retry_at <= now,
                    ),
                    or_(
                        TenantResource.worker_lease_expires_at.is_(None),
                        TenantResource.worker_lease_expires_at <= now,
                    ),
                )
                .order_by(
                    TenantResource.next_retry_at.asc().nullsfirst(),
                    TenantResource.created_at,
                    TenantResource.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            return await self._claim_locked_row(
                session,
                row,
                worker_id=worker_id,
                lease_duration=lease_duration,
            )

    @staticmethod
    def _lease_predicate(
        lease: ProvisioningLease,
        *,
        now: datetime,
    ) -> tuple[object, ...]:
        return (
            TenantResource.id == lease.resource_id,
            TenantResource.team_id == lease.team_id,
            TenantResource.worker_lease_owner == lease.worker_id,
            TenantResource.fencing_token == lease.fencing_token,
            TenantResource.worker_lease_expires_at > now,
        )

    async def read(self, lease: ProvisioningLease) -> TenantResourceRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(TenantResource).where(*self._lease_predicate(lease, now=self._clock()))
            )
            if row is None:
                raise ProvisioningLeaseLost
            return self._record(row)

    async def activate(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        owner_user_id = record.requested_owner_user_id
        if (
            owner_user_id is None
            or record.state != "active"
            or record.provisioning_step != "active"
        ):
            raise ProvisioningInterrupted
        values = {name: getattr(record, name) for name in _MUTABLE_RECORD_FIELD_NAMES}
        values["version"] = record.version + 1
        async with self._session_factory() as session, session.begin():
            active_owner_id = await session.scalar(
                select(User.id).where(
                    User.id == owner_user_id,
                    User.state == "active",
                )
            )
            if active_owner_id is None:
                raise ProvisioningInterrupted
            row = await session.scalar(
                update(TenantResource)
                .where(
                    *self._lease_predicate(lease, now=self._clock()),
                    TenantResource.version == record.version,
                )
                .values(**values)
                .returning(TenantResource)
            )
            if row is None:
                raise ProvisioningLeaseLost
            await session.execute(
                postgresql_insert(Membership)
                .values(
                    team_id=record.team_id,
                    user_id=owner_user_id,
                    role="team_owner",
                )
                .on_conflict_do_nothing(index_elements=(Membership.team_id, Membership.user_id))
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    team_id=record.team_id,
                    event_type="tenant.provisioned",
                    target_type="tenant_resource",
                    target_id=record.id,
                    request_id=f"provisioner:{record.id.hex}",
                    outcome="succeeded",
                    details={
                        "owner_user_id": str(owner_user_id),
                        "resource_version": record.resource_version,
                    },
                )
            )
            await session.flush()
            return self._record(row)

    async def save(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        values = {name: getattr(record, name) for name in _MUTABLE_RECORD_FIELD_NAMES}
        values["version"] = record.version + 1
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                update(TenantResource)
                .where(
                    *self._lease_predicate(lease, now=self._clock()),
                    TenantResource.version == record.version,
                )
                .values(**values)
                .returning(TenantResource)
            )
            if row is None:
                raise ProvisioningLeaseLost
            return self._record(row)

    async def assert_lease(self, lease: ProvisioningLease) -> None:
        await self.read(lease)

    async def renew(
        self,
        lease: ProvisioningLease,
        *,
        lease_duration: timedelta,
    ) -> None:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease duration must be positive")
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            resource_id = await session.scalar(
                update(TenantResource)
                .where(*self._lease_predicate(lease, now=now))
                .values(worker_lease_expires_at=now + lease_duration)
                .returning(TenantResource.id)
            )
            if resource_id is None:
                raise ProvisioningLeaseLost

    async def release(self, lease: ProvisioningLease) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(TenantResource)
                .where(
                    TenantResource.id == lease.resource_id,
                    TenantResource.team_id == lease.team_id,
                    TenantResource.worker_lease_owner == lease.worker_id,
                    TenantResource.fencing_token == lease.fencing_token,
                )
                .values(
                    worker_lease_owner=None,
                    worker_lease_expires_at=None,
                    version=TenantResource.version + 1,
                )
            )


class Provisioner:
    def __init__(
        self,
        *,
        repository: ProvisioningRepository,
        postgres: PostgresTenantAdmin,
        secret_store: SecretStore,
        migrator: TenantMigrator,
        bucket_admin: BucketAdmin,
        tenant_router: TenantRouterLifecycle,
        replicator: TenantReplicator,
        sites_origin: str,
        password_source: Callable[[], bytes] | None = None,
        token_source: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
        retirement_grace: timedelta = _DEFAULT_RETIREMENT_GRACE,
    ) -> None:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease duration must be positive")
        if retirement_grace.total_seconds() <= 0:
            raise ValueError("retirement grace must be positive")
        self._repository = repository
        self._postgres = postgres
        self._secret_store = secret_store
        self._migrator = migrator
        self._bucket_admin = bucket_admin
        self._tenant_router = tenant_router
        self._replicator = replicator
        self._bucket_policy = BucketPolicy.raw_artifacts(sites_origin=sites_origin)
        self._password_source = password_source or (
            lambda: secrets.token_urlsafe(32).encode("ascii")
        )
        self._token_source = token_source or (lambda: secrets.token_hex(32))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_duration = lease_duration
        self._retirement_grace = retirement_grace

    def _token(self) -> str:
        token = self._token_source()
        if not _TOKEN_PATTERN.fullmatch(token):
            raise ProvisioningInterrupted
        return token

    def _password(self) -> bytes:
        password = self._password_source()
        if not isinstance(password, bytes) or len(password) < 16:
            raise ProvisioningInterrupted
        return password

    async def _save(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
        **changes: object,
    ) -> TenantResourceRecord:
        return await self._repository.save(lease, replace(record, **changes))

    async def _external(
        self,
        lease: ProvisioningLease,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        await self._repository.assert_lease(lease)
        operation_task = asyncio.ensure_future(operation())
        heartbeat_task = asyncio.create_task(self._renew_lease(lease))
        try:
            completed, _ = await asyncio.wait(
                (operation_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in completed:
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is not None:
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    raise heartbeat_error
            result = await operation_task
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        await self._repository.assert_lease(lease)
        return result

    async def _renew_lease(self, lease: ProvisioningLease) -> None:
        interval_seconds = max(self._lease_duration.total_seconds() / 3, 0.001)
        while True:
            await asyncio.sleep(interval_seconds)
            await self._repository.renew(
                lease,
                lease_duration=self._lease_duration,
            )

    async def _ensure_role_secret(
        self,
        lease: ProvisioningLease,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
        reference: str,
        context: SecretContext,
    ) -> bytes:
        try:
            password = await self._external(
                lease,
                lambda: self._secret_store.get(reference, context=context),
            )
            secret_exists = True
        except SecretNotFoundError:
            password = self._password()
            secret_exists = False
        await self._external(
            lease,
            lambda: self._postgres.set_role_password(
                database_name=database_name,
                role_name=role_name,
                password=password,
                ownership_receipt=ownership_receipt,
            ),
        )
        if not secret_exists:
            stored_reference = await self._external(
                lease,
                lambda: self._secret_store.put(
                    password,
                    context=context,
                    reference=reference,
                ),
            )
            if not secrets.compare_digest(stored_reference, reference):
                raise ProvisioningInterrupted
        return password

    @staticmethod
    def _required(value: _T | None) -> _T:
        if value is None:
            raise ProvisioningInterrupted
        return value

    @staticmethod
    def _secret_context(
        record: TenantResourceRecord,
        *,
        credential_version: int,
        migration: bool,
    ) -> SecretContext:
        return SecretContext(
            team_id=record.team_id,
            resource_id=record.id,
            credential_version=credential_version,
            purpose=(
                "tenant_database_migration_password" if migration else "tenant_database_password"
            ),
        )

    @staticmethod
    def _route(record: TenantResourceRecord, *, pending: bool = False) -> TenantRoute:
        return TenantRoute(
            team_id=record.team_id,
            resource_id=record.id,
            resource_version=Provisioner._required(
                record.pending_resource_version if pending else record.resource_version
            ),
            credential_version=Provisioner._required(
                record.pending_credential_version if pending else record.credential_version
            ),
            database_name=Provisioner._required(
                (record.pending_database_name or record.database_name)
                if pending
                else record.database_name
            ),
            database_role_name=Provisioner._required(
                record.pending_database_role_name if pending else record.database_role_name
            ),
            database_secret_ref=Provisioner._required(
                record.pending_database_secret_ref if pending else record.database_secret_ref
            ),
            write_paused=False,
        )

    async def provision(
        self,
        team_id: UUID,
        idempotency_key: str,
        *,
        worker_id: str = "provisioner-inline",
    ) -> TenantResourceRecord:
        lease, record = await self._repository.claim_for_request(
            team_id=team_id,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            lease_duration=self._lease_duration,
        )
        try:
            return await self._process_initial_claim(lease, record)
        finally:
            await self._repository.release(lease)

    async def process_next(
        self,
        *,
        worker_id: str,
    ) -> TenantResourceRecord | None:
        claimed = await self._repository.claim_next(
            worker_id=worker_id,
            lease_duration=self._lease_duration,
        )
        if claimed is None:
            return None
        lease, record = claimed
        try:
            if record.transition_kind == "credential_rotation":
                try:
                    return await self._resume_credential_rotation(lease, record)
                except ProvisioningLeaseLost:
                    raise
                except Exception as exc:
                    raise ProvisioningInterrupted from exc
            if record.transition_kind == "resource_migration":
                try:
                    return await self._resume_resource_migration(lease, record)
                except ProvisioningLeaseLost:
                    raise
                except Exception as exc:
                    current = await self._repository.read(lease)
                    if current.transition_step in {
                        "switched",
                        "old_pool_closed",
                        "retirement_wait",
                    }:
                        raise ProvisioningInterrupted from exc
                    return await self._rollback_migration(lease, current)
            return await self._process_initial_claim(lease, record)
        finally:
            await self._repository.release(lease)

    async def _process_initial_claim(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        if record.state == "cleanup_pending":
            await self._compensate_initial(lease, record)
            record = await self._repository.read(lease)
            record = await self._reset_after_cleanup(lease, record)
        if record.state == "active":
            return record
        if record.state not in {"requested", "provisioning"}:
            raise ProvisioningInterrupted
        try:
            return await self._resume_initial_provisioning(lease, record)
        except ProvisioningLeaseLost:
            raise
        except Exception:
            record = await self._repository.read(lease)
            record = await self._save(
                lease,
                record,
                state="cleanup_pending",
                provisioning_step="cleanup",
                transition_step="cleanup_started",
                last_error_code="tenant_provisioning_failed",
                next_retry_at=self._clock() + timedelta(seconds=30),
            )
            try:
                await self._compensate_initial(lease, record)
            except ProvisioningLeaseLost:
                raise
            except Exception:
                pass
            return await self._repository.read(lease)

    async def _resume_initial_provisioning(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        if record.provisioning_step == "requested":
            ownership_receipt = self._token()
            bucket_ownership_receipt = self._token()
            database_suffix = ownership_receipt[-20:]
            record = await self._save(
                lease,
                record,
                state="provisioning",
                provisioning_step="database_allocated",
                database_name=f"pp_t_{database_suffix}",
                database_owner_role_name=f"pp_o_{database_suffix}",
                database_migration_role_name=f"pp_m_{database_suffix}",
                database_role_name=f"pp_r_{database_suffix}",
                bucket_name=f"pp-{bucket_ownership_receipt[-32:]}",
                database_ownership_receipt=ownership_receipt,
                role_ownership_receipt=ownership_receipt,
                bucket_ownership_receipt=bucket_ownership_receipt,
                last_error_code=None,
                next_retry_at=None,
                transition_step=None,
            )
        if record.provisioning_step == "database_allocated":
            await self._external(
                lease,
                lambda: self._postgres.ensure_database(
                    self._required(record.database_name),
                    self._required(record.database_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="database_created",
            )
        if record.provisioning_step == "database_created":
            record = await self._save(
                lease,
                record,
                provisioning_step="roles_allocated",
            )
        if record.provisioning_step == "roles_allocated":
            await self._external(
                lease,
                lambda: self._postgres.ensure_role_set(
                    database_name=self._required(record.database_name),
                    owner_role_name=self._required(record.database_owner_role_name),
                    migration_role_name=self._required(record.database_migration_role_name),
                    runtime_role_name=self._required(record.database_role_name),
                    ownership_receipt=self._required(record.role_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="roles_created",
            )
        if record.provisioning_step == "roles_created":
            if record.database_migration_secret_ref is None:
                record = await self._save(
                    lease,
                    record,
                    database_migration_secret_ref=self._secret_store.allocate_reference(),
                )
            await self._ensure_role_secret(
                lease,
                database_name=self._required(record.database_name),
                role_name=self._required(record.database_migration_role_name),
                ownership_receipt=self._required(record.role_ownership_receipt),
                reference=self._required(record.database_migration_secret_ref),
                context=self._secret_context(
                    record,
                    credential_version=record.credential_version,
                    migration=True,
                ),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="migration_credential_stored",
            )
        if record.provisioning_step == "migration_credential_stored":
            if record.database_secret_ref is None:
                record = await self._save(
                    lease,
                    record,
                    database_secret_ref=self._secret_store.allocate_reference(),
                )
            await self._ensure_role_secret(
                lease,
                database_name=self._required(record.database_name),
                role_name=self._required(record.database_role_name),
                ownership_receipt=self._required(record.role_ownership_receipt),
                reference=self._required(record.database_secret_ref),
                context=self._secret_context(
                    record,
                    credential_version=record.credential_version,
                    migration=False,
                ),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="credentials_stored",
            )
        if record.provisioning_step == "credentials_stored":
            migration_password = await self._external(
                lease,
                lambda: self._secret_store.get(
                    self._required(record.database_migration_secret_ref),
                    context=self._secret_context(
                        record,
                        credential_version=record.credential_version,
                        migration=True,
                    ),
                ),
            )
            revision = await self._external(
                lease,
                lambda: self._migrator.migrate(
                    database_name=self._required(record.database_name),
                    migration_role_name=self._required(record.database_migration_role_name),
                    runtime_role_name=self._required(record.database_role_name),
                    password=migration_password,
                ),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="tenant_migrated",
                database_migration_revision=revision,
            )
        if record.provisioning_step == "tenant_migrated":
            record = await self._save(
                lease,
                record,
                provisioning_step="bucket_allocated",
            )
        if record.provisioning_step == "bucket_allocated":
            await self._external(
                lease,
                lambda: self._bucket_admin.ensure_bucket(
                    self._required(record.bucket_name),
                    self._required(record.bucket_ownership_receipt),
                    self._bucket_policy,
                ),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="bucket_created",
            )
        if record.provisioning_step == "bucket_created":
            await self._external(
                lease,
                lambda: self._tenant_router.validate_route(self._route(record)),
            )
            record = await self._save(
                lease,
                record,
                provisioning_step="route_validated",
            )
        if record.provisioning_step == "route_validated":
            record = await self._repository.activate(
                lease,
                replace(
                    record,
                    state="active",
                    provisioning_step="active",
                    transition_step=None,
                ),
            )
        return record

    async def _compensate_initial(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        errors: list[Exception] = []

        async def attempt(step: str, operation: Callable[[], Awaitable[object]]) -> None:
            nonlocal record
            try:
                await self._external(lease, operation)
                record = await self._save(lease, record, transition_step=step)
            except ProvisioningLeaseLost:
                raise
            except Exception as exc:
                errors.append(exc)

        if record.bucket_name and record.bucket_ownership_receipt:
            await attempt(
                "cleanup_bucket_deleted",
                lambda: self._bucket_admin.delete_bucket(
                    self._required(record.bucket_name),
                    self._required(record.bucket_ownership_receipt),
                ),
            )
        if record.database_secret_ref:
            await attempt(
                "cleanup_runtime_secret_deleted",
                lambda: self._secret_store.delete(self._required(record.database_secret_ref)),
            )
        if record.database_migration_secret_ref:
            await attempt(
                "cleanup_migration_secret_deleted",
                lambda: self._secret_store.delete(
                    self._required(record.database_migration_secret_ref)
                ),
            )
        if (
            record.database_name
            and record.database_owner_role_name
            and record.database_migration_role_name
            and record.database_role_name
            and record.role_ownership_receipt
        ):
            await attempt(
                "cleanup_roles_deleted",
                lambda: self._postgres.delete_role_set(
                    database_name=self._required(record.database_name),
                    owner_role_name=self._required(record.database_owner_role_name),
                    migration_role_name=self._required(record.database_migration_role_name),
                    runtime_role_name=self._required(record.database_role_name),
                    ownership_receipt=self._required(record.role_ownership_receipt),
                ),
            )
        if record.database_name and record.database_ownership_receipt:
            await attempt(
                "cleanup_database_deleted",
                lambda: self._postgres.delete_database(
                    self._required(record.database_name),
                    self._required(record.database_ownership_receipt),
                ),
            )
        if errors:
            raise ProvisioningInterrupted from errors[-1]
        return record

    async def _reset_after_cleanup(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        return await self._save(
            lease,
            record,
            state="requested",
            provisioning_step="requested",
            database_name=None,
            database_owner_role_name=None,
            database_migration_role_name=None,
            database_migration_secret_ref=None,
            database_role_name=None,
            database_secret_ref=None,
            database_migration_revision=None,
            bucket_name=None,
            database_ownership_receipt=None,
            role_ownership_receipt=None,
            bucket_ownership_receipt=None,
            retry_count=record.retry_count + 1,
            next_retry_at=None,
            transition_step=None,
        )

    async def rotate_credentials(
        self,
        team_id: UUID,
        *,
        worker_id: str = "provisioner-credential-rotation",
    ) -> TenantResourceRecord:
        lease, record = await self._repository.claim_team(
            team_id=team_id,
            worker_id=worker_id,
            lease_duration=self._lease_duration,
        )
        try:
            if record.state != "active" or record.transition_kind not in {
                None,
                "credential_rotation",
            }:
                raise ProvisioningInterrupted
            if record.transition_kind is None:
                record = await self._save(
                    lease,
                    record,
                    transition_kind="credential_rotation",
                    transition_step="role_allocated",
                    pending_resource_version=record.resource_version + 1,
                    pending_credential_version=record.credential_version + 1,
                    pending_database_role_name=f"pp_r_{self._token()[-20:]}",
                    pending_role_ownership_receipt=self._required(record.role_ownership_receipt),
                )
            try:
                return await self._resume_credential_rotation(lease, record)
            except ProvisioningLeaseLost:
                raise
            except Exception as exc:
                raise ProvisioningInterrupted from exc
        finally:
            await self._repository.release(lease)

    async def _resume_credential_rotation(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        if record.transition_step == "role_allocated":
            await self._external(
                lease,
                lambda: self._postgres.ensure_runtime_role(
                    database_name=self._required(record.database_name),
                    role_name=self._required(record.pending_database_role_name),
                    ownership_receipt=self._required(record.pending_role_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="role_created",
            )
        if record.transition_step == "role_created":
            if record.pending_database_secret_ref is None:
                record = await self._save(
                    lease,
                    record,
                    pending_database_secret_ref=self._secret_store.allocate_reference(),
                )
            await self._ensure_role_secret(
                lease,
                database_name=self._required(record.database_name),
                role_name=self._required(record.pending_database_role_name),
                ownership_receipt=self._required(record.pending_role_ownership_receipt),
                reference=self._required(record.pending_database_secret_ref),
                context=self._secret_context(
                    record,
                    credential_version=self._required(record.pending_credential_version),
                    migration=False,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="credential_stored",
            )
        if record.transition_step == "credential_stored":
            pending_route = replace(
                self._route(record, pending=True),
                database_name=self._required(record.database_name),
            )
            await self._external(
                lease,
                lambda: self._tenant_router.validate_route(pending_route),
            )
            record = await self._save(
                lease,
                record,
                transition_step="validated",
            )
        if record.transition_step == "validated":
            record = await self._save(
                lease,
                record,
                transition_step="switched",
                previous_resource_version=record.resource_version,
                previous_database_role_name=record.database_role_name,
                previous_database_secret_ref=record.database_secret_ref,
                previous_credential_version=record.credential_version,
                previous_role_ownership_receipt=record.role_ownership_receipt,
                resource_version=self._required(record.pending_resource_version),
                database_role_name=self._required(record.pending_database_role_name),
                database_secret_ref=self._required(record.pending_database_secret_ref),
                credential_version=self._required(record.pending_credential_version),
                pending_resource_version=None,
                pending_database_role_name=None,
                pending_database_secret_ref=None,
                pending_credential_version=None,
                pending_role_ownership_receipt=None,
            )
        if record.transition_step == "switched":
            await self._external(
                lease,
                lambda: self._tenant_router.dispose_team(
                    record.team_id,
                    keep_resource_version=record.resource_version,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="old_pool_closed",
            )
        if record.transition_step == "old_pool_closed":
            record = await self._save(
                lease,
                record,
                transition_step="retirement_wait",
                next_retry_at=self._clock() + self._retirement_grace,
            )
        if record.transition_step == "retirement_wait":
            if record.next_retry_at is not None and self._clock() < record.next_retry_at:
                return record
            await self._external(
                lease,
                lambda: self._tenant_router.dispose_team(
                    record.team_id,
                    keep_resource_version=record.resource_version,
                ),
            )
            await self._external(
                lease,
                lambda: self._postgres.revoke_runtime_role(
                    database_name=self._required(record.database_name),
                    role_name=self._required(record.previous_database_role_name),
                    ownership_receipt=self._required(record.previous_role_ownership_receipt),
                ),
            )
            if record.previous_database_secret_ref:
                await self._external(
                    lease,
                    lambda: self._secret_store.delete(
                        self._required(record.previous_database_secret_ref)
                    ),
                )
            record = await self._save(
                lease,
                record,
                transition_kind=None,
                transition_step=None,
                previous_resource_version=None,
                previous_database_role_name=None,
                previous_database_secret_ref=None,
                previous_credential_version=None,
                previous_role_ownership_receipt=None,
                next_retry_at=None,
            )
        return record

    async def migrate_team(
        self,
        team_id: UUID,
        *,
        worker_id: str = "provisioner-resource-migration",
    ) -> TenantResourceRecord:
        lease, record = await self._repository.claim_team(
            team_id=team_id,
            worker_id=worker_id,
            lease_duration=self._lease_duration,
        )
        try:
            if record.state not in {"active", "migrating"} or record.transition_kind not in {
                None,
                "resource_migration",
            }:
                raise ProvisioningInterrupted
            if record.transition_kind is None:
                record = await self._allocate_migration(lease, record)
            try:
                return await self._resume_resource_migration(lease, record)
            except ProvisioningLeaseLost:
                raise
            except Exception as exc:
                current = await self._repository.read(lease)
                if current.transition_step in {
                    "switched",
                    "old_pool_closed",
                    "retirement_wait",
                }:
                    raise ProvisioningInterrupted from exc
                return await self._rollback_migration(lease, current)
        finally:
            await self._repository.release(lease)

    async def _allocate_migration(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        ownership_receipt = self._token()
        database_suffix = ownership_receipt[-20:]
        return await self._save(
            lease,
            record,
            state="migrating",
            write_paused=True,
            transition_kind="resource_migration",
            transition_step="target_allocated",
            pending_resource_version=record.resource_version + 1,
            pending_credential_version=record.credential_version + 1,
            pending_database_name=f"pp_t_{database_suffix}",
            pending_database_owner_role_name=f"pp_o_{database_suffix}",
            pending_database_migration_role_name=f"pp_m_{database_suffix}",
            pending_database_role_name=f"pp_r_{database_suffix}",
            pending_database_ownership_receipt=ownership_receipt,
            pending_role_ownership_receipt=ownership_receipt,
        )

    async def _resume_resource_migration(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        if record.transition_step == "target_allocated":
            await self._external(
                lease,
                lambda: self._postgres.ensure_database(
                    self._required(record.pending_database_name),
                    self._required(record.pending_database_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="target_database_created",
            )
        if record.transition_step == "target_database_created":
            await self._external(
                lease,
                lambda: self._postgres.ensure_role_set(
                    database_name=self._required(record.pending_database_name),
                    owner_role_name=self._required(record.pending_database_owner_role_name),
                    migration_role_name=self._required(record.pending_database_migration_role_name),
                    runtime_role_name=self._required(record.pending_database_role_name),
                    ownership_receipt=self._required(record.pending_role_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="target_roles_created",
            )
        if record.transition_step == "target_roles_created":
            if record.pending_database_migration_secret_ref is None:
                record = await self._save(
                    lease,
                    record,
                    pending_database_migration_secret_ref=(self._secret_store.allocate_reference()),
                )
            await self._ensure_role_secret(
                lease,
                database_name=self._required(record.pending_database_name),
                role_name=self._required(record.pending_database_migration_role_name),
                ownership_receipt=self._required(record.pending_role_ownership_receipt),
                reference=self._required(record.pending_database_migration_secret_ref),
                context=self._secret_context(
                    record,
                    credential_version=self._required(record.pending_credential_version),
                    migration=True,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="target_migration_credential_stored",
            )
        if record.transition_step == "target_migration_credential_stored":
            if record.pending_database_secret_ref is None:
                record = await self._save(
                    lease,
                    record,
                    pending_database_secret_ref=self._secret_store.allocate_reference(),
                )
            await self._ensure_role_secret(
                lease,
                database_name=self._required(record.pending_database_name),
                role_name=self._required(record.pending_database_role_name),
                ownership_receipt=self._required(record.pending_role_ownership_receipt),
                reference=self._required(record.pending_database_secret_ref),
                context=self._secret_context(
                    record,
                    credential_version=self._required(record.pending_credential_version),
                    migration=False,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="target_credentials_stored",
            )
        if record.transition_step == "target_credentials_stored":
            target_password = await self._external(
                lease,
                lambda: self._secret_store.get(
                    self._required(record.pending_database_migration_secret_ref),
                    context=self._secret_context(
                        record,
                        credential_version=self._required(record.pending_credential_version),
                        migration=True,
                    ),
                ),
            )
            revision = await self._external(
                lease,
                lambda: self._migrator.migrate(
                    database_name=self._required(record.pending_database_name),
                    migration_role_name=self._required(record.pending_database_migration_role_name),
                    runtime_role_name=self._required(record.pending_database_role_name),
                    password=target_password,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="target_migrated",
                pending_database_migration_revision=revision,
            )
        if record.transition_step == "target_migrated":
            await self._external(
                lease,
                lambda: self._postgres.pause_runtime_writes(
                    database_name=self._required(record.database_name),
                    role_name=self._required(record.database_role_name),
                    ownership_receipt=self._required(record.role_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="source_writes_paused",
            )
        if record.transition_step == "source_writes_paused":
            source_password = await self._external(
                lease,
                lambda: self._secret_store.get(
                    self._required(record.database_migration_secret_ref),
                    context=self._secret_context(
                        record,
                        credential_version=record.credential_version,
                        migration=True,
                    ),
                ),
            )
            target_password = await self._external(
                lease,
                lambda: self._secret_store.get(
                    self._required(record.pending_database_migration_secret_ref),
                    context=self._secret_context(
                        record,
                        credential_version=self._required(record.pending_credential_version),
                        migration=True,
                    ),
                ),
            )
            await self._external(
                lease,
                lambda: self._replicator.copy_and_validate(
                    source_database_name=self._required(record.database_name),
                    source_migration_role_name=self._required(record.database_migration_role_name),
                    source_password=source_password,
                    target_database_name=self._required(record.pending_database_name),
                    target_migration_role_name=self._required(
                        record.pending_database_migration_role_name
                    ),
                    target_password=target_password,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="copy_validated",
            )
        if record.transition_step == "copy_validated":
            await self._external(
                lease,
                lambda: self._tenant_router.validate_route(self._route(record, pending=True)),
            )
            record = await self._save(
                lease,
                record,
                transition_step="route_validated",
            )
        if record.transition_step == "route_validated":
            record = await self._switch_migrated_resource(lease, record)
        if record.transition_step == "switched":
            await self._external(
                lease,
                lambda: self._tenant_router.dispose_team(
                    record.team_id,
                    keep_resource_version=record.resource_version,
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="old_pool_closed",
            )
        if record.transition_step == "old_pool_closed":
            await self._external(
                lease,
                lambda: self._postgres.make_database_read_only(
                    database_name=self._required(record.previous_database_name),
                    ownership_receipt=self._required(record.previous_database_ownership_receipt),
                ),
            )
            record = await self._save(
                lease,
                record,
                transition_step="retirement_wait",
                write_paused=False,
                next_retry_at=self._clock() + self._retirement_grace,
            )
        if record.transition_step == "retirement_wait":
            if record.next_retry_at is not None and self._clock() < record.next_retry_at:
                return record
            await self._external(
                lease,
                lambda: self._tenant_router.dispose_team(
                    record.team_id,
                    keep_resource_version=record.resource_version,
                ),
            )
            await self._external(
                lease,
                lambda: self._postgres.delete_role_set(
                    database_name=self._required(record.previous_database_name),
                    owner_role_name=self._required(record.previous_database_owner_role_name),
                    migration_role_name=self._required(
                        record.previous_database_migration_role_name
                    ),
                    runtime_role_name=self._required(record.previous_database_role_name),
                    ownership_receipt=self._required(record.previous_role_ownership_receipt),
                ),
            )
            await self._external(
                lease,
                lambda: self._postgres.delete_database(
                    self._required(record.previous_database_name),
                    self._required(record.previous_database_ownership_receipt),
                ),
            )
            for reference in (
                record.previous_database_secret_ref,
                record.previous_database_migration_secret_ref,
            ):
                if reference:
                    await self._external(
                        lease,
                        lambda reference=reference: self._secret_store.delete(reference),
                    )
            record = await self._save(
                lease,
                record,
                transition_kind=None,
                transition_step=None,
                previous_resource_version=None,
                previous_database_name=None,
                previous_database_owner_role_name=None,
                previous_database_migration_role_name=None,
                previous_database_migration_secret_ref=None,
                previous_database_role_name=None,
                previous_database_secret_ref=None,
                previous_credential_version=None,
                previous_database_migration_revision=None,
                previous_database_ownership_receipt=None,
                previous_role_ownership_receipt=None,
                next_retry_at=None,
            )
        return record

    async def _switch_migrated_resource(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        return await self._save(
            lease,
            record,
            state="active",
            transition_step="switched",
            previous_resource_version=record.resource_version,
            previous_database_name=record.database_name,
            previous_database_owner_role_name=record.database_owner_role_name,
            previous_database_migration_role_name=record.database_migration_role_name,
            previous_database_migration_secret_ref=record.database_migration_secret_ref,
            previous_database_role_name=record.database_role_name,
            previous_database_secret_ref=record.database_secret_ref,
            previous_credential_version=record.credential_version,
            previous_database_migration_revision=record.database_migration_revision,
            previous_database_ownership_receipt=record.database_ownership_receipt,
            previous_role_ownership_receipt=record.role_ownership_receipt,
            resource_version=self._required(record.pending_resource_version),
            database_name=self._required(record.pending_database_name),
            database_owner_role_name=self._required(record.pending_database_owner_role_name),
            database_migration_role_name=self._required(
                record.pending_database_migration_role_name
            ),
            database_migration_secret_ref=self._required(
                record.pending_database_migration_secret_ref
            ),
            database_role_name=self._required(record.pending_database_role_name),
            database_secret_ref=self._required(record.pending_database_secret_ref),
            credential_version=self._required(record.pending_credential_version),
            database_migration_revision=self._required(record.pending_database_migration_revision),
            database_ownership_receipt=self._required(record.pending_database_ownership_receipt),
            role_ownership_receipt=self._required(record.pending_role_ownership_receipt),
            pending_resource_version=None,
            pending_database_name=None,
            pending_database_owner_role_name=None,
            pending_database_migration_role_name=None,
            pending_database_migration_secret_ref=None,
            pending_database_role_name=None,
            pending_database_secret_ref=None,
            pending_credential_version=None,
            pending_database_migration_revision=None,
            pending_database_ownership_receipt=None,
            pending_role_ownership_receipt=None,
        )

    async def _rollback_migration(
        self,
        lease: ProvisioningLease,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        await self._external(
            lease,
            lambda: self._postgres.resume_runtime_writes(
                database_name=self._required(record.database_name),
                role_name=self._required(record.database_role_name),
                ownership_receipt=self._required(record.role_ownership_receipt),
            ),
        )
        if record.pending_database_secret_ref:
            await self._external(
                lease,
                lambda: self._secret_store.delete(
                    self._required(record.pending_database_secret_ref)
                ),
            )
        if record.pending_database_migration_secret_ref:
            await self._external(
                lease,
                lambda: self._secret_store.delete(
                    self._required(record.pending_database_migration_secret_ref)
                ),
            )
        if (
            record.pending_database_name
            and record.pending_database_owner_role_name
            and record.pending_database_migration_role_name
            and record.pending_database_role_name
            and record.pending_role_ownership_receipt
        ):
            await self._external(
                lease,
                lambda: self._postgres.delete_role_set(
                    database_name=self._required(record.pending_database_name),
                    owner_role_name=self._required(record.pending_database_owner_role_name),
                    migration_role_name=self._required(record.pending_database_migration_role_name),
                    runtime_role_name=self._required(record.pending_database_role_name),
                    ownership_receipt=self._required(record.pending_role_ownership_receipt),
                ),
            )
        if record.pending_database_name and record.pending_database_ownership_receipt:
            await self._external(
                lease,
                lambda: self._postgres.delete_database(
                    self._required(record.pending_database_name),
                    self._required(record.pending_database_ownership_receipt),
                ),
            )
        return await self._save(
            lease,
            record,
            state="active",
            write_paused=False,
            transition_kind=None,
            transition_step=None,
            last_error_code="tenant_migration_failed",
            pending_resource_version=None,
            pending_database_name=None,
            pending_database_owner_role_name=None,
            pending_database_migration_role_name=None,
            pending_database_migration_secret_ref=None,
            pending_database_role_name=None,
            pending_database_secret_ref=None,
            pending_credential_version=None,
            pending_database_migration_revision=None,
            pending_database_ownership_receipt=None,
            pending_role_ownership_receipt=None,
        )


class AdminSessionInvalid(RuntimeError):
    def __init__(self) -> None:
        super().__init__("administrator session is invalid")


class AdminCsrfInvalid(RuntimeError):
    def __init__(self) -> None:
        super().__init__("administrator csrf token is invalid")


class AdminNotPlatformAdministrator(RuntimeError):
    def __init__(self) -> None:
        super().__init__("platform administrator permission is required")


class AdminOwnerNotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("active owner user was not found")


class AdminTeamNotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("team provisioning status was not found")


class AdminIdempotencyConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("idempotency key was reused with another request")


class AdminRequestInvalid(RuntimeError):
    def __init__(self) -> None:
        super().__init__("administrator team request is invalid")


@dataclass(frozen=True, slots=True)
class AdminTeamResult:
    team_id: UUID
    team_name: str
    resource_id: UUID
    resource_state: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class AdminTeamStatus:
    team_id: UUID
    team_name: str
    team_state: str
    resource_state: str
    provisioning_step: str
    transition_kind: str | None
    transition_step: str | None
    retry_count: int
    next_retry_at: datetime | None
    last_error_code: str | None
    resource_version: int
    credential_version: int
    write_paused: bool


_ADMIN_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class AdminTeamService:
    """Atomically authorize and enqueue one actor-scoped tenant creation request."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _request_hash(*, name: str, owner_user_id: UUID) -> str:
        canonical = json.dumps(
            {"name": name, "owner_user_id": str(owner_user_id)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _authenticate(
        self,
        session: AsyncSession,
        *,
        session_token: str,
        csrf_token: str | None,
        now: datetime,
    ) -> tuple[AuthSession, User]:
        stored = await session.scalar(
            select(AuthSession)
            .where(AuthSession.token_digest == digest_session_token(session_token))
            .with_for_update()
        )
        if (
            stored is None
            or stored.kind != "authenticated"
            or stored.user_id is None
            or stored.revoked_at is not None
            or stored.absolute_expires_at <= now
            or stored.last_seen_at + timedelta(hours=12) < now
        ):
            raise AdminSessionInvalid
        actor = await session.get(User, stored.user_id)
        if actor is None or actor.state != "active":
            raise AdminSessionInvalid
        if csrf_token is not None and not verify_csrf_token(
            csrf_token,
            stored.csrf_secret_hash,
        ):
            raise AdminCsrfInvalid
        if not actor.is_platform_admin:
            raise AdminNotPlatformAdministrator
        return stored, actor

    async def get_team_status(
        self,
        *,
        session_token: str,
        team_id: UUID,
    ) -> AdminTeamStatus:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            stored_session, _ = await self._authenticate(
                session,
                session_token=session_token,
                csrf_token=None,
                now=now,
            )
            team = await session.get(Team, team_id)
            resource = await session.scalar(
                select(TenantResource).where(TenantResource.team_id == team_id)
            )
            if team is None or resource is None:
                raise AdminTeamNotFound
            stored_session.last_seen_at = now
            return AdminTeamStatus(
                team_id=team.id,
                team_name=team.name,
                team_state=team.state,
                resource_state=resource.state,
                provisioning_step=resource.provisioning_step,
                transition_kind=resource.transition_kind,
                transition_step=resource.transition_step,
                retry_count=resource.retry_count,
                next_retry_at=resource.next_retry_at,
                last_error_code=resource.last_error_code,
                resource_version=resource.resource_version,
                credential_version=resource.credential_version,
                write_paused=resource.write_paused,
            )

    async def create_team(
        self,
        *,
        session_token: str,
        csrf_token: str,
        idempotency_key: str,
        name: str,
        owner_user_id: UUID,
        request_id: str,
    ) -> AdminTeamResult:
        canonical_name = name.strip()
        if (
            not canonical_name
            or len(canonical_name) > 200
            or not _ADMIN_IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key)
            or not _REQUEST_ID_PATTERN.fullmatch(request_id)
        ):
            raise AdminRequestInvalid
        now = self._clock()
        request_hash = self._request_hash(
            name=canonical_name,
            owner_user_id=owner_user_id,
        )
        async with self._session_factory() as session, session.begin():
            stored_session, actor = await self._authenticate(
                session,
                session_token=session_token,
                csrf_token=csrf_token,
                now=now,
            )
            inserted_key_id = await session.scalar(
                postgresql_insert(IdempotencyKey)
                .values(
                    team_id=None,
                    key=idempotency_key,
                    operation="create_team",
                    scope_type="actor",
                    scope_id=actor.id,
                    request_hash=request_hash,
                    state="pending",
                    response_resource_id=None,
                    expires_at=now + timedelta(hours=24),
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        IdempotencyKey.operation,
                        IdempotencyKey.scope_type,
                        IdempotencyKey.scope_id,
                        IdempotencyKey.key,
                    )
                )
                .returning(IdempotencyKey.id)
            )
            if inserted_key_id is None:
                existing = await session.scalar(
                    select(IdempotencyKey)
                    .where(
                        IdempotencyKey.operation == "create_team",
                        IdempotencyKey.scope_type == "actor",
                        IdempotencyKey.scope_id == actor.id,
                        IdempotencyKey.key == idempotency_key,
                    )
                    .with_for_update()
                )
                if (
                    existing is None
                    or existing.request_hash != request_hash
                    or existing.state != "completed"
                    or existing.team_id is None
                    or existing.response_resource_id is None
                ):
                    raise AdminIdempotencyConflict
                team = await session.get(Team, existing.team_id)
                resource = await session.get(
                    TenantResource,
                    existing.response_resource_id,
                )
                if team is None or resource is None or resource.team_id != team.id:
                    raise AdminIdempotencyConflict
                stored_session.last_seen_at = now
                return AdminTeamResult(
                    team_id=team.id,
                    team_name=team.name,
                    resource_id=resource.id,
                    resource_state=resource.state,
                    replayed=True,
                )

            owner = await session.scalar(
                select(User).where(
                    User.id == owner_user_id,
                    User.state == "active",
                )
            )
            if owner is None:
                raise AdminOwnerNotFound
            team = Team(name=canonical_name, state="active")
            session.add(team)
            await session.flush()
            resource = TenantResource(
                team_id=team.id,
                requested_owner_user_id=owner.id,
                resource_version=1,
                state="requested",
                provisioning_step="requested",
                credential_version=1,
            )
            session.add_all(
                (
                    resource,
                    TenantQuota(
                        team_id=team.id,
                        active_device_limit=2,
                        queued_device_limit=20,
                    ),
                )
            )
            await session.flush()
            stored_key = await session.get(IdempotencyKey, inserted_key_id)
            if stored_key is None:
                raise AdminIdempotencyConflict
            stored_key.team_id = team.id
            stored_key.response_resource_id = resource.id
            stored_key.state = "completed"
            session.add(
                AuditEvent(
                    actor_user_id=actor.id,
                    team_id=team.id,
                    event_type="team.create_requested",
                    target_type="team",
                    target_id=team.id,
                    request_id=request_id,
                    outcome="accepted",
                    details={"owner_user_id": str(owner.id)},
                )
            )
            stored_session.last_seen_at = now
            return AdminTeamResult(
                team_id=team.id,
                team_name=team.name,
                resource_id=resource.id,
                resource_state=resource.state,
                replayed=False,
            )
