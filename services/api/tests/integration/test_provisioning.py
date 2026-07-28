import asyncio
import json
import os
import traceback
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import boto3
from alembic import command
from alembic.config import Config
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from perfpilot_api.config import Settings
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
from perfpilot_api.db.control.session import create_control_session_factory
from perfpilot_api.db.tenant.router import TenantRoute
from perfpilot_api.main import create_app
from perfpilot_api.security.csrf import digest_csrf_token
from perfpilot_api.security.proxy_signature import sign_proxy_request
from perfpilot_api.security.sessions import COOKIE_NAME, digest_session_token
from perfpilot_api.secrets.base import SecretContext, SecretNotFoundError
from perfpilot_api.services.provisioning import (
    AdminIdempotencyConflict,
    AdminNotPlatformAdministrator,
    AdminOwnerNotFound,
    AdminTeamService,
    AlembicTenantMigrator,
    BucketPolicy,
    InMemoryProvisioningRepository,
    Provisioner,
    ProvisioningInterrupted,
    ProvisioningLeaseLost,
    PsycopgTenantAdmin,
    S3BucketAdmin,
    SqlAlchemyProvisioningRepository,
    TenantResourceRecord,
)

TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
RESOURCE_ID = UUID("80000000-0000-4000-8000-000000000001")
IDEMPOTENCY_KEY = "create-team-request-1"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
_API_ROOT = Path(__file__).resolve().parents[2]
_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_TENANT_ADMIN_URL_ENV = "PERFPILOT_TEST_TENANT_ADMIN_URL"


class FakeSecretStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.values: dict[str, tuple[bytes, SecretContext]] = {}
        self.deleted: list[str] = []

    def allocate_reference(self) -> str:
        return f"secret://{uuid4()}"

    async def put(
        self,
        secret: bytes,
        *,
        context: SecretContext,
        reference: str | None = None,
    ) -> str:
        self.events.append(f"secret.put:{context.purpose}")
        resolved_reference = reference or self.allocate_reference()
        existing = self.values.get(resolved_reference)
        if existing is not None and existing[1] != context:
            raise RuntimeError("secret context mismatch")
        self.values[resolved_reference] = (secret, context)
        return resolved_reference

    async def get(self, reference: str, *, context: SecretContext) -> bytes:
        stored = self.values.get(reference)
        if stored is None:
            raise SecretNotFoundError("not found")
        value, stored_context = stored
        if stored_context != context:
            raise RuntimeError("secret context mismatch")
        return value

    async def delete(self, reference: str) -> None:
        self.events.append("secret.delete")
        self.deleted.append(reference)
        self.values.pop(reference, None)


class FakePostgresAdmin:
    def __init__(
        self,
        events: list[str],
        current: Callable[[], TenantResourceRecord],
    ) -> None:
        self.events = events
        self.current = current
        self.deleted_databases: list[str] = []
        self.revoked_roles: list[str] = []
        self.read_only_databases: list[str] = []
        self.paused_roles: list[str] = []
        self.resumed_roles: list[str] = []
        self.passwords: dict[str, bytes] = {}
        self.fail_delete_database_once = False

    async def ensure_database(self, database_name: str, ownership_receipt: str) -> None:
        record = self.current()
        if record.transition_kind == "resource_migration":
            assert record.transition_step == "target_allocated"
            assert (
                record.pending_database_name,
                record.pending_database_ownership_receipt,
            ) == (database_name, ownership_receipt)
        else:
            assert record.provisioning_step == "database_allocated"
            assert (record.database_name, record.database_ownership_receipt) == (
                database_name,
                ownership_receipt,
            )
        self.events.append("postgres.ensure_database")

    async def ensure_role_set(
        self,
        *,
        database_name: str,
        owner_role_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        ownership_receipt: str,
    ) -> None:
        record = self.current()
        if record.transition_kind == "resource_migration":
            assert record.transition_step == "target_database_created"
            assert record.pending_database_name == database_name
            assert record.pending_database_owner_role_name == owner_role_name
            assert record.pending_database_migration_role_name == migration_role_name
            assert record.pending_database_role_name == runtime_role_name
            assert record.pending_role_ownership_receipt == ownership_receipt
        else:
            assert record.provisioning_step == "roles_allocated"
            assert record.database_name == database_name
            assert record.database_owner_role_name == owner_role_name
            assert record.database_migration_role_name == migration_role_name
            assert record.database_role_name == runtime_role_name
            assert record.role_ownership_receipt == ownership_receipt
        self.events.append("postgres.ensure_role_set")

    async def ensure_runtime_role(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        del database_name, role_name, ownership_receipt
        self.events.append("postgres.ensure_runtime_role")

    async def set_role_password(
        self,
        *,
        database_name: str,
        role_name: str,
        password: bytes,
        ownership_receipt: str,
    ) -> None:
        del database_name, ownership_receipt
        assert password.startswith(b"generated-password-")
        self.events.append(f"postgres.set_password:{role_name[:4]}")
        self.passwords[role_name] = password

    async def delete_role_set(
        self,
        *,
        database_name: str,
        owner_role_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        ownership_receipt: str,
    ) -> None:
        del database_name, owner_role_name, migration_role_name, runtime_role_name
        assert ownership_receipt
        self.events.append("postgres.delete_role_set")

    async def revoke_runtime_role(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        del database_name
        assert ownership_receipt
        self.events.append("postgres.revoke_runtime_role")
        self.revoked_roles.append(role_name)

    async def make_database_read_only(
        self,
        *,
        database_name: str,
        ownership_receipt: str,
    ) -> None:
        assert ownership_receipt
        self.events.append("postgres.make_database_read_only")
        self.read_only_databases.append(database_name)

    async def pause_runtime_writes(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        del database_name
        assert ownership_receipt
        self.events.append("postgres.pause_runtime_writes")
        self.paused_roles.append(role_name)

    async def resume_runtime_writes(
        self,
        *,
        database_name: str,
        role_name: str,
        ownership_receipt: str,
    ) -> None:
        del database_name
        assert ownership_receipt
        self.events.append("postgres.resume_runtime_writes")
        self.resumed_roles.append(role_name)

    async def delete_database(
        self,
        database_name: str,
        ownership_receipt: str,
    ) -> None:
        assert ownership_receipt
        self.events.append("postgres.delete_database")
        if self.fail_delete_database_once:
            self.fail_delete_database_once = False
            raise RuntimeError("injected database cleanup failure")
        self.deleted_databases.append(database_name)


class FakeTenantMigrator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def migrate(
        self,
        *,
        database_name: str,
        migration_role_name: str,
        runtime_role_name: str,
        password: bytes,
    ) -> str:
        del database_name, migration_role_name, runtime_role_name
        assert password.startswith(b"generated-password-")
        self.events.append("migrator.migrate")
        return "0001_tenant_schema"


class FakeBucketAdmin:
    def __init__(
        self,
        events: list[str],
        current: Callable[[], TenantResourceRecord],
    ) -> None:
        self.events = events
        self.current = current
        self.fail_ensure = False
        self.policies: list[BucketPolicy] = []
        self.deleted: list[str] = []

    async def ensure_bucket(
        self,
        bucket_name: str,
        ownership_receipt: str,
        policy: BucketPolicy,
    ) -> None:
        record = self.current()
        assert record.provisioning_step == "bucket_allocated"
        assert (record.bucket_name, record.bucket_ownership_receipt) == (
            bucket_name,
            ownership_receipt,
        )
        self.events.append("bucket.ensure")
        self.policies.append(policy)
        if self.fail_ensure:
            raise RuntimeError("injected bucket failure")

    async def delete_bucket(self, bucket_name: str, ownership_receipt: str) -> None:
        assert ownership_receipt
        self.events.append("bucket.delete")
        self.deleted.append(bucket_name)


class FakeTenantRouter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.validated: list[TenantRoute] = []
        self.disposed: list[tuple[UUID, int]] = []
        self.fail_dispose_once = False

    async def validate_route(self, route: TenantRoute) -> None:
        self.events.append("router.validate")
        self.validated.append(route)

    async def dispose_team(
        self,
        team_id: UUID,
        *,
        keep_resource_version: int | None = None,
    ) -> int:
        assert keep_resource_version is not None
        self.events.append("router.dispose_team")
        if self.fail_dispose_once:
            self.fail_dispose_once = False
            raise RuntimeError("injected dispose crash")
        self.disposed.append((team_id, keep_resource_version))
        return 1


class FakeReplicator:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail_copy = False

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
        del (
            source_database_name,
            source_migration_role_name,
            target_database_name,
            target_migration_role_name,
        )
        assert source_password and target_password
        self.events.append("replicator.copy_and_validate")
        if self.fail_copy:
            raise RuntimeError("injected copy failure")


class Harness:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.repository = InMemoryProvisioningRepository(clock=lambda: NOW)
        self.resource = self.repository.add_requested(
            team_id=TEAM_ID,
            resource_id=RESOURCE_ID,
            idempotency_key=IDEMPOTENCY_KEY,
        )

        def current() -> TenantResourceRecord:
            return self.repository.current(TEAM_ID)

        self.postgres = FakePostgresAdmin(self.events, current)
        self.secrets = FakeSecretStore(self.events)
        self.migrator = FakeTenantMigrator(self.events)
        self.buckets = FakeBucketAdmin(self.events, current)
        self.router = FakeTenantRouter(self.events)
        self.replicator = FakeReplicator(self.events)
        password_counter = iter(range(1, 100))
        token_counter = iter(range(1, 100))
        self.provisioner = Provisioner(
            repository=self.repository,
            postgres=self.postgres,
            secret_store=self.secrets,
            migrator=self.migrator,
            bucket_admin=self.buckets,
            tenant_router=self.router,
            replicator=self.replicator,
            sites_origin="https://sites.example",
            password_source=lambda: f"generated-password-{next(password_counter)}".encode(),
            token_source=lambda: f"{next(token_counter):032x}",
            clock=lambda: NOW,
        )


@pytest.mark.asyncio
async def test_provisioning_persists_every_step_and_applies_exact_bucket_policy() -> None:
    harness = Harness()

    result = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)

    assert result.state == "active"
    assert result.provisioning_step == "active"
    assert result.database_migration_revision == "0001_tenant_schema"
    assert result.database_ownership_receipt is not None
    assert result.bucket_ownership_receipt is not None
    database_suffix = result.database_ownership_receipt[-20:]
    assert result.database_name == f"pp_t_{database_suffix}"
    assert result.database_owner_role_name == f"pp_o_{database_suffix}"
    assert result.database_migration_role_name == f"pp_m_{database_suffix}"
    assert result.database_role_name == f"pp_r_{database_suffix}"
    assert result.bucket_name == f"pp-{result.bucket_ownership_receipt[-32:]}"
    assert [record.provisioning_step for record in harness.repository.history] == [
        "requested",
        "database_allocated",
        "database_created",
        "roles_allocated",
        "roles_created",
        "roles_created",
        "migration_credential_stored",
        "migration_credential_stored",
        "credentials_stored",
        "tenant_migrated",
        "bucket_allocated",
        "bucket_created",
        "route_validated",
        "active",
    ]
    assert harness.events == [
        "postgres.ensure_database",
        "postgres.ensure_role_set",
        f"postgres.set_password:{result.database_migration_role_name[:4]}",
        "secret.put:tenant_database_migration_password",
        f"postgres.set_password:{result.database_role_name[:4]}",
        "secret.put:tenant_database_password",
        "migrator.migrate",
        "bucket.ensure",
        "router.validate",
    ]
    assert harness.buckets.policies == [
        BucketPolicy.raw_artifacts(
            sites_origin="https://sites.example",
            retention_days=30,
        )
    ]
    policy = harness.buckets.policies[0]
    assert policy.cors_origins == ("https://sites.example",)
    assert policy.cors_methods == ("PUT", "HEAD")
    assert policy.cors_headers == ("content-type", "x-amz-checksum-sha256")
    assert "*" not in (*policy.cors_origins, *policy.cors_headers)
    assert policy.allow_credentials is False


@pytest.mark.asyncio
async def test_bucket_failure_compensates_in_reverse_and_retry_is_safe() -> None:
    harness = Harness()
    harness.buckets.fail_ensure = True

    failed = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)

    assert failed.state == "cleanup_pending"
    assert failed.provisioning_step == "cleanup"
    assert harness.postgres.deleted_databases == [failed.database_name]
    assert harness.events[-5:] == [
        "bucket.delete",
        "secret.delete",
        "secret.delete",
        "postgres.delete_role_set",
        "postgres.delete_database",
    ]

    harness.buckets.fail_ensure = False
    recovered = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)

    assert recovered.state == "active"
    assert recovered.retry_count == 1
    assert harness.postgres.deleted_databases.count(failed.database_name) == 2
    assert len({failed.database_name, recovered.database_name}) == 2


