import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from perfpilot_api.runtime import artifacts as artifact_runtime
from perfpilot_api.runtime.artifacts import ArtifactRuntimeError, build_artifact_runtime
from perfpilot_api.runtime.secrets import build_configured_secret_store


class FakeSecretValue:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        s3_endpoint_url="https://objects.example.com",
        s3_region="cn-north-1",
        tenant_cluster_host="tenant-db.example.com",
        tenant_cluster_port=6432,
        tenant_cluster_sslmode="verify-full",
        secret_keyring_config=Path("/run/secrets/perfpilot/keyring.json"),
        secret_store_root=Path("/var/lib/perfpilot/secrets"),
    )


def test_shared_secret_builder_loads_owner_only_keyring(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
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
    keyring_config.chmod(0o600)

    store = build_configured_secret_store(
        keyring_config=keyring_config,
        secret_store_root=store_root,
    )

    try:
        assert store.allocate_reference().startswith("secret://")
    finally:
        store.close()


def test_shared_secret_builder_redacts_invalid_keyring_payload(tmp_path: Path) -> None:
    marker = "key-id-secret-marker"
    store_root = tmp_path / "store"
    store_root.mkdir(mode=0o700)
    keyring_config = tmp_path / "keyring.json"
    keyring_config.write_text(json.dumps({"active_key_id": marker, "keys": {marker: 42}}))
    keyring_config.chmod(0o600)

    with pytest.raises(RuntimeError) as captured:
        build_configured_secret_store(
            keyring_config=keyring_config,
            secret_store_root=store_root,
        )

    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "key_path",
    ["relative/master.key", "/", "/run/secrets/../master.key"],
)
def test_shared_secret_builder_rejects_ambiguous_master_key_paths(
    tmp_path: Path,
    key_path: str,
) -> None:
    store_root = tmp_path / "store"
    store_root.mkdir(mode=0o700)
    keyring_config = tmp_path / "keyring.json"
    keyring_config.write_text(
        json.dumps(
            {
                "active_key_id": "key-1",
                "keys": {"key-1": key_path},
            }
        )
    )
    keyring_config.chmod(0o600)

    with pytest.raises(
        RuntimeError,
        match="secret keyring configuration is invalid",
    ) as captured:
        build_configured_secret_store(
            keyring_config=keyring_config,
            secret_store_root=store_root,
        )

    assert key_path not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "payload",
    [
        ('{"active_key_id":"key-1","active_key_id":"key-2","keys":{"key-1":"/run/key-1"}}'),
        ('{"active_key_id":"key-1","keys":{"key-1":"/run/key-1","key-1":"/run/key-2"}}'),
    ],
)
def test_shared_secret_builder_rejects_duplicate_json_keys(
    tmp_path: Path,
    payload: str,
) -> None:
    store_root = tmp_path / "store"
    store_root.mkdir(mode=0o700)
    keyring_config = tmp_path / "keyring.json"
    keyring_config.write_text(payload)
    keyring_config.chmod(0o600)

    with pytest.raises(
        RuntimeError,
        match="secret keyring configuration is invalid",
    ) as captured:
        build_configured_secret_store(
            keyring_config=keyring_config,
            secret_store_root=store_root,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_artifact_runtime_builds_authoritative_dependencies_with_sigv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeSecretStore:
        def close(self) -> None:
            events.append(("secret.close", None))

    class FakeRouter:
        async def dispose(self) -> None:
            events.append(("router.dispose", None))

    class FakeS3Client:
        def close(self) -> None:
            events.append(("s3.close", None))

    secret_store = FakeSecretStore()
    router = FakeRouter()
    s3_client = FakeS3Client()
    route_repository = object()
    bucket_resolver = object()
    repository = object()
    artifact_store = object()
    upload_service = object()
    session_factory = object()

    monkeypatch.setattr(
        artifact_runtime,
        "build_configured_secret_store",
        lambda **kwargs: events.append(("secret", kwargs)) or secret_store,
    )
    monkeypatch.setattr(
        artifact_runtime,
        "SqlAlchemyTenantRouteRepository",
        lambda **kwargs: events.append(("route_repository", kwargs)) or route_repository,
    )
    monkeypatch.setattr(
        artifact_runtime,
        "TenantRouter",
        lambda **kwargs: events.append(("router", kwargs)) or router,
    )
    monkeypatch.setattr(
        artifact_runtime.upload_core,
        "SQLAlchemyTenantBucketResolver",
        lambda **kwargs: events.append(("bucket_resolver", kwargs)) or bucket_resolver,
        raising=False,
    )
    monkeypatch.setattr(
        artifact_runtime.upload_core,
        "SQLAlchemyUploadRepository",
        lambda **kwargs: events.append(("upload_repository", kwargs)) or repository,
        raising=False,
    )
    monkeypatch.setattr(
        artifact_runtime,
        "S3ArtifactStore",
        lambda **kwargs: events.append(("artifact_store", kwargs)) or artifact_store,
    )
    monkeypatch.setattr(
        artifact_runtime.upload_core,
        "UploadService",
        lambda **kwargs: events.append(("upload_service", kwargs)) or upload_service,
    )

    def create_client(service_name: str, **kwargs: object) -> FakeS3Client:
        events.append(("s3", {"service_name": service_name, **kwargs}))
        return s3_client

    monkeypatch.setattr(artifact_runtime.boto3, "client", create_client)

    runtime = await build_artifact_runtime(
        settings=_settings(),
        control_session_factory=session_factory,  # type: ignore[arg-type]
    )

    assert runtime.upload_service is upload_service
    assert ("route_repository", {"session_factory": session_factory}) in events
    assert (
        "bucket_resolver",
        {"session_factory": session_factory},
    ) in events
    assert ("upload_repository", {"tenant_router": router}) in events
    s3_kwargs = next(value for name, value in events if name == "s3")
    assert isinstance(s3_kwargs, dict)
    assert s3_kwargs["service_name"] == "s3"
    assert s3_kwargs["endpoint_url"] == "https://objects.example.com"
    assert s3_kwargs["region_name"] == "cn-north-1"
    assert s3_kwargs["config"].signature_version == "s3v4"
    assert "verify" not in s3_kwargs

    await runtime.close()
    assert events[-3:] == [
        ("router.dispose", None),
        ("s3.close", None),
        ("secret.close", None),
    ]


@pytest.mark.asyncio
async def test_artifact_runtime_cleans_partial_build_and_redacts_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    marker = "constructor-secret-marker"

    class FakeSecretStore:
        def close(self) -> None:
            events.append("secret")

    class FakeRouter:
        async def dispose(self) -> None:
            events.append("router")

    class FakeS3Client:
        def close(self) -> None:
            events.append("s3")

    monkeypatch.setattr(
        artifact_runtime,
        "build_configured_secret_store",
        lambda **kwargs: FakeSecretStore(),
    )
    monkeypatch.setattr(
        artifact_runtime,
        "SqlAlchemyTenantRouteRepository",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(artifact_runtime, "TenantRouter", lambda **kwargs: FakeRouter())
    monkeypatch.setattr(
        artifact_runtime.upload_core,
        "SQLAlchemyTenantBucketResolver",
        lambda **kwargs: object(),
        raising=False,
    )
    monkeypatch.setattr(
        artifact_runtime.upload_core,
        "SQLAlchemyUploadRepository",
        lambda **kwargs: object(),
        raising=False,
    )
    monkeypatch.setattr(
        artifact_runtime.boto3,
        "client",
        lambda *args, **kwargs: FakeS3Client(),
    )
    monkeypatch.setattr(artifact_runtime, "S3ArtifactStore", lambda **kwargs: object())
    monkeypatch.setattr(
        artifact_runtime.upload_core,
        "UploadService",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError(marker)),
    )

    with pytest.raises(ArtifactRuntimeError) as captured:
        await build_artifact_runtime(
            settings=_settings(),
            control_session_factory=object(),  # type: ignore[arg-type]
        )

    assert events == ["router", "s3", "secret"]
    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_artifact_runtime_close_finishes_after_cancellation_and_can_retry() -> None:
    events: list[str] = []
    cancel_router = True

    class FakeRouter:
        async def dispose(self) -> None:
            events.append("router")
            if cancel_router:
                raise asyncio.CancelledError

    class FakeClient:
        def close(self) -> None:
            events.append("s3")

    class FakeSecretStore:
        def close(self) -> None:
            events.append("secret")

    runtime = artifact_runtime.ArtifactRuntime(
        upload_service=object(),
        tenant_router=FakeRouter(),
        s3_client=FakeClient(),
        secret_store=FakeSecretStore(),
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime.close()

    assert events == ["router", "s3", "secret"]
    cancel_router = False
    await runtime.close()
    assert events == ["router", "s3", "secret", "router", "s3", "secret"]


@pytest.mark.asyncio
async def test_artifact_runtime_close_attempts_all_steps_and_redacts_cleanup_error() -> None:
    events: list[str] = []
    marker = "cleanup-secret-marker"

    class FirstCleanupError(RuntimeError):
        pass

    class SecondCleanupError(RuntimeError):
        pass

    first_error = FirstCleanupError(marker)

    class FakeRouter:
        async def dispose(self) -> None:
            events.append("router")
            raise first_error

    class FakeClient:
        def close(self) -> None:
            events.append("s3")
            raise SecondCleanupError("second")

    class FakeSecretStore:
        def close(self) -> None:
            events.append("secret")

    runtime = artifact_runtime.ArtifactRuntime(
        upload_service=object(),
        tenant_router=FakeRouter(),
        s3_client=FakeClient(),
        secret_store=FakeSecretStore(),
    )

    with pytest.raises(ArtifactRuntimeError) as captured:
        await runtime.close()

    assert events == ["router", "s3", "secret"]
    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
