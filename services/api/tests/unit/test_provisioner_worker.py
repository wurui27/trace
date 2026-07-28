import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from perfpilot_api.services.provisioning import ProvisioningInterrupted
from perfpilot_api.workers import provisioner as provisioner_worker
from perfpilot_api.workers.provisioner import ProvisionerWorker


class FakeProvisioner:
    def __init__(self, results: list[object]) -> None:
        self.results = iter(results)
        self.worker_ids: list[str] = []

    async def process_next(self, *, worker_id: str) -> object:
        self.worker_ids.append(worker_id)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_worker_run_once_distinguishes_work_idle_and_recoverable_failure() -> None:
    provisioner = FakeProvisioner(
        [
            SimpleNamespace(state="active"),
            None,
            ProvisioningInterrupted(),
        ]
    )
    worker = ProvisionerWorker(
        provisioner=provisioner,  # type: ignore[arg-type]
        worker_id="provisioner-worker-1",
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is False
    assert await worker.run_once() is False
    assert provisioner.worker_ids == [
        "provisioner-worker-1",
        "provisioner-worker-1",
        "provisioner-worker-1",
    ]


@pytest.mark.asyncio
async def test_worker_close_attempts_every_callback_and_redacts_cleanup_error() -> None:
    events: list[str] = []
    marker = "cleanup-secret-marker"

    class FirstCleanupError(RuntimeError):
        pass

    class SecondCleanupError(RuntimeError):
        pass

    first_error = FirstCleanupError(marker)

    async def close_router() -> None:
        events.append("router")
        raise first_error

    def close_s3() -> None:
        events.append("s3")
        raise SecondCleanupError("second-secret-marker")

    async def close_engine() -> None:
        events.append("engine")

    def close_secret() -> None:
        events.append("secret")

    worker = ProvisionerWorker(
        provisioner=FakeProvisioner([]),  # type: ignore[arg-type]
        worker_id="provisioner-worker-1",
        close_callbacks=(close_secret, close_engine, close_s3, close_router),
    )

    with pytest.raises(RuntimeError, match="^provisioner cleanup failed$") as captured:
        await worker.close()

    assert events == ["router", "s3", "engine", "secret"]
    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_worker_close_finishes_after_cancellation_and_preserves_it() -> None:
    events: list[str] = []

    async def close_router() -> None:
        events.append("router")
        raise asyncio.CancelledError

    def close_s3() -> None:
        events.append("s3")

    async def close_engine() -> None:
        events.append("engine")

    def close_secret() -> None:
        events.append("secret")

    worker = ProvisionerWorker(
        provisioner=FakeProvisioner([]),  # type: ignore[arg-type]
        worker_id="provisioner-worker-1",
        close_callbacks=(close_secret, close_engine, close_s3, close_router),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.close()

    assert events == ["router", "s3", "engine", "secret"]


def test_worker_requires_a_stable_nonempty_identity() -> None:
    with pytest.raises(ValueError, match="worker identity"):
        ProvisionerWorker(
            provisioner=FakeProvisioner([]),  # type: ignore[arg-type]
            worker_id=" ",
        )


def test_build_production_worker_rejects_nonproduction_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="test"),
    )

    with pytest.raises(RuntimeError, match="production environment"):
        provisioner_worker.build_production_worker()


@pytest.mark.parametrize(
    "sslmode",
    ["disable", "allow", "prefer", "require", "verify-ca"],
)
def test_build_production_worker_rejects_weaker_tenant_tls_modes(
    monkeypatch: pytest.MonkeyPatch,
    sslmode: str,
) -> None:
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setenv("PERFPILOT_PROVISIONER_WORKER_ID", "provisioner-worker-1")
    monkeypatch.setenv("PERFPILOT_SITES_ORIGIN", "https://sites.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_CLUSTER_HOST", "tenant-db.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_CLUSTER_SSLMODE", sslmode)

    with pytest.raises(RuntimeError, match="verify-full"):
        provisioner_worker.build_production_worker()