@pytest.mark.asyncio
async def test_stale_worker_fence_stops_before_the_next_external_step() -> None:
    harness = Harness()
    real_ensure_database = harness.postgres.ensure_database

    async def steal_after_database(database_name: str, ownership_receipt: str) -> None:
        await real_ensure_database(database_name, ownership_receipt)
        harness.repository.steal_lease(TEAM_ID, new_owner="new-worker")

    harness.postgres.ensure_database = steal_after_database  # type: ignore[method-assign]

    with pytest.raises(ProvisioningLeaseLost):
        await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)

    assert harness.events == ["postgres.ensure_database"]


@pytest.mark.asyncio
async def test_long_external_step_renews_lease_before_another_worker_can_claim() -> None:
    harness = Harness()
    harness.repository._clock = lambda: datetime.now(UTC)
    harness.provisioner._lease_duration = timedelta(milliseconds=60)
    migration_started = asyncio.Event()
    allow_migration_to_finish = asyncio.Event()
    original_migrate = harness.migrator.migrate

    async def blocking_migrate(**kwargs: object) -> str:
        migration_started.set()
        await allow_migration_to_finish.wait()
        return await original_migrate(**kwargs)  # type: ignore[arg-type]

    harness.migrator.migrate = blocking_migrate  # type: ignore[method-assign]
    provisioning = asyncio.create_task(
        harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY, worker_id="worker-1")
    )
    await migration_started.wait()
    await asyncio.sleep(0.12)

    competing_lease = None
    try:
        competing_lease, _ = await harness.repository.claim_team(
            team_id=TEAM_ID,
            worker_id="worker-2",
            lease_duration=timedelta(seconds=1),
        )
    except ProvisioningInterrupted:
        pass
    finally:
        allow_migration_to_finish.set()

    try:
        result = await provisioning
    finally:
        if competing_lease is not None:
            await harness.repository.release(competing_lease)
    assert competing_lease is None
    assert result.state == "active", "\n".join(harness.events)


@pytest.mark.asyncio
async def test_worker_claims_the_next_recoverable_resource_without_request_key() -> None:
    harness = Harness()

    processed = await harness.provisioner.process_next(worker_id="worker-1")
    no_more_work = await harness.provisioner.process_next(worker_id="worker-1")

    assert processed is not None
    assert processed.state == "active"
    assert no_more_work is None


@pytest.mark.asyncio
async def test_credential_rotation_recovers_after_switch_before_old_role_revoke() -> None:
    harness = Harness()
    original = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)
    original_role = original.database_role_name
    harness.events.clear()
    harness.router.fail_dispose_once = True

    with pytest.raises(ProvisioningInterrupted):
        await harness.provisioner.rotate_credentials(TEAM_ID)

    switched = harness.repository.current(TEAM_ID)
    assert switched.state == "active"
    assert switched.resource_version == original.resource_version + 1
    assert switched.credential_version == original.credential_version + 1
    assert switched.previous_database_role_name == original_role
    assert harness.postgres.revoked_roles == []
    assert harness.events.index("router.validate") < harness.events.index("router.dispose_team")

    retirement_pending = await harness.provisioner.rotate_credentials(TEAM_ID)

    assert retirement_pending.transition_kind == "credential_rotation"
    assert retirement_pending.transition_step == "retirement_wait"
    assert retirement_pending.previous_database_role_name == original_role
    assert retirement_pending.next_retry_at is not None
    assert harness.postgres.revoked_roles == []
    assert harness.router.disposed[-1] == (TEAM_ID, retirement_pending.resource_version)

    harness.provisioner._clock = lambda: NOW + timedelta(minutes=10)
    completed = await harness.provisioner.rotate_credentials(TEAM_ID)

    assert completed.transition_kind is None
    assert completed.previous_database_role_name is None
    assert harness.postgres.revoked_roles == [original_role]
    assert harness.events.index("router.dispose_team") < harness.events.index(
        "postgres.revoke_runtime_role"
    )


@pytest.mark.asyncio
async def test_initial_secret_intent_survives_crash_before_checkpoint_without_orphan() -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    harness = Harness()
    real_save = harness.repository.save
    crash_once = True

    async def crash_after_secret_put(
        lease: object,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        nonlocal crash_once
        if crash_once and record.provisioning_step == "migration_credential_stored":
            crash_once = False
            raise SimulatedProcessCrash
        return await real_save(lease, record)  # type: ignore[arg-type]

    harness.repository.save = crash_after_secret_put  # type: ignore[method-assign]
    with pytest.raises(SimulatedProcessCrash):
        await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)

    interrupted = harness.repository.current(TEAM_ID)
    reserved_reference = interrupted.database_migration_secret_ref
    assert reserved_reference is not None
    assert len(harness.secrets.values) == 1

    completed = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)

    assert completed.database_migration_secret_ref == reserved_reference
    assert len(harness.secrets.values) == 2
    stored_password, _ = harness.secrets.values[reserved_reference]
    assert harness.postgres.passwords[completed.database_migration_role_name] == stored_password


@pytest.mark.asyncio
async def test_rotation_secret_intent_retries_same_ref_and_keeps_role_in_sync() -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    harness = Harness()
    await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)
    real_save = harness.repository.save
    crash_once = True

    async def crash_after_secret_put(
        lease: object,
        record: TenantResourceRecord,
    ) -> TenantResourceRecord:
        nonlocal crash_once
        if crash_once and record.transition_step == "credential_stored":
            crash_once = False
            raise SimulatedProcessCrash
        return await real_save(lease, record)  # type: ignore[arg-type]

    harness.repository.save = crash_after_secret_put  # type: ignore[method-assign]
    with pytest.raises(SimulatedProcessCrash):
        await harness.provisioner.rotate_credentials(TEAM_ID)

    interrupted = harness.repository.current(TEAM_ID)
    reserved_reference = interrupted.pending_database_secret_ref
    assert reserved_reference is not None

    retirement_pending = await harness.provisioner.rotate_credentials(TEAM_ID)

    assert retirement_pending.database_secret_ref == reserved_reference
    assert len(harness.secrets.values) == 3
    stored_password, _ = harness.secrets.values[reserved_reference]
    assert harness.postgres.passwords[retirement_pending.database_role_name] == stored_password

    harness.provisioner._clock = lambda: NOW + timedelta(minutes=10)
    await harness.provisioner.rotate_credentials(TEAM_ID)
    assert len(harness.secrets.values) == 2


@pytest.mark.asyncio
async def test_resource_migration_failure_before_switch_keeps_original_active() -> None:
    harness = Harness()
    original = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)
    harness.replicator.fail_copy = True
    harness.events.clear()

    result = await harness.provisioner.migrate_team(TEAM_ID)

    assert result.state == "active"
    assert result.write_paused is False
    assert result.resource_version == original.resource_version
    assert result.database_name == original.database_name
    assert result.database_role_name == original.database_role_name
    assert result.transition_kind is None
    assert result.pending_database_name is None
    assert harness.postgres.paused_roles == [original.database_role_name]
    assert harness.postgres.resumed_roles == [original.database_role_name]
    assert harness.events.index("postgres.pause_runtime_writes") < harness.events.index(
        "replicator.copy_and_validate"
    )
    assert harness.events.index("replicator.copy_and_validate") < harness.events.index(
        "postgres.resume_runtime_writes"
    )
    assert "router.validate" not in harness.events
    assert "router.dispose_team" not in harness.events


@pytest.mark.asyncio
async def test_resource_migration_switches_only_after_copy_and_route_validation() -> None:
    harness = Harness()
    original = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)
    harness.events.clear()

    migrated = await harness.provisioner.migrate_team(TEAM_ID)

    assert migrated.state == "active"
    assert migrated.write_paused is False
    assert migrated.resource_version == original.resource_version + 1
    assert migrated.database_name != original.database_name
    assert migrated.previous_database_name == original.database_name
    assert harness.postgres.read_only_databases == [original.database_name]
    assert harness.postgres.paused_roles == [original.database_role_name]
    assert harness.postgres.resumed_roles == []
    pause_index = harness.events.index("postgres.pause_runtime_writes")
    copy_index = harness.events.index("replicator.copy_and_validate")
    validate_index = harness.events.index("router.validate")
    dispose_index = harness.events.index("router.dispose_team")
    read_only_index = harness.events.index("postgres.make_database_read_only")
    assert pause_index < copy_index < validate_index < dispose_index < read_only_index
    assert harness.router.disposed[-1] == (TEAM_ID, migrated.resource_version)

    assert migrated.transition_kind == "resource_migration"
    assert migrated.transition_step == "retirement_wait"
    assert original.database_name not in harness.postgres.deleted_databases

    harness.provisioner._clock = lambda: NOW + timedelta(minutes=10)
    retired = await harness.provisioner.migrate_team(TEAM_ID)

    assert retired.transition_kind is None
    assert retired.previous_database_name is None
    assert original.database_name in harness.postgres.deleted_databases


@pytest.mark.asyncio
async def test_resource_retirement_failure_never_rolls_back_the_switched_route() -> None:
    harness = Harness()
    original = await harness.provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)
    migrated = await harness.provisioner.migrate_team(TEAM_ID)
    harness.provisioner._clock = lambda: NOW + timedelta(minutes=10)
    harness.postgres.fail_delete_database_once = True

    with pytest.raises(ProvisioningInterrupted):
        await harness.provisioner.migrate_team(TEAM_ID)

    still_switched = harness.repository.current(TEAM_ID)
    assert still_switched.database_name == migrated.database_name
    assert still_switched.database_name != original.database_name
    assert still_switched.transition_kind == "resource_migration"
    assert still_switched.transition_step == "retirement_wait"
    assert still_switched.previous_database_name == original.database_name

    retired = await harness.provisioner.migrate_team(TEAM_ID)
    assert retired.transition_kind is None
    assert retired.previous_database_name is None


def test_active_route_generation_is_not_the_orm_cas_version() -> None:
    record = TenantResourceRecord.requested(team_id=TEAM_ID, resource_id=RESOURCE_ID)

    changed = replace(record, resource_version=2, version=99)

    assert changed.resource_version == 2
    assert changed.version == 99


def test_requested_resource_persists_the_owner_until_activation() -> None:
    owner_user_id = uuid4()

    record = TenantResourceRecord.requested(
        team_id=TEAM_ID,
        resource_id=RESOURCE_ID,
        requested_owner_user_id=owner_user_id,
    )

    assert record.requested_owner_user_id == owner_user_id