def test_build_production_worker_accepts_production_with_verify_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_secret_loading = False

    class ReachedSecretLoading(Exception):
        pass

    def load_admin_conninfo(_: object) -> bytes:
        return (
            b"user=tenant_admin password=test-password dbname=postgres "
            b"host=tenant-db.example.com sslmode=verify-full"
        )

    def stop_at_secret_loading() -> object:
        nonlocal reached_secret_loading
        reached_secret_loading = True
        raise ReachedSecretLoading("secret-loading-marker")

    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setenv("PERFPILOT_PROVISIONER_WORKER_ID", "provisioner-worker-1")
    monkeypatch.setenv("PERFPILOT_SITES_ORIGIN", "https://sites.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_CLUSTER_HOST", "tenant-db.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_CLUSTER_SSLMODE", "verify-full")
    monkeypatch.setenv("PERFPILOT_TENANT_ADMIN_CONNINFO_FILE", "/unused/admin")
    monkeypatch.setattr(provisioner_worker, "_read_owner_only_file", load_admin_conninfo)
    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        stop_at_secret_loading,
    )

    with pytest.raises(
        RuntimeError,
        match="^provisioner worker is unavailable$",
    ) as captured:
        provisioner_worker.build_production_worker()

    assert reached_secret_loading is True
    assert "secret-loading-marker" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_production_worker_uses_shared_runtime_settings_sigv4_and_closes_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    s3_calls: list[tuple[str, dict[str, object]]] = []

    class SecretValue:
        def get_secret_value(self) -> str:
            return "postgresql+psycopg://control.example/db?sslmode=verify-full"

    class FakeSecretStore:
        def close(self) -> None:
            closed.append("secret")

    class FakeEngine:
        async def dispose(self) -> None:
            closed.append("engine")

    class FakeRouter:
        async def dispose(self) -> None:
            closed.append("router")

    class FakeS3Client:
        def close(self) -> None:
            closed.append("s3")

    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SecretValue(),
        s3_endpoint_url="https://objects.example.com",
        s3_region="cn-north-1",
        tenant_cluster_host="tenant-db.example.com",
        tenant_cluster_port=6432,
        tenant_cluster_sslmode="verify-full",
        secret_keyring_config=Path("/run/secrets/perfpilot/keyring.json"),
        secret_store_root=Path("/var/lib/perfpilot/secrets"),
    )
    secret_store = FakeSecretStore()
    engine = FakeEngine()
    router = FakeRouter()
    s3_client = FakeS3Client()

    monkeypatch.setattr(provisioner_worker, "get_settings", lambda: settings)
    monkeypatch.setenv("PERFPILOT_PROVISIONER_WORKER_ID", "provisioner-worker-1")
    monkeypatch.setenv("PERFPILOT_SITES_ORIGIN", "https://sites.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_ADMIN_CONNINFO_FILE", "/unused/admin")
    monkeypatch.delenv("PERFPILOT_TENANT_CLUSTER_HOST", raising=False)
    monkeypatch.delenv("PERFPILOT_TENANT_CLUSTER_PORT", raising=False)
    monkeypatch.delenv("PERFPILOT_TENANT_CLUSTER_SSLMODE", raising=False)
    monkeypatch.setattr(
        provisioner_worker,
        "_read_owner_only_file",
        lambda _: (
            b"user=tenant_admin password=test-password dbname=postgres "
            b"host=tenant-db.example.com sslmode=verify-full"
        ),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        lambda: secret_store,
    )
    monkeypatch.setattr(
        provisioner_worker,
        "create_control_engine",
        lambda _: engine,
    )
    monkeypatch.setattr(
        provisioner_worker,
        "create_control_session_factory",
        lambda _: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "SqlAlchemyProvisioningRepository",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "SqlAlchemyTenantRouteRepository",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "TenantRouter",
        lambda **kwargs: router,
    )
    monkeypatch.setattr(
        provisioner_worker,
        "PsycopgTenantAdmin",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "AlembicTenantMigrator",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "S3BucketAdmin",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_tenant_replicator",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "Provisioner",
        lambda **kwargs: object(),
    )

    def create_s3(service_name: str, **kwargs: object) -> FakeS3Client:
        s3_calls.append((service_name, kwargs))
        return s3_client

    monkeypatch.setattr(provisioner_worker.boto3, "client", create_s3)

    worker = provisioner_worker.build_production_worker()

    assert len(s3_calls) == 1
    service_name, s3_kwargs = s3_calls[0]
    assert service_name == "s3"
    assert s3_kwargs["endpoint_url"] == "https://objects.example.com"
    assert s3_kwargs["region_name"] == "cn-north-1"
    assert s3_kwargs["config"].signature_version == "s3v4"
    assert "verify" not in s3_kwargs

    await worker.close()
    assert closed == ["router", "s3", "engine", "secret"]