@pytest.mark.asyncio
async def test_s3_adapter_applies_exact_owned_bucket_configuration() -> None:
    class MissingBucket(Exception):
        def __init__(self) -> None:
            self.response = {"Error": {"Code": "NoSuchBucket"}}

    class FakeS3Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.tags: dict[str, str] | None = None
            self.readbacks: dict[str, dict[str, object]] = {}

        def get_bucket_tagging(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("get_bucket_tagging", kwargs))
            if self.tags is None:
                raise MissingBucket
            return {"TagSet": [{"Key": key, "Value": value} for key, value in self.tags.items()]}

        def __getattr__(self, name: str) -> Callable[..., dict[str, object]]:
            def call(**kwargs: object) -> dict[str, object]:
                self.calls.append((name, kwargs))
                if name == "put_bucket_tagging":
                    tag_set = kwargs["Tagging"]["TagSet"]  # type: ignore[index]
                    self.tags = {
                        tag["Key"]: tag["Value"]  # type: ignore[index]
                        for tag in tag_set
                    }
                elif name == "put_public_access_block":
                    self.readbacks["get_public_access_block"] = {
                        "PublicAccessBlockConfiguration": kwargs["PublicAccessBlockConfiguration"]
                    }
                elif name == "put_bucket_versioning":
                    self.readbacks["get_bucket_versioning"] = kwargs["VersioningConfiguration"]  # type: ignore[assignment]
                elif name == "put_bucket_encryption":
                    self.readbacks["get_bucket_encryption"] = {
                        "ServerSideEncryptionConfiguration": kwargs[
                            "ServerSideEncryptionConfiguration"
                        ]
                    }
                elif name == "put_bucket_lifecycle_configuration":
                    self.readbacks["get_bucket_lifecycle_configuration"] = kwargs[
                        "LifecycleConfiguration"
                    ]  # type: ignore[assignment]
                elif name == "put_bucket_cors":
                    self.readbacks["get_bucket_cors"] = kwargs["CORSConfiguration"]  # type: ignore[assignment]
                elif name.startswith("get_"):
                    return self.readbacks[name]
                return {}

            return call

    client = FakeS3Client()
    adapter = S3BucketAdmin(client=client)  # type: ignore[arg-type]
    policy = BucketPolicy.raw_artifacts(sites_origin="https://sites.example")

    await adapter.ensure_bucket("pp-owned-bucket", "ownership-receipt", policy)

    calls = dict(client.calls)
    assert calls["put_public_access_block"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    assert calls["put_bucket_versioning"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert calls["put_bucket_encryption"]["ServerSideEncryptionConfiguration"] == {
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    }
    lifecycle = calls["put_bucket_lifecycle_configuration"]["LifecycleConfiguration"]
    assert lifecycle == {
        "Rules": [
            {
                "ID": "raw-artifact-retention",
                "Status": "Enabled",
                "Filter": {"Prefix": "raw/"},
                "Expiration": {"Days": 30},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        ]
    }
    assert calls["put_bucket_cors"]["CORSConfiguration"] == {
        "CORSRules": [
            {
                "AllowedOrigins": ["https://sites.example"],
                "AllowedMethods": ["PUT", "HEAD"],
                "AllowedHeaders": ["content-type", "x-amz-checksum-sha256"],
            }
        ]
    }


@pytest.mark.asyncio
async def test_s3_adapter_adopts_only_an_untagged_deterministic_bucket() -> None:
    class MissingTagSet(Exception):
        def __init__(self) -> None:
            self.response = {"Error": {"Code": "NoSuchTagSet"}}

    class FakeS3Client:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.tags: dict[str, str] | None = None
            self.readbacks: dict[str, dict[str, object]] = {}

        def get_bucket_tagging(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            self.calls.append("get_bucket_tagging")
            if self.tags is None:
                raise MissingTagSet
            return {"TagSet": [{"Key": key, "Value": value} for key, value in self.tags.items()]}

        def __getattr__(self, name: str) -> Callable[..., dict[str, object]]:
            def call(**kwargs: object) -> dict[str, object]:
                self.calls.append(name)
                if name == "put_bucket_tagging":
                    tag_set = kwargs["Tagging"]["TagSet"]  # type: ignore[index]
                    self.tags = {
                        tag["Key"]: tag["Value"]  # type: ignore[index]
                        for tag in tag_set
                    }
                elif name == "put_public_access_block":
                    self.readbacks["get_public_access_block"] = {
                        "PublicAccessBlockConfiguration": kwargs["PublicAccessBlockConfiguration"]
                    }
                elif name == "put_bucket_versioning":
                    self.readbacks["get_bucket_versioning"] = kwargs["VersioningConfiguration"]  # type: ignore[assignment]
                elif name == "put_bucket_encryption":
                    self.readbacks["get_bucket_encryption"] = {
                        "ServerSideEncryptionConfiguration": kwargs[
                            "ServerSideEncryptionConfiguration"
                        ]
                    }
                elif name == "put_bucket_lifecycle_configuration":
                    self.readbacks["get_bucket_lifecycle_configuration"] = kwargs[
                        "LifecycleConfiguration"
                    ]  # type: ignore[assignment]
                elif name == "put_bucket_cors":
                    self.readbacks["get_bucket_cors"] = kwargs["CORSConfiguration"]  # type: ignore[assignment]
                elif name.startswith("get_"):
                    return self.readbacks[name]
                return {}

            return call

    receipt = "a" * 64
    bucket_name = f"pp-{receipt[-32:]}"
    client = FakeS3Client()
    adapter = S3BucketAdmin(client=client)  # type: ignore[arg-type]

    await adapter.ensure_bucket(
        bucket_name,
        receipt,
        BucketPolicy.raw_artifacts(sites_origin="https://sites.example"),
    )

    assert "create_bucket" not in client.calls
    assert "put_bucket_tagging" in client.calls
    client.tags = None
    with pytest.raises(ProvisioningInterrupted):
        await adapter.ensure_bucket(
            "pp-unrelated-name",
            receipt,
            BucketPolicy.raw_artifacts(sites_origin="https://sites.example"),
        )


@pytest.mark.asyncio
async def test_s3_adapter_stubber_verifies_exact_write_and_readback_order() -> None:
    receipt = "e" * 64
    bucket_name = f"pp-{receipt[-32:]}"
    ownership_tagging = {
        "TagSet": [
            {
                "Key": "perfpilot:ownership-receipt",
                "Value": receipt,
            }
        ]
    }
    public_access = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    encryption = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
    lifecycle = {
        "Rules": [
            {
                "ID": "raw-artifact-retention",
                "Status": "Enabled",
                "Filter": {"Prefix": "raw/"},
                "Expiration": {"Days": 30},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        ]
    }
    cors = {
        "CORSRules": [
            {
                "AllowedOrigins": ["https://sites.example"],
                "AllowedMethods": ["PUT", "HEAD"],
                "AllowedHeaders": ["content-type", "x-amz-checksum-sha256"],
            }
        ]
    }
    # Stubber intercepts every request; this is an SDK contract gate, not real MinIO.
    client = boto3.client(
        "s3",
        endpoint_url="https://minio.internal",
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_bucket_tagging",
            service_error_code="NoSuchBucket",
            http_status_code=404,
            expected_params={"Bucket": bucket_name},
        )
        stubber.add_response("create_bucket", {}, {"Bucket": bucket_name})
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": bucket_name, "Tagging": ownership_tagging},
        )
        stubber.add_response(
            "put_public_access_block",
            {},
            {"Bucket": bucket_name, "PublicAccessBlockConfiguration": public_access},
        )
        stubber.add_response(
            "put_bucket_versioning",
            {},
            {"Bucket": bucket_name, "VersioningConfiguration": {"Status": "Enabled"}},
        )
        stubber.add_response(
            "put_bucket_encryption",
            {},
            {"Bucket": bucket_name, "ServerSideEncryptionConfiguration": encryption},
        )
        stubber.add_response(
            "put_bucket_lifecycle_configuration",
            {},
            {"Bucket": bucket_name, "LifecycleConfiguration": lifecycle},
        )
        stubber.add_response(
            "put_bucket_cors",
            {},
            {"Bucket": bucket_name, "CORSConfiguration": cors},
        )
        stubber.add_response(
            "get_public_access_block",
            {"PublicAccessBlockConfiguration": public_access},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_versioning",
            {"Status": "Enabled"},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_encryption",
            {"ServerSideEncryptionConfiguration": encryption},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_lifecycle_configuration",
            lifecycle,
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_cors",
            cors,
            {"Bucket": bucket_name},
        )

        await S3BucketAdmin(client=client).ensure_bucket(
            bucket_name,
            receipt,
            BucketPolicy.raw_artifacts(sites_origin="https://sites.example"),
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_s3_adapter_stubber_adopts_only_the_deterministic_unmarked_bucket() -> None:
    receipt = "d" * 64
    bucket_name = f"pp-{receipt[-32:]}"
    ownership_tagging = {"TagSet": [{"Key": "perfpilot:ownership-receipt", "Value": receipt}]}
    public_access = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    encryption = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
    lifecycle = {
        "Rules": [
            {
                "ID": "raw-artifact-retention",
                "Status": "Enabled",
                "Filter": {"Prefix": "raw/"},
                "Expiration": {"Days": 30},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        ]
    }
    cors = {
        "CORSRules": [
            {
                "AllowedOrigins": ["https://sites.example"],
                "AllowedMethods": ["PUT", "HEAD"],
                "AllowedHeaders": ["content-type", "x-amz-checksum-sha256"],
            }
        ]
    }
    client = boto3.client(
        "s3",
        endpoint_url="https://minio.internal",
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_bucket_tagging",
            service_error_code="NoSuchTagSet",
            http_status_code=404,
            expected_params={"Bucket": bucket_name},
        )
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": bucket_name, "Tagging": ownership_tagging},
        )
        stubber.add_response(
            "put_public_access_block",
            {},
            {"Bucket": bucket_name, "PublicAccessBlockConfiguration": public_access},
        )
        stubber.add_response(
            "put_bucket_versioning",
            {},
            {"Bucket": bucket_name, "VersioningConfiguration": {"Status": "Enabled"}},
        )
        stubber.add_response(
            "put_bucket_encryption",
            {},
            {"Bucket": bucket_name, "ServerSideEncryptionConfiguration": encryption},
        )
        stubber.add_response(
            "put_bucket_lifecycle_configuration",
            {},
            {"Bucket": bucket_name, "LifecycleConfiguration": lifecycle},
        )
        stubber.add_response(
            "put_bucket_cors",
            {},
            {"Bucket": bucket_name, "CORSConfiguration": cors},
        )
        stubber.add_response(
            "get_public_access_block",
            {"PublicAccessBlockConfiguration": public_access},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_versioning",
            {"Status": "Enabled"},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_encryption",
            {"ServerSideEncryptionConfiguration": encryption},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "get_bucket_lifecycle_configuration",
            lifecycle,
            {"Bucket": bucket_name},
        )
        stubber.add_response("get_bucket_cors", cors, {"Bucket": bucket_name})

        await S3BucketAdmin(client=client).ensure_bucket(
            bucket_name,
            receipt,
            BucketPolicy.raw_artifacts(sites_origin="https://sites.example"),
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_s3_adapter_stubber_deletes_every_owned_object_version_before_bucket() -> None:
    receipt = "c" * 64
    bucket_name = f"pp-{receipt[-32:]}"
    client = boto3.client(
        "s3",
        endpoint_url="https://minio.internal",
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_bucket_tagging",
            {"TagSet": [{"Key": "perfpilot:ownership-receipt", "Value": receipt}]},
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": True,
                "NextKeyMarker": "raw/second",
                "NextVersionIdMarker": "version-2",
                "Versions": [{"Key": "raw/first", "VersionId": "version-1"}],
                "DeleteMarkers": [{"Key": "raw/deleted", "VersionId": "marker-1"}],
            },
            {"Bucket": bucket_name},
        )
        stubber.add_response(
            "delete_objects",
            {},
            {
                "Bucket": bucket_name,
                "Delete": {
                    "Objects": [
                        {"Key": "raw/first", "VersionId": "version-1"},
                        {"Key": "raw/deleted", "VersionId": "marker-1"},
                    ],
                    "Quiet": True,
                },
            },
        )
        stubber.add_response(
            "list_object_versions",
            {"IsTruncated": False},
            {
                "Bucket": bucket_name,
                "KeyMarker": "raw/second",
                "VersionIdMarker": "version-2",
            },
        )
        stubber.add_response("delete_bucket", {}, {"Bucket": bucket_name})

        await S3BucketAdmin(client=client).delete_bucket(bucket_name, receipt)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_s3_adapter_stubber_maps_configuration_error_and_stops() -> None:
    receipt = "f" * 64
    bucket_name = f"pp-{receipt[-32:]}"
    client = boto3.client(
        "s3",
        endpoint_url="https://minio.internal",
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {
                        "Key": "perfpilot:ownership-receipt",
                        "Value": receipt,
                    }
                ]
            },
            {"Bucket": bucket_name},
        )
        stubber.add_client_error(
            "put_public_access_block",
            service_error_code="AccessDenied",
            service_message=f"credential=test-secret-key receipt={receipt}",
            http_status_code=403,
            expected_params={
                "Bucket": bucket_name,
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            },
        )

        with pytest.raises(ProvisioningInterrupted) as exc_info:
            await S3BucketAdmin(client=client).ensure_bucket(
                bucket_name,
                receipt,
                BucketPolicy.raw_artifacts(sites_origin="https://sites.example"),
            )
        stubber.assert_no_pending_responses()
    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert "test-secret-key" not in rendered
    assert receipt not in rendered
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_postgres_adapter_adopts_deterministic_unmarked_database_after_crash() -> None:
    receipt = "b" * 64
    database_name = f"pp_t_{receipt[-20:]}"
    adapter = PsycopgTenantAdmin(admin_conninfo="user=perfpilot_admin dbname=postgres")
    statements: list[object] = []

    async def database_metadata(name: str) -> tuple[bool, str | None, str | None]:
        assert name == database_name
        return True, None, "perfpilot_admin"

    class FakeConnection:
        async def __aenter__(self) -> "FakeConnection":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def execute(self, statement: object, *args: object) -> None:
            del args
            statements.append(statement)

    async def connection(**kwargs: object) -> FakeConnection:
        del kwargs
        return FakeConnection()

    adapter._database_metadata = database_metadata  # type: ignore[method-assign]
    adapter._connection = connection  # type: ignore[method-assign]

    await adapter.ensure_database(database_name, receipt)

    assert len(statements) == 1
    assert "COMMENT ON DATABASE" in str(statements[0])


@pytest.mark.asyncio
async def test_postgres_role_create_and_ownership_marker_share_one_transaction() -> None:
    receipt = "c" * 64
    role_name = f"pp_r_{receipt[-20:]}"
    adapter = PsycopgTenantAdmin(admin_conninfo="user=perfpilot_admin dbname=postgres")
    autocommit_values: list[bool] = []
    statements: list[object] = []

    async def role_metadata(name: str) -> tuple[bool, str | None]:
        assert name == role_name
        return False, None

    class FakeConnection:
        async def __aenter__(self) -> "FakeConnection":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def execute(self, statement: object, *args: object) -> None:
            del args
            statements.append(statement)

    async def connection(
        *, database_name: str | None = None, autocommit: bool = True
    ) -> FakeConnection:
        del database_name
        autocommit_values.append(autocommit)
        return FakeConnection()

    adapter._role_metadata = role_metadata  # type: ignore[method-assign]
    adapter._connection = connection  # type: ignore[method-assign]

    await adapter._ensure_role(
        role_name=role_name,
        can_login=True,
        ownership_receipt=receipt,
    )

    assert autocommit_values == [False]
    assert len(statements) == 2
    assert "CREATE ROLE" in str(statements[0])
    assert "COMMENT ON ROLE" in str(statements[1])


def _assert_sanitized_boundary_error(
    error: BaseException,
    *,
    forbidden_markers: tuple[str, ...],
) -> None:
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            "".join(traceback.format_exception(error)),
        )
    )
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    for marker in forbidden_markers:
        assert marker not in rendered


def test_postgres_admin_conninfo_parse_error_suppresses_sensitive_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "postgres-password-marker"
    dsn = f"postgresql://admin:{password}@db.internal/postgres"
    receipt = "receipt-marker-" + "a" * 64

    def reject_conninfo(value: str) -> dict[str, str]:
        assert value == dsn
        raise RuntimeError(f"could not parse {dsn} with receipt {receipt}")

    monkeypatch.setattr(
        "perfpilot_api.services.provisioning.conninfo_to_dict",
        reject_conninfo,
    )

    with pytest.raises(ValueError) as exc_info:
        PsycopgTenantAdmin(admin_conninfo=dsn)

    _assert_sanitized_boundary_error(
        exc_info.value,
        forbidden_markers=(password, dsn, receipt),
    )


@pytest.mark.asyncio
async def test_postgres_role_password_is_bound_and_not_rendered_into_sql() -> None:
    receipt = "e" * 64
    password = b"postgres-password-marker"
    role_name = f"pp_r_{receipt[-20:]}"
    adapter = PsycopgTenantAdmin(admin_conninfo="user=perfpilot_admin dbname=postgres")
    executions: list[tuple[object, tuple[object, ...]]] = []

    async def ownership_check(*args: object, **kwargs: object) -> None:
        del args, kwargs

    class FakeConnection:
        async def __aenter__(self) -> "FakeConnection":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def execute(self, statement: object, *args: object) -> None:
            executions.append((statement, args))

    async def connection(**kwargs: object) -> FakeConnection:
        del kwargs
        return FakeConnection()

    adapter._require_database_owner = ownership_check  # type: ignore[method-assign]
    adapter._require_role_owner = ownership_check  # type: ignore[method-assign]
    adapter._connection = connection  # type: ignore[method-assign]

    await adapter.set_role_password(
        database_name=f"pp_t_{receipt[-20:]}",
        role_name=role_name,
        password=password,
        ownership_receipt=receipt,
    )

    assert len(executions) == 1
    statement, arguments = executions[0]
    assert "PASSWORD %s" in str(statement)
    assert password.decode() not in str(statement)
    assert arguments == ((password.decode(),),)


@pytest.mark.asyncio
async def test_postgres_admin_suppresses_sensitive_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "postgres-password-marker"
    dsn = f"postgresql://admin:{password}@db.internal/postgres"
    receipt = "f" * 64
    database_name = f"pp_t_{receipt[-20:]}"
    adapter = PsycopgTenantAdmin(admin_conninfo="user=perfpilot_admin dbname=postgres")

    async def connect(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(f"driver rejected {dsn}; receipt={receipt}")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)

    with pytest.raises(ProvisioningInterrupted) as exc_info:
        await adapter.ensure_database(database_name, receipt)

    _assert_sanitized_boundary_error(
        exc_info.value,
        forbidden_markers=(password, dsn, receipt),
    )


@pytest.mark.asyncio
async def test_tenant_migrator_keeps_credentials_out_of_config_and_suppresses_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "migration-password-marker"
    dsn = f"postgresql://migration:{password}@tenant-postgres.internal/tenant"
    receipt = "receipt-marker-" + "b" * 64
    captured_configs: list[Config] = []

    async def run_inline(function: Callable[..., object], *args: object) -> object:
        del function
        config = args[0]
        assert isinstance(config, Config)
        captured_configs.append(config)
        raise RuntimeError(f"migration failed for {dsn}; receipt={receipt}")

    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    migrator = AlembicTenantMigrator(
        migration_root=_API_ROOT / "migrations" / "tenant",
        cluster_host="tenant-postgres.internal",
    )

    with pytest.raises(ProvisioningInterrupted) as exc_info:
        await migrator.migrate(
            database_name="pp_t_aaaaaaaaaaaaaaaaaaaa",
            migration_role_name="pp_m_aaaaaaaaaaaaaaaaaaaa",
            runtime_role_name="pp_r_aaaaaaaaaaaaaaaaaaaa",
            password=password.encode(),
        )

    assert len(captured_configs) == 1
    config_rendered = "\n".join(
        (
            str(captured_configs[0]),
            repr(captured_configs[0]),
            repr(captured_configs[0].get_section(captured_configs[0].config_ini_section)),
            repr(captured_configs[0].attributes),
        )
    )
    assert password not in config_rendered
    assert dsn not in config_rendered
    _assert_sanitized_boundary_error(
        exc_info.value,
        forbidden_markers=(password, dsn, receipt),
    )


@pytest.mark.asyncio
async def test_postgres_role_set_revokes_default_public_tenant_access() -> None:
    receipt = "d" * 64
    adapter = PsycopgTenantAdmin(admin_conninfo="user=perfpilot_admin dbname=postgres")
    statements: list[object] = []

    async def no_database_check(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def no_role_create(*args: object, **kwargs: object) -> None:
        del args, kwargs

    class FakeConnection:
        async def __aenter__(self) -> "FakeConnection":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def execute(self, statement: object, *args: object) -> None:
            del args
            statements.append(statement)

    async def connection(**kwargs: object) -> FakeConnection:
        del kwargs
        return FakeConnection()

    adapter._require_database_owner = no_database_check  # type: ignore[method-assign]
    adapter._ensure_role = no_role_create  # type: ignore[method-assign]
    adapter._connection = connection  # type: ignore[method-assign]

    await adapter.ensure_role_set(
        database_name=f"pp_t_{receipt[-20:]}",
        owner_role_name=f"pp_o_{receipt[-20:]}",
        migration_role_name=f"pp_m_{receipt[-20:]}",
        runtime_role_name=f"pp_r_{receipt[-20:]}",
        ownership_receipt=receipt,
    )

    rendered = "\n".join(str(statement) for statement in statements)
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE" in rendered
    assert "REVOKE ALL PRIVILEGES ON SCHEMA" in rendered
    assert "FROM PUBLIC" in rendered


@pytest.mark.asyncio
async def test_tenant_migrator_grants_default_sequence_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[object] = []

    class FakeCursor:
        async def fetchone(self) -> tuple[str]:
            return ("0001_tenant_schema",)

    class FakeConnection:
        async def __aenter__(self) -> "FakeConnection":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def execute(self, statement: object, *args: object) -> FakeCursor:
            del args
            statements.append(statement)
            return FakeCursor()

        async def commit(self) -> None:
            return None

    async def connect(*args: object, **kwargs: object) -> FakeConnection:
        del args, kwargs
        return FakeConnection()

    async def run_inline(function: Callable[..., object], *args: object) -> object:
        del function, args
        return None

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    migrator = AlembicTenantMigrator(
        migration_root=_API_ROOT / "migrations" / "tenant",
        cluster_host="tenant-postgres.internal",
    )

    await migrator.migrate(
        database_name="pp_t_aaaaaaaaaaaaaaaaaaaa",
        migration_role_name="pp_m_aaaaaaaaaaaaaaaaaaaa",
        runtime_role_name="pp_r_aaaaaaaaaaaaaaaaaaaa",
        password=b"generated-password-1",
    )

    rendered = "\n".join(str(statement) for statement in statements)
    assert "ALTER DEFAULT PRIVILEGES" in rendered
    assert "ON SEQUENCES" in rendered


def _psycopg_conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def provisioning_control_database_url() -> Iterator[URL]:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL provisioning tests")
    admin_url = make_url(raw_url)
    database_name = f"perfpilot_test_provisioning_{uuid4().hex}"
    database_url = admin_url.set(database=database_name)
    with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name))
        )
    try:
        migration_root = (_API_ROOT / "migrations" / "control").resolve()
        config = Config(str(migration_root / "alembic.ini"))
        config.set_main_option("script_location", str(migration_root))
        config.set_main_option(
            "sqlalchemy.url",
            database_url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        command.upgrade(config, "head")
        yield database_url
    finally:
        with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


@pytest.fixture
async def provisioning_session_factory(
    provisioning_control_database_url: URL,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        provisioning_control_database_url.render_as_string(hide_password=False),
        poolclass=NullPool,
    )
    factory = create_control_session_factory(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE audit_events, idempotency_keys, tenant_quotas, "
                "tenant_resources, memberships, sessions, teams, users CASCADE"
            )
        )
    try:
        yield factory
    finally:
        await engine.dispose()