def test_production_worker_rolls_back_partial_build_and_redacts_build_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class BuildFailure(RuntimeError):
        pass

    class CleanupFailure(RuntimeError):
        pass

    marker = "late-constructor-secret-marker"
    build_error = BuildFailure(marker)

    class SecretValue:
        def get_secret_value(self) -> str:
            return "postgresql+psycopg://control.example/db?sslmode=verify-full"

    class FakeSecretStore:
        def close(self) -> None:
            closed.append("secret")

    class FakeEngine:
        async def dispose(self) -> None:
            closed.append("engine")

    class FakeRouter:
        async def dispose(self) -> None:
            closed.append("router")
            raise CleanupFailure("cleanup-secret-marker")

    class FakeS3Client:
        def close(self) -> None:
            closed.append("s3")

    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SecretValue(),
        s3_endpoint_url="https://objects.example.com",
        s3_region="cn-north-1",
        tenant_cluster_host="tenant-db.example.com",
        tenant_cluster_port=6432,
        tenant_cluster_sslmode="verify-full",
        secret_keyring_config=Path("/run/secrets/perfpilot/keyring.json"),
        secret_store_root=Path("/var/lib/perfpilot/secrets"),
    )
    secret_store = FakeSecretStore()
    engine = FakeEngine()
    router = FakeRouter()
    s3_client = FakeS3Client()

    monkeypatch.setattr(provisioner_worker, "get_settings", lambda: settings)
    monkeypatch.setenv("PERFPILOT_PROVISIONER_WORKER_ID", "provisioner-worker-1")
    monkeypatch.setenv("PERFPILOT_SITES_ORIGIN", "https://sites.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_ADMIN_CONNINFO_FILE", "/unused/admin")
    monkeypatch.delenv("PERFPILOT_TENANT_CLUSTER_HOST", raising=False)
    monkeypatch.delenv("PERFPILOT_TENANT_CLUSTER_PORT", raising=False)
    monkeypatch.delenv("PERFPILOT_TENANT_CLUSTER_SSLMODE", raising=False)
    monkeypatch.setattr(
        provisioner_worker,
        "_read_owner_only_file",
        lambda _: (
            b"user=tenant_admin password=test-password dbname=postgres "
            b"host=tenant-db.example.com sslmode=verify-full"
        ),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        lambda: secret_store,
    )
    monkeypatch.setattr(provisioner_worker, "create_control_engine", lambda _: engine)
    monkeypatch.setattr(
        provisioner_worker,
        "create_control_session_factory",
        lambda _: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "SqlAlchemyProvisioningRepository",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "SqlAlchemyTenantRouteRepository",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(provisioner_worker, "TenantRouter", lambda **kwargs: router)
    monkeypatch.setattr(
        provisioner_worker,
        "PsycopgTenantAdmin",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "AlembicTenantMigrator",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "S3BucketAdmin",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_tenant_replicator",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "Provisioner",
        lambda **kwargs: (_ for _ in ()).throw(build_error),
    )
    monkeypatch.setattr(
        provisioner_worker.boto3,
        "client",
        lambda *args, **kwargs: s3_client,
    )

    with pytest.raises(
        RuntimeError,
        match="^provisioner worker is unavailable$",
    ) as captured:
        provisioner_worker.build_production_worker()

    assert closed == ["router", "s3", "engine", "secret"]
    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "admin_conninfo",
    [
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=disable"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=allow"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=prefer"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=require"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=verify-ca"
        ),
        (
            "password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=verify-full"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker "
            "host=tenant-db.example.com sslmode=verify-full"
        ),
        ("user=tenant_admin password=conninfo-secret-marker dbname=postgres sslmode=verify-full"),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=verify-full service=unexpected"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=verify-full "
            "servicefile=/mounted/service.conf"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=verify-full passfile=/mounted/passfile"
        ),
        (
            "user=tenant_admin user=ambiguous password=conninfo-secret-marker "
            "dbname=postgres host=tenant-db.example.com sslmode=verify-full"
        ),
        (
            "user=tenant_admin password=conninfo-secret-marker dbname=postgres "
            "host=tenant-db.example.com sslmode=disable sslmode=verify-full"
        ),
        (
            "postgresql://tenant_admin:conninfo-secret-marker@tenant-db.example.com/"
            "postgres?sslmode=verify-full&sslmode=disable"
        ),
    ],
    ids=[
        "missing-sslmode",
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "missing-user",
        "missing-dbname",
        "missing-host",
        "service",
        "servicefile",
        "passfile",
        "duplicate-user",
        "duplicate-sslmode",
        "duplicate-uri-sslmode",
    ],
)
def test_build_production_worker_rejects_unsafe_admin_conninfo_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    admin_conninfo: str,
) -> None:
    def forbidden_dependency(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("dependencies must not be constructed")

    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setenv("PERFPILOT_PROVISIONER_WORKER_ID", "provisioner-worker-1")
    monkeypatch.setenv("PERFPILOT_SITES_ORIGIN", "https://sites.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_CLUSTER_HOST", "tenant-db.example.com")
    monkeypatch.setenv("PERFPILOT_TENANT_CLUSTER_SSLMODE", "verify-full")
    monkeypatch.setenv("PERFPILOT_TENANT_ADMIN_CONNINFO_FILE", "/unused/admin")
    monkeypatch.setattr(
        provisioner_worker,
        "_read_owner_only_file",
        lambda _: admin_conninfo.encode(),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        forbidden_dependency,
    )
    monkeypatch.setattr(
        provisioner_worker,
        "create_control_engine",
        forbidden_dependency,
    )
    monkeypatch.setattr(
        provisioner_worker.boto3,
        "client",
        forbidden_dependency,
    )

    with pytest.raises(
        RuntimeError,
        match="tenant admin connection configuration is invalid",
    ) as exc_info:
        provisioner_worker.build_production_worker()

    rendered_error = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "conninfo-secret-marker" not in rendered_error
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_tenant_replicator_defaults_to_builtin_without_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpectedReplicator:
        async def copy_and_validate(self, **kwargs: object) -> None:
            del kwargs

    expected = ExpectedReplicator()
    received: list[dict[str, object]] = []

    def builtin_factory(**kwargs: object) -> object:
        received.append(kwargs)
        return expected

    monkeypatch.delenv("PERFPILOT_TENANT_REPLICATOR_FACTORY", raising=False)
    monkeypatch.setattr(
        provisioner_worker,
        "PsycopgTenantReplicator",
        builtin_factory,
        raising=False,
    )

    replicator = provisioner_worker._build_tenant_replicator(
        cluster_host="tenant-db.example.com",
        cluster_port=5432,
        sslmode="verify-full",
    )

    assert replicator is expected
    assert received == [
        {
            "cluster_host": "tenant-db.example.com",
            "cluster_port": 5432,
            "sslmode": "verify-full",
        }
    ]


def test_secret_store_maintenance_entrypoint_rotates_and_closes_the_configured_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSecretStore:
        def __init__(self) -> None:
            self.rotations: list[tuple[str, Path]] = []
            self.closed = False

        async def rotate(self, *, new_key_id: str, new_key_file: Path) -> None:
            self.rotations.append((new_key_id, new_key_file))

        def close(self) -> None:
            self.closed = True

    store = FakeSecretStore()
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        lambda: store,
        raising=False,
    )

    provisioner_worker.secret_maintenance_main(
        ["rotate", "--key-id", "key-2", "--key-file", "/mounted/key-2"]
    )

    assert store.rotations == [("key-2", Path("/mounted/key-2"))]
    assert store.closed is True