def _tenant_role_conninfo(
    admin_url: URL,
    *,
    database_name: str,
    role_name: str,
    password: str,
) -> str:
    return URL.create(
        "postgresql",
        username=role_name,
        password=password,
        host=admin_url.host,
        port=admin_url.port,
        database=database_name,
        query=dict(admin_url.query),
    ).render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_real_postgres_tenant_admin_enforces_isolation_and_drains_connections(
    provisioning_control_database_url: URL,
) -> None:
    raw_admin_url = os.getenv(_TENANT_ADMIN_URL_ENV)
    if raw_admin_url is None:
        pytest.skip(f"set {_TENANT_ADMIN_URL_ENV} to run tenant-admin integration")
    admin_url = make_url(raw_admin_url)
    if admin_url.host is None:
        pytest.skip("tenant-admin integration requires an explicit cluster host")
    adapter = PsycopgTenantAdmin(admin_conninfo=_psycopg_conninfo(admin_url))
    first_receipt = uuid4().hex + uuid4().hex
    second_receipt = uuid4().hex + uuid4().hex
    first_suffix = first_receipt[-20:]
    second_suffix = second_receipt[-20:]
    first_database = f"pp_t_{first_suffix}"
    second_database = f"pp_t_{second_suffix}"
    first_roles = (f"pp_o_{first_suffix}", f"pp_m_{first_suffix}", f"pp_r_{first_suffix}")
    second_roles = (
        f"pp_o_{second_suffix}",
        f"pp_m_{second_suffix}",
        f"pp_r_{second_suffix}",
    )
    migration_password = "migration-password-for-real-pg"
    runtime_password = "runtime-password-for-real-pg"
    runtime_connection: psycopg.AsyncConnection[object] | None = None
    try:
        await adapter.ensure_database(first_database, first_receipt)
        await adapter.ensure_role_set(
            database_name=first_database,
            owner_role_name=first_roles[0],
            migration_role_name=first_roles[1],
            runtime_role_name=first_roles[2],
            ownership_receipt=first_receipt,
        )
        await adapter.set_role_password(
            database_name=first_database,
            role_name=first_roles[1],
            password=migration_password.encode(),
            ownership_receipt=first_receipt,
        )
        await adapter.set_role_password(
            database_name=first_database,
            role_name=first_roles[2],
            password=runtime_password.encode(),
            ownership_receipt=first_receipt,
        )
        await adapter.ensure_database(second_database, second_receipt)
        await adapter.ensure_role_set(
            database_name=second_database,
            owner_role_name=second_roles[0],
            migration_role_name=second_roles[1],
            runtime_role_name=second_roles[2],
            ownership_receipt=second_receipt,
        )

        sslmode = str(admin_url.query.get("sslmode", "disable"))
        await AlembicTenantMigrator(
            migration_root=_API_ROOT / "migrations" / "tenant",
            cluster_host=admin_url.host,
            cluster_port=admin_url.port or 5432,
            sslmode=sslmode,
        ).migrate(
            database_name=first_database,
            migration_role_name=first_roles[1],
            runtime_role_name=first_roles[2],
            password=migration_password.encode(),
        )
        migration_conninfo = _tenant_role_conninfo(
            admin_url,
            database_name=first_database,
            role_name=first_roles[1],
            password=migration_password,
        )
        async with await psycopg.AsyncConnection.connect(
            migration_conninfo,
            autocommit=True,
        ) as migration_connection:
            await migration_connection.execute(
                "CREATE TABLE public.future_items (id bigint PRIMARY KEY, value text NOT NULL)"
            )
            await migration_connection.execute("CREATE SEQUENCE public.future_sequence")

        runtime_conninfo = _tenant_role_conninfo(
            admin_url,
            database_name=first_database,
            role_name=first_roles[2],
            password=runtime_password,
        )
        runtime_connection = await psycopg.AsyncConnection.connect(
            runtime_conninfo,
            autocommit=True,
        )
        await runtime_connection.execute(
            "INSERT INTO public.future_items (id, value) VALUES (1, 'ok')"
        )
        assert (
            await (
                await runtime_connection.execute(
                    "SELECT value FROM public.future_items WHERE id = 1"
                )
            ).fetchone()
        ) == ("ok",)
        assert (
            await (await runtime_connection.execute("SELECT nextval('future_sequence')")).fetchone()
        ) == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await runtime_connection.execute("CREATE TABLE public.forbidden_ddl (id bigint)")

        with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
            database_acl = connection.execute(
                "SELECT has_database_privilege('public', %s, 'CONNECT'), "
                "has_database_privilege('public', %s, 'TEMPORARY')",
                (first_database, first_database),
            ).fetchone()
            role_attributes = connection.execute(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
                (list(first_roles),),
            ).fetchall()
        assert database_acl == (False, False)
        assert role_attributes == [
            (first_roles[1], True, False, False, False),
            (first_roles[0], False, False, False, False),
            (first_roles[2], True, False, False, False),
        ]
        async with await psycopg.AsyncConnection.connect(
            _tenant_role_conninfo(
                admin_url,
                database_name=first_database,
                role_name=first_roles[1],
                password=migration_password,
            )
        ) as migration_connection:
            schema_acl = await (
                await migration_connection.execute(
                    "SELECT has_schema_privilege('public', 'public', 'USAGE')"
                )
            ).fetchone()
        assert schema_acl == (False,)

        with pytest.raises(psycopg.OperationalError):
            await psycopg.AsyncConnection.connect(
                _tenant_role_conninfo(
                    admin_url,
                    database_name=second_database,
                    role_name=first_roles[2],
                    password=runtime_password,
                )
            )
        control_denied = False
        try:
            async with await psycopg.AsyncConnection.connect(
                _tenant_role_conninfo(
                    admin_url,
                    database_name=provisioning_control_database_url.database,
                    role_name=first_roles[2],
                    password=runtime_password,
                )
            ) as control_connection:
                await control_connection.execute("SELECT count(*) FROM users")
        except psycopg.Error:
            control_denied = True
        assert control_denied

        await adapter.pause_runtime_writes(
            database_name=first_database,
            role_name=first_roles[2],
            ownership_receipt=first_receipt,
        )
        with pytest.raises(psycopg.Error):
            await runtime_connection.execute("SELECT 1")
        await runtime_connection.close()
        runtime_connection = None
        await adapter.resume_runtime_writes(
            database_name=first_database,
            role_name=first_roles[2],
            ownership_receipt=first_receipt,
        )
        async with await psycopg.AsyncConnection.connect(
            runtime_conninfo,
            autocommit=True,
        ) as resumed_connection:
            await resumed_connection.execute(
                "INSERT INTO public.future_items (id, value) VALUES (2, 'resumed')"
            )

        with pytest.raises(ProvisioningInterrupted):
            await adapter.delete_database(first_database, "0" * 64)
        with pytest.raises(ProvisioningInterrupted):
            await adapter.revoke_runtime_role(
                database_name=first_database,
                role_name=first_roles[2],
                ownership_receipt="0" * 64,
            )

        runtime_connection = await psycopg.AsyncConnection.connect(
            runtime_conninfo,
            autocommit=True,
        )
        await runtime_connection.execute("SELECT 1")
        await adapter.revoke_runtime_role(
            database_name=first_database,
            role_name=first_roles[2],
            ownership_receipt=first_receipt,
        )
        with pytest.raises(psycopg.Error):
            await runtime_connection.execute("SELECT 1")
        await runtime_connection.close()
        runtime_connection = None
    finally:
        if runtime_connection is not None:
            await runtime_connection.close()
        for database_name, roles, receipt in (
            (first_database, first_roles, first_receipt),
            (second_database, second_roles, second_receipt),
        ):
            try:
                await adapter.delete_role_set(
                    database_name=database_name,
                    owner_role_name=roles[0],
                    migration_role_name=roles[1],
                    runtime_role_name=roles[2],
                    ownership_receipt=receipt,
                )
            finally:
                await adapter.delete_database(database_name, receipt)


async def _seed_admin_request_identities(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor_is_admin: bool = True,
    owner_state: str = "active",
) -> tuple[User, User, str, str]:
    actor = User(
        username="platform-admin",
        password_hash="unused-test-hash",
        state="active",
        is_platform_admin=actor_is_admin,
    )
    owner = User(
        username="tenant-owner",
        password_hash="unused-test-hash",
        state=owner_state,
        is_platform_admin=False,
    )
    session_token = "admin-session-token"
    csrf_token = "admin-csrf-token"
    async with session_factory() as session, session.begin():
        session.add_all((actor, owner))
        await session.flush()
        session.add(
            AuthSession(
                user_id=actor.id,
                token_digest=digest_session_token(session_token),
                kind="authenticated",
                csrf_secret_hash=digest_csrf_token(csrf_token),
                last_seen_at=NOW,
                absolute_expires_at=NOW.replace(year=2027),
                revoked_at=None,
            )
        )
    return actor, owner, session_token, csrf_token


@pytest.mark.asyncio
async def test_admin_team_create_is_atomic_actor_scoped_and_replay_safe(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor, owner, session_token, csrf_token = await _seed_admin_request_identities(
        provisioning_session_factory
    )
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )

    created = await service.create_team(
        session_token=session_token,
        csrf_token=csrf_token,
        idempotency_key="admin-create-1",
        name="Team Alpha",
        owner_user_id=owner.id,
        request_id="req-admin-create",
    )
    replayed = await service.create_team(
        session_token=session_token,
        csrf_token=csrf_token,
        idempotency_key="admin-create-1",
        name="Team Alpha",
        owner_user_id=owner.id,
        request_id="req-admin-replay",
    )

    assert created.team_id == replayed.team_id
    assert created.resource_state == "requested"
    async with provisioning_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Team)) == 1
        assert await session.scalar(select(func.count()).select_from(TenantResource)) == 1
        assert await session.scalar(select(func.count()).select_from(TenantQuota)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        memberships = (await session.scalars(select(Membership))).all()
        assert memberships == []
        stored_resource = (await session.scalars(select(TenantResource))).one()
        assert stored_resource.requested_owner_user_id == owner.id
        stored_key = (await session.scalars(select(IdempotencyKey))).one()
        assert stored_key.operation == "create_team"
        assert stored_key.scope_type == "actor"
        assert stored_key.scope_id == actor.id
        assert stored_key.team_id == created.team_id
        assert stored_key.response_resource_id == created.resource_id
        assert stored_key.state == "completed"

    with pytest.raises(AdminIdempotencyConflict):
        await service.create_team(
            session_token=session_token,
            csrf_token=csrf_token,
            idempotency_key="admin-create-1",
            name="Different Team",
            owner_user_id=owner.id,
            request_id="req-admin-conflict",
        )


@pytest.mark.asyncio
async def test_sql_activation_atomically_adds_owner_and_success_audit(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, session_token, csrf_token = await _seed_admin_request_identities(
        provisioning_session_factory
    )
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )
    created = await service.create_team(
        session_token=session_token,
        csrf_token=csrf_token,
        idempotency_key="activation-create",
        name="Activation Team",
        owner_user_id=owner.id,
        request_id="req-activation-create",
    )
    repository = SqlAlchemyProvisioningRepository(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )
    lease, record = await repository.claim_for_request(
        team_id=created.team_id,
        idempotency_key="activation-create",
        worker_id="activation-worker",
        lease_duration=timedelta(minutes=1),
    )

    activated = await repository.activate(
        lease,
        replace(record, state="active", provisioning_step="active"),
    )
    await repository.release(lease)

    assert activated.state == "active"
    async with provisioning_session_factory() as session:
        membership = (await session.scalars(select(Membership))).one()
        assert (membership.team_id, membership.user_id, membership.role) == (
            created.team_id,
            owner.id,
            "team_owner",
        )
        event_types = set(await session.scalars(select(AuditEvent.event_type)))
        assert event_types == {"team.create_requested", "tenant.provisioned"}


@pytest.mark.asyncio
async def test_sql_repository_rejects_a_stale_fencing_token(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, _, _ = await _seed_admin_request_identities(provisioning_session_factory)
    team = Team(name="Repository Team", state="active")
    resource = TenantResource(team_id=uuid4(), state="requested")
    async with provisioning_session_factory() as session, session.begin():
        session.add(team)
        await session.flush()
        resource.team_id = team.id
        session.add(resource)
        await session.flush()
        session.add(
            IdempotencyKey(
                team_id=team.id,
                key="repo-request",
                operation="create_team",
                scope_type="actor",
                scope_id=owner.id,
                request_hash="a" * 64,
                state="completed",
                response_resource_id=resource.id,
                expires_at=NOW + timedelta(days=1),
            )
        )
        session.add(
            IdempotencyKey(
                team_id=team.id,
                key="repo-request",
                operation="unrelated_operation",
                scope_type="actor",
                scope_id=owner.id,
                request_hash="b" * 64,
                state="completed",
                response_resource_id=uuid4(),
                expires_at=NOW + timedelta(days=1),
            )
        )
    repository = SqlAlchemyProvisioningRepository(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )
    first_lease, first_record = await repository.claim_for_request(
        team_id=team.id,
        idempotency_key="repo-request",
        worker_id="worker-1",
        lease_duration=timedelta(seconds=60),
    )
    first_record = await repository.save(
        first_lease,
        replace(first_record, state="provisioning"),
    )
    async with provisioning_session_factory() as session, session.begin():
        await session.execute(
            update(TenantResource)
            .where(TenantResource.id == resource.id)
            .values(worker_lease_expires_at=NOW - timedelta(seconds=1))
        )
    second_lease, second_record = await repository.claim_team(
        team_id=team.id,
        worker_id="worker-2",
        lease_duration=timedelta(seconds=60),
    )

    with pytest.raises(ProvisioningLeaseLost):
        await repository.save(
            first_lease,
            replace(first_record, provisioning_step="database_created"),
        )
    saved = await repository.save(
        second_lease,
        replace(second_record, provisioning_step="database_allocated"),
    )
    assert saved.fencing_token == second_lease.fencing_token
    assert saved.provisioning_step == "database_allocated"


@pytest.mark.asyncio
async def test_concurrent_admin_replays_create_exactly_one_team(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, session_token, csrf_token = await _seed_admin_request_identities(
        provisioning_session_factory
    )
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )

    first, second = await asyncio.gather(
        *(
            service.create_team(
                session_token=session_token,
                csrf_token=csrf_token,
                idempotency_key="concurrent-create",
                name="Concurrent Team",
                owner_user_id=owner.id,
                request_id=f"req-concurrent-{index}",
            )
            for index in (1, 2)
        )
    )

    assert first.team_id == second.team_id
    async with provisioning_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Team)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyKey)) == 1