def test_secret_store_maintenance_entrypoint_retires_and_closes_the_configured_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSecretStore:
        def __init__(self) -> None:
            self.retired_key_ids: list[str] = []
            self.closed = False

        async def retire_key(self, key_id: str) -> None:
            self.retired_key_ids.append(key_id)

        def close(self) -> None:
            self.closed = True

    store = FakeSecretStore()
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        lambda: store,
    )

    provisioner_worker.secret_maintenance_main(["retire", "--key-id", "key-1"])

    assert store.retired_key_ids == ["key-1"]
    assert store.closed is True


def test_secret_store_maintenance_rejects_nonproduction_before_loading_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="test"),
    )

    def reject_secret_loading() -> object:
        raise AssertionError("secret configuration must not be loaded")

    monkeypatch.setattr(
        provisioner_worker,
        "_build_configured_secret_store",
        reject_secret_loading,
    )

    with pytest.raises(RuntimeError, match="production environment"):
        provisioner_worker.secret_maintenance_main(["retire", "--key-id", "key-1"])


def test_secret_store_maintenance_rejects_an_insecure_keyring_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "secrets"
    store_root.mkdir(mode=0o700)
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    keyring_config = tmp_path / "keyring.json"
    keyring_config.write_text(
        json.dumps(
            {
                "active_key_id": "key-1",
                "keys": {"key-1": str(key_file)},
            }
        )
    )
    keyring_config.chmod(0o644)
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setenv("PERFPILOT_SECRET_KEYRING_CONFIG", str(keyring_config))
    monkeypatch.setenv("PERFPILOT_SECRET_STORE_ROOT", str(store_root))

    with pytest.raises(RuntimeError, match="secret file permissions"):
        provisioner_worker.secret_maintenance_main(["retire", "--key-id", "key-1"])


def test_secret_store_maintenance_rejects_an_insecure_master_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "secrets"
    store_root.mkdir(mode=0o700)
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o640)
    keyring_config = tmp_path / "keyring.json"
    keyring_config.write_text(
        json.dumps(
            {
                "active_key_id": "key-1",
                "keys": {"key-1": str(key_file)},
            }
        )
    )
    keyring_config.chmod(0o600)
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setenv("PERFPILOT_SECRET_KEYRING_CONFIG", str(keyring_config))
    monkeypatch.setenv("PERFPILOT_SECRET_STORE_ROOT", str(store_root))

    with pytest.raises(RuntimeError, match="master key.*permissions"):
        provisioner_worker.secret_maintenance_main(["retire", "--key-id", "key-1"])


def test_secret_store_maintenance_is_registered_as_an_explicit_worker_entrypoint() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"

    project = tomllib.loads(pyproject.read_text())

    assert project["project"]["scripts"]["perfpilot-secret-maintenance"] == (
        "perfpilot_api.workers.provisioner:secret_maintenance_main"
    )