@pytest.mark.asyncio
async def test_admin_team_create_requires_platform_admin_and_active_owner(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, session_token, csrf_token = await _seed_admin_request_identities(
        provisioning_session_factory,
        actor_is_admin=False,
    )
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )
    with pytest.raises(AdminNotPlatformAdministrator):
        await service.create_team(
            session_token=session_token,
            csrf_token=csrf_token,
            idempotency_key="non-admin",
            name="Forbidden Team",
            owner_user_id=owner.id,
            request_id="req-non-admin",
        )

    async with provisioning_session_factory() as session, session.begin():
        actor = (await session.scalars(select(User).where(User.username == "platform-admin"))).one()
        actor.is_platform_admin = True
        owner.state = "disabled"
        stored_owner = await session.get(User, owner.id)
        assert stored_owner is not None
        stored_owner.state = "disabled"
    with pytest.raises(AdminOwnerNotFound):
        await service.create_team(
            session_token=session_token,
            csrf_token=csrf_token,
            idempotency_key="disabled-owner",
            name="No Owner Team",
            owner_user_id=owner.id,
            request_id="req-disabled-owner",
        )


def _signed_admin_headers(body: bytes, *, request_id: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "origin": "https://app.example",
        "x-csrf-token": "admin-csrf-token",
        "idempotency-key": "api-create-1",
        "x-request-id": request_id,
        "x-perfpilot-proxy-timestamp": "1700000000",
        "x-perfpilot-proxy-signature": sign_proxy_request(
            b"test-proxy-secret",
            timestamp=1_700_000_000,
            request_id=request_id,
            method="POST",
            raw_path=b"/v1/admin/teams",
            body=body,
        ),
    }


def _signed_admin_get_headers(team_id: UUID, *, request_id: str) -> dict[str, str]:
    raw_path = f"/v1/admin/teams/{team_id}".encode()
    return {
        "x-request-id": request_id,
        "x-perfpilot-proxy-timestamp": "1700000000",
        "x-perfpilot-proxy-signature": sign_proxy_request(
            b"test-proxy-secret",
            timestamp=1_700_000_000,
            request_id=request_id,
            method="GET",
            raw_path=raw_path,
        ),
    }


def _admin_test_app(service: AdminTeamService) -> object:
    settings = Settings(
        app_env="test",
        proxy_secret="test-proxy-secret",
        allowed_origins=("https://app.example",),
        _env_prefix="PERFPILOT_TEST_ISOLATED_",
        _env_file=None,
        _secrets_dir=None,
    )
    return create_app(
        testing=True,
        settings_override=settings,
        admin_team_service=service,
        proxy_clock=lambda: 1_700_000_000,
    )


async def _seed_team_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_user_id: UUID,
    name: str,
    state: str,
    provisioning_step: str,
    transition_kind: str | None = None,
    transition_step: str | None = None,
    retry_count: int = 0,
    next_retry_at: datetime | None = None,
    last_error_code: str | None = None,
    resource_version: int = 1,
    credential_version: int = 1,
    write_paused: bool = False,
) -> Team:
    team = Team(name=name, state="active")
    async with session_factory() as session, session.begin():
        session.add(team)
        await session.flush()
        session.add(
            TenantResource(
                team_id=team.id,
                requested_owner_user_id=owner_user_id,
                state=state,
                provisioning_step=provisioning_step,
                transition_kind=transition_kind,
                transition_step=transition_step,
                retry_count=retry_count,
                next_retry_at=next_retry_at,
                last_error_code=last_error_code,
                resource_version=resource_version,
                credential_version=credential_version,
                write_paused=write_paused,
                database_name=f"sensitive_database_{name}",
                database_owner_role_name=f"sensitive_owner_role_{name}",
                database_migration_role_name=f"sensitive_migration_role_{name}",
                database_migration_secret_ref=f"secret://migration/{name}",
                database_role_name=f"sensitive_runtime_role_{name}",
                database_secret_ref=f"secret://runtime/{name}",
                bucket_name=f"sensitive-bucket-{name}",
                database_ownership_receipt=f"sensitive-database-receipt-{name}",
                role_ownership_receipt=f"sensitive-role-receipt-{name}",
                bucket_ownership_receipt=f"sensitive-bucket-receipt-{name}",
                worker_lease_owner=f"sensitive-worker-{name}",
                fencing_token=99,
            )
        )
    return team


def _walk_json(value: object) -> Iterator[tuple[str | None, object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield None, child
            yield from _walk_json(child)


@pytest.mark.asyncio
async def test_admin_team_status_maps_lifecycle_states_without_sensitive_fields(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor, owner, session_token, _ = await _seed_admin_request_identities(
        provisioning_session_factory
    )
    status_now = NOW + timedelta(hours=1)
    cases = (
        {
            "name": "Requested Team",
            "state": "requested",
            "provisioning_step": "requested",
            "transition_kind": None,
            "transition_step": None,
            "retry_count": 0,
            "next_retry_at": None,
            "last_error_code": None,
            "resource_version": 1,
            "credential_version": 1,
            "write_paused": False,
        },
        {
            "name": "Active Team",
            "state": "active",
            "provisioning_step": "active",
            "transition_kind": None,
            "transition_step": None,
            "retry_count": 0,
            "next_retry_at": None,
            "last_error_code": None,
            "resource_version": 2,
            "credential_version": 3,
            "write_paused": False,
        },
        {
            "name": "Cleanup Team",
            "state": "cleanup_pending",
            "provisioning_step": "cleanup",
            "transition_kind": None,
            "transition_step": None,
            "retry_count": 4,
            "next_retry_at": NOW + timedelta(minutes=5),
            "last_error_code": "tenant_cleanup_failed",
            "resource_version": 1,
            "credential_version": 1,
            "write_paused": True,
        },
        {
            "name": "Migrating Team",
            "state": "migrating",
            "provisioning_step": "active",
            "transition_kind": "resource_migration",
            "transition_step": "copying_data",
            "retry_count": 2,
            "next_retry_at": None,
            "last_error_code": "tenant_copy_retryable",
            "resource_version": 7,
            "credential_version": 5,
            "write_paused": True,
        },
    )
    seeded = [
        (
            await _seed_team_status(
                provisioning_session_factory,
                owner_user_id=owner.id,
                **case,
            ),
            case,
        )
        for case in cases
    ]
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: status_now,
    )

    with TestClient(_admin_test_app(service)) as client:
        client.cookies.set(COOKIE_NAME, session_token)
        responses = [
            client.get(
                f"/v1/admin/teams/{team.id}",
                headers=_signed_admin_get_headers(
                    team.id,
                    request_id=f"req-status-{index}",
                ),
            )
            for index, (team, _) in enumerate(seeded)
        ]

    for response, (team, case) in zip(responses, seeded, strict=True):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload == {
            "schema_version": "1.0",
            "team": {
                "id": str(team.id),
                "name": case["name"],
                "state": "active",
            },
            "provisioning": {
                "state": case["state"],
                "provisioning_step": case["provisioning_step"],
                "transition_kind": case["transition_kind"],
                "transition_step": case["transition_step"],
                "retry_count": case["retry_count"],
                "next_retry_at": (
                    case["next_retry_at"].isoformat() if case["next_retry_at"] is not None else None
                ),
                "last_error_code": case["last_error_code"],
                "resource_version": case["resource_version"],
                "credential_version": case["credential_version"],
                "write_paused": case["write_paused"],
            },
        }
        forbidden_keys = {
            "resource_id",
            "requested_owner_user_id",
            "database_name",
            "database_owner_role_name",
            "database_migration_role_name",
            "database_migration_secret_ref",
            "database_role_name",
            "database_secret_ref",
            "bucket_name",
            "database_ownership_receipt",
            "role_ownership_receipt",
            "bucket_ownership_receipt",
            "worker_lease_owner",
            "worker_lease_expires_at",
            "fencing_token",
        }
        flattened = list(_walk_json(payload))
        assert forbidden_keys.isdisjoint(key for key, _ in flattened if key is not None)
        assert all("sensitive" not in str(value) for _, value in flattened)

    async with provisioning_session_factory() as session:
        stored_session = await session.scalar(
            select(AuthSession).where(AuthSession.user_id == actor.id)
        )
        assert stored_session is not None
        assert stored_session.last_seen_at == status_now


@pytest.mark.asyncio
async def test_admin_team_status_requires_platform_admin_even_when_actor_is_owner(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor, _, session_token, _ = await _seed_admin_request_identities(
        provisioning_session_factory,
        actor_is_admin=False,
    )
    team = await _seed_team_status(
        provisioning_session_factory,
        owner_user_id=actor.id,
        name="Owned Team",
        state="active",
        provisioning_step="active",
    )
    async with provisioning_session_factory() as session, session.begin():
        session.add(Membership(team_id=team.id, user_id=actor.id, role="team_owner"))
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )

    with TestClient(_admin_test_app(service)) as client:
        client.cookies.set(COOKIE_NAME, session_token)
        response = client.get(
            f"/v1/admin/teams/{team.id}",
            headers=_signed_admin_get_headers(team.id, request_id="req-status-owner"),
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "platform_admin_required"


@pytest.mark.asyncio
async def test_admin_team_status_returns_stable_not_found_for_unknown_team(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, session_token, _ = await _seed_admin_request_identities(provisioning_session_factory)
    missing_team_id = uuid4()
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )

    with TestClient(_admin_test_app(service)) as client:
        client.cookies.set(COOKIE_NAME, session_token)
        response = client.get(
            f"/v1/admin/teams/{missing_team_id}",
            headers=_signed_admin_get_headers(
                missing_team_id,
                request_id="req-status-missing",
            ),
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "team_not_found"


@pytest.mark.asyncio
async def test_admin_team_status_rejects_invalid_session(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, _, _ = await _seed_admin_request_identities(provisioning_session_factory)
    team = await _seed_team_status(
        provisioning_session_factory,
        owner_user_id=owner.id,
        name="Session Team",
        state="requested",
        provisioning_step="requested",
    )
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )

    with TestClient(_admin_test_app(service)) as client:
        client.cookies.set(COOKIE_NAME, "invalid-session-token")
        response = client.get(
            f"/v1/admin/teams/{team.id}",
            headers=_signed_admin_get_headers(team.id, request_id="req-status-session"),
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_admin_team_status_proxy_signature_binds_get_method_and_raw_path(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, session_token, _ = await _seed_admin_request_identities(provisioning_session_factory)
    team = await _seed_team_status(
        provisioning_session_factory,
        owner_user_id=owner.id,
        name="Signed Team",
        state="requested",
        provisioning_step="requested",
    )
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )
    wrong_headers = _signed_admin_get_headers(team.id, request_id="req-status-wrong-method")
    wrong_headers["x-perfpilot-proxy-signature"] = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id="req-status-wrong-method",
        method="POST",
        raw_path=f"/v1/admin/teams/{team.id}".encode(),
    )

    with TestClient(_admin_test_app(service)) as client:
        client.cookies.set(COOKIE_NAME, session_token)
        response = client.get(
            f"/v1/admin/teams/{team.id}",
            headers=wrong_headers,
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proxy_authentication_failed"


@pytest.mark.asyncio
async def test_admin_team_api_returns_202_without_resource_mapping(
    provisioning_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, owner, session_token, _ = await _seed_admin_request_identities(provisioning_session_factory)
    service = AdminTeamService(
        session_factory=provisioning_session_factory,
        clock=lambda: NOW,
    )
    settings = Settings(
        app_env="test",
        proxy_secret="test-proxy-secret",
        allowed_origins=("https://app.example",),
        _env_prefix="PERFPILOT_TEST_ISOLATED_",
        _env_file=None,
        _secrets_dir=None,
    )
    app = create_app(
        testing=True,
        settings_override=settings,
        admin_team_service=service,
        proxy_clock=lambda: 1_700_000_000,
    )
    body = json.dumps(
        {"name": "API Team", "owner_user_id": str(owner.id)},
        separators=(",", ":"),
    ).encode()

    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session_token)
        response = client.post(
            "/v1/admin/teams",
            content=body,
            headers=_signed_admin_headers(body, request_id="req-api-create"),
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert payload["team"]["name"] == "API Team"
    assert payload["team"]["provisioning_state"] == "requested"
    rendered = json.dumps(payload)
    for forbidden in (
        "resource_id",
        "resource_version",
        "database",
        "bucket",
        "secret",
    ):
        assert forbidden not in rendered
