import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

import perfpilot_api.config as config
import perfpilot_api.errors as errors
import perfpilot_api.main as main
from perfpilot_api.config import Settings
from perfpilot_api.main import create_app


def test_health_returns_request_id() -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get("/v1/health", headers={"x-request-id": "req-health"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"] == "req-health"


def test_production_app_rejects_development_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.get_settings.cache_clear()
    monkeypatch.setenv("PERFPILOT_APP_ENV", "production")
    try:
        with pytest.raises(ValidationError, match="production secret"):
            create_app(testing=False)
    finally:
        config.get_settings.cache_clear()


def test_testing_app_ignores_hostile_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.get_settings.cache_clear()
    monkeypatch.setenv("PERFPILOT_APP_ENV", "production")
    monkeypatch.setenv("PERFPILOT_S3_ENDPOINT_URL", "not-a-url")
    monkeypatch.setenv("PERFPILOT_ALLOWED_ORIGINS", '["not-a-url"]')
    try:
        app = create_app(testing=True)
        with TestClient(app) as client:
            response = client.get("/v1/health")
    finally:
        config.get_settings.cache_clear()

    assert response.status_code == 200
    assert app.state.settings.app_env == "test"


def test_development_app_does_not_build_production_artifact_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("production artifact dependencies must not be built")

    monkeypatch.setattr(main, "create_control_engine", forbidden)
    monkeypatch.setattr(main, "build_artifact_runtime", forbidden)
    app = create_app(
        testing=False,
        settings_override=Settings(app_env="development", _env_file=None),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with TestClient(app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert app.state.upload_service is None


def test_unknown_route_uses_stable_error_shape() -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get("/v1/missing", headers={"x-request-id": "req-404"})
    assert response.status_code == 404
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "route_not_found",
            "message": "请求的接口不存在",
            "retryable": False,
            "request_id": "req-404",
        },
    }


def test_production_rejects_development_secrets() -> None:
    with pytest.raises(ValidationError, match="production secret"):
        Settings(app_env="production")


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "control_database_url": (
            "postgresql+psycopg://perfpilot:test-password@db.example.com:5432/"
            "perfpilot_control?sslmode=verify-full"
        ),
        "redis_url": "rediss://cache.example.com:6380/0",
        "s3_endpoint_url": "https://objects.example.com",
        "s3_region": "cn-north-1",
        "tenant_cluster_host": "tenant-db.example.com",
        "tenant_cluster_port": 6432,
        "tenant_cluster_sslmode": "verify-full",
        "secret_keyring_config": "/run/secrets/perfpilot/keyring.json",
        "secret_store_root": "/var/lib/perfpilot/secrets",
        "apkanalyzer_binary": "/opt/android-sdk/cmdline-tools/latest/bin/apkanalyzer",
        "proxy_secret": "test-production-proxy-secret",
        "session_secret": "test-production-session-secret",
        "jws_signing_key_reference": "kms://keys/perfpilot-signing",
        "agent_registration_secret_reference": "vault://secrets/agent-registration",
        "allowed_origins": ["https://console.example.com"],
    }
    values.update(overrides)
    return Settings(**values)


def test_valid_explicit_production_settings() -> None:
    settings = _production_settings()

    assert settings.app_env == "production"
    assert [origin.scheme for origin in settings.allowed_origins] == ["https"]
    assert settings.s3_region == "cn-north-1"
    assert settings.tenant_cluster_host == "tenant-db.example.com"
    assert settings.tenant_cluster_port == 6432
    assert settings.tenant_cluster_sslmode == "verify-full"
    assert settings.secret_keyring_config == Path("/run/secrets/perfpilot/keyring.json")
    assert settings.secret_store_root == Path("/var/lib/perfpilot/secrets")
    assert settings.apkanalyzer_binary == Path(
        "/opt/android-sdk/cmdline-tools/latest/bin/apkanalyzer"
    )


def test_production_refuses_owned_in_process_apk_inspector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        async def dispose(self) -> None:
            pass

    class FakeArtifactRuntime:
        upload_service = object()
        apk_inspector = object()
        tenant_router = object()

        async def close(self) -> None:
            pass

    monkeypatch.setattr(main, "create_control_engine", lambda _: FakeEngine())
    monkeypatch.setattr(main, "create_control_session_factory", lambda _: object())

    async def build_artifact_runtime(**kwargs: object) -> FakeArtifactRuntime:
        return FakeArtifactRuntime()

    monkeypatch.setattr(main, "build_artifact_runtime", build_artifact_runtime)
    app = create_app(
        testing=False,
        settings_override=_production_settings(),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with pytest.raises(RuntimeError, match="externally isolated"):
        with TestClient(app):
            pass


def test_production_rejects_injected_analysis_service_without_apk_inspector() -> None:
    app = create_app(
        testing=False,
        settings_override=_production_settings(),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        upload_service=object(),  # type: ignore[arg-type]
        analysis_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with pytest.raises(RuntimeError, match="externally isolated"):
        with TestClient(app):
            pass


def test_production_app_cleanup_attempts_every_dependency_and_redacts_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    marker = "cleanup-secret-marker"

    class FakeEngine:
        async def dispose(self) -> None:
            events.append("engine")

    class FakeArtifactRuntime:
        upload_service = object()
        apk_inspector = object()
        tenant_router = object()

        async def close(self) -> None:
            events.append("artifacts")
            raise RuntimeError(marker)

    monkeypatch.setattr(main, "create_control_engine", lambda _: FakeEngine())
    monkeypatch.setattr(main, "create_control_session_factory", lambda _: object())

    async def build_artifact_runtime(**kwargs: object) -> FakeArtifactRuntime:
        return FakeArtifactRuntime()

    monkeypatch.setattr(main, "build_artifact_runtime", build_artifact_runtime)
    app = create_app(
        testing=False,
        settings_override=_production_settings(),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        apk_inspector=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with pytest.raises(RuntimeError, match="Application dependency cleanup failed") as captured:
        with TestClient(app) as client:
            assert client.get("/v1/health").status_code == 200

    assert events == ["artifacts", "engine"]
    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_production_app_cleanup_preserves_late_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeEngine:
        async def dispose(self) -> None:
            events.append("engine")
            raise asyncio.CancelledError

    class FakeArtifactRuntime:
        upload_service = object()
        apk_inspector = object()
        tenant_router = object()

        async def close(self) -> None:
            events.append("artifacts")
            raise RuntimeError("ordinary cleanup failure")

    monkeypatch.setattr(main, "create_control_engine", lambda _: FakeEngine())
    monkeypatch.setattr(main, "create_control_session_factory", lambda _: object())

    async def build_artifact_runtime(**kwargs: object) -> FakeArtifactRuntime:
        return FakeArtifactRuntime()

    monkeypatch.setattr(main, "build_artifact_runtime", build_artifact_runtime)
    app = create_app(
        testing=False,
        settings_override=_production_settings(),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        apk_inspector=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with pytest.raises(asyncio.CancelledError):
        async with app.router.lifespan_context(app):
            pass

    assert events == ["artifacts", "engine"]


@pytest.mark.asyncio
async def test_production_app_startup_cancellation_survives_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeEngine:
        async def dispose(self) -> None:
            events.append("engine")
            raise RuntimeError("ordinary cleanup failure")

    monkeypatch.setattr(main, "create_control_engine", lambda _: FakeEngine())
    monkeypatch.setattr(main, "create_control_session_factory", lambda _: object())

    async def build_artifact_runtime(**kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "build_artifact_runtime", build_artifact_runtime)
    app = create_app(
        testing=False,
        settings_override=_production_settings(),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        apk_inspector=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with pytest.raises(asyncio.CancelledError):
        async with app.router.lifespan_context(app):
            pass

    assert events == ["engine"]


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_nonproduction_keeps_local_plaintext_service_defaults(app_env: str) -> None:
    settings = Settings(app_env=app_env)

    assert settings.control_database_url.get_secret_value().startswith("postgresql+psycopg://")
    assert settings.redis_url.get_secret_value().startswith("redis://")
    assert str(settings.s3_endpoint_url).startswith("http://")
    assert settings.s3_region == "us-east-1"
    assert settings.tenant_cluster_host == "127.0.0.1"
    assert settings.tenant_cluster_port == 5432
    assert settings.tenant_cluster_sslmode == "disable"


@pytest.mark.parametrize(
    "sslmode",
    ["disable", "allow", "prefer", "require", "verify-ca"],
)
def test_production_tenant_cluster_requires_verify_full(sslmode: str) -> None:
    with pytest.raises(ValidationError, match="tenant cluster") as exc_info:
        _production_settings(tenant_cluster_sslmode=sslmode)

    assert exc_info.value.errors()[0]["input"] == "[redacted]"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_cluster_host", ""),
        ("tenant_cluster_host", "db.example.com/override"),
        ("tenant_cluster_host", "db-a.example.com,db-b.example.com"),
        ("tenant_cluster_host", "user@db.example.com"),
        ("tenant_cluster_port", 0),
        ("tenant_cluster_port", 65536),
        ("s3_region", ""),
        ("s3_region", "region/override"),
        ("secret_keyring_config", ""),
        ("secret_keyring_config", "relative/keyring.json"),
        ("secret_keyring_config", "/"),
        ("secret_store_root", ""),
        ("secret_store_root", "relative/secrets"),
        ("secret_store_root", "/"),
        ("apkanalyzer_binary", "relative/apkanalyzer"),
        ("apkanalyzer_binary", "/"),
    ],
)
def test_production_rejects_invalid_artifact_runtime_settings(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _production_settings(**{field: value})

    assert exc_info.value.errors()[0]["input"] == "[redacted]"


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "127.1", "[::1]", "2130706433"],
)
def test_production_rejects_loopback_tenant_cluster_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="artifact runtime") as exc_info:
        _production_settings(tenant_cluster_host=host)

    assert exc_info.value.errors()[0]["input"] == "[redacted]"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?sslmode=disable",
        "?sslmode=allow",
        "?sslmode=prefer",
        "?sslmode=require",
        "?sslmode=verify-ca",
        "?sslmode=verify-full&sslmode=disable",
        "?sslmode=disable&sslmode=verify-full",
        "?sslmode=verify-full&SSLMode=disable",
        "?sslmode=verify-full&application_name=a&application_name=b",
        "?sslmode=verify-full&host=127.0.0.1",
        "?sslmode=verify-full&service=attacker",
        "?sslmode=verify-full&servicefile=%2Ftmp%2Fpg_service.conf",
        "?sslmode=verify-full&passfile=%2Ftmp%2Fpgpass",
        "?sslmode=verify-full&options=-c%20search_path%3Dpublic",
    ],
    ids=[
        "missing",
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "duplicate-weak-last",
        "duplicate-strong-last",
        "confusable-case",
        "duplicate-other-query-key",
        "host-override",
        "service-override",
        "servicefile-override",
        "passfile-override",
        "options-override",
    ],
)
def test_production_control_database_requires_unambiguous_verify_full(
    query: str,
) -> None:
    marker = "database-url-secret-marker"
    database_url = f"postgresql+psycopg://perfpilot:{marker}@db.example.com:5432/control{query}"

    with pytest.raises(ValidationError, match="production service URL") as exc_info:
        _production_settings(control_database_url=database_url)

    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value.errors())
    assert marker not in exc_info.value.json()
    assert exc_info.value.errors()[0]["input"] == "[redacted]"


def test_production_requires_tls_for_redis_and_s3() -> None:
    redis_marker = "redis-url-secret-marker"
    with pytest.raises(ValidationError, match="production service URL") as redis_error:
        _production_settings(redis_url=f"redis://:{redis_marker}@cache.example.com:6379/0")

    s3_marker = "s3-url-secret-marker"
    with pytest.raises(ValidationError, match="production service URL") as s3_error:
        _production_settings(s3_endpoint_url=f"http://objects.example.com/{s3_marker}")

    for marker, error in (
        (redis_marker, redis_error.value),
        (s3_marker, s3_error.value),
    ):
        assert marker not in str(error)
        assert marker not in repr(error.errors())
        assert marker not in error.json()
        assert error.errors()[0]["input"] == "[redacted]"


@pytest.mark.parametrize(
    "override",
    [
        {
            "control_database_url": (
                "postgresql+psycopg://perfpilot:perfpilot@127.0.0.1:5432/perfpilot_control"
            )
        },
        {"redis_url": "redis://127.0.0.1:6379/0"},
        {"s3_endpoint_url": "http://127.0.0.1:9000"},
        {"proxy_secret": "development-only-proxy-secret"},
        {"session_secret": "development-only-session-secret"},
        {"jws_signing_key_reference": ("development-only-jws-signing-key-reference")},
        {
            "agent_registration_secret_reference": (
                "development-only-agent-registration-secret-reference"
            )
        },
        {"allowed_origins": ["http://127.0.0.1:3000"]},
    ],
)
def test_production_rejects_each_development_default(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="production secret"):
        _production_settings(**override)


@pytest.mark.parametrize(
    "field",
    [
        "proxy_secret",
        "session_secret",
        "jws_signing_key_reference",
        "agent_registration_secret_reference",
    ],
)
def test_production_rejects_empty_required_secrets(field: str) -> None:
    with pytest.raises(ValidationError, match="production secret"):
        _production_settings(**{field: "  "})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("control_database_url", ""),
        (
            "control_database_url",
            "postgresql+psycopg://user:url-secret-marker@[invalid",
        ),
        (
            "control_database_url",
            "postgresql+psycopg://user:url-secret-marker@/control",
        ),
        (
            "control_database_url",
            "postgresql://user:url-secret-marker@db.example.com/control",
        ),
        (
            "control_database_url",
            "postgresql+psycopg://user:url-secret-marker@db.example.com:notaport/control",
        ),
        (
            "control_database_url",
            "postgresql+psycopg://user:url-secret-marker@db.example.com:65536/control",
        ),
        (
            "control_database_url",
            "postgresql+psycopg://user:url-secret-marker@db example.com:5432/control",
        ),
        (
            "control_database_url",
            "postgresql+psycopg://user:url-secret-marker@db^example.com:5432/control",
        ),
        ("redis_url", ""),
        ("redis_url", "redis://:url-secret-marker@[invalid"),
        ("redis_url", "redis://:url-secret-marker@/0"),
        ("redis_url", "http://:url-secret-marker@cache.example.com/0"),
        ("redis_url", "redis://:url-secret-marker@cache.example.com:notaport/0"),
        ("redis_url", "redis://:url-secret-marker@cache.example.com:65536/0"),
        ("redis_url", "redis://:url-secret-marker@cache example.com:6379/0"),
        ("redis_url", "redis://:url-secret-marker@cache^example.com:6379/0"),
        ("s3_endpoint_url", "https://url-secret-marker[invalid"),
    ],
    ids=[
        "database-empty",
        "database-malformed",
        "database-missing-host",
        "database-wrong-scheme",
        "database-nonnumeric-port",
        "database-out-of-range-port",
        "database-space-in-host",
        "database-illegal-host",
        "redis-empty",
        "redis-malformed",
        "redis-missing-host",
        "redis-wrong-scheme",
        "redis-nonnumeric-port",
        "redis-out-of-range-port",
        "redis-space-in-host",
        "redis-illegal-host",
        "s3-malformed",
    ],
)
def test_production_rejects_invalid_service_url_structure(
    field: str,
    value: str,
) -> None:
    with pytest.raises(
        ValidationError,
        match="production service URL configuration is invalid",
    ) as exc_info:
        _production_settings(**{field: value})

    assert "url-secret-marker" not in repr(exc_info.value.errors())
    assert "url-secret-marker" not in exc_info.value.json()
    assert exc_info.value.errors()[0]["input"] == "[redacted]"


def test_production_accepts_tls_database_and_redis_urls() -> None:
    redis_url = "rediss://cache.example.com:6380/0"
    settings = _production_settings(
        control_database_url=(
            "postgresql+psycopg://user:test-password@db.example.com:5432/"
            "control?sslmode=verify-full"
        ),
        redis_url=redis_url,
    )

    assert settings.app_env == "production"
    assert settings.redis_url.get_secret_value() == redis_url


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://user:password@localhost:6432/control?sslmode=verify-full",
        "postgresql+psycopg://user:password@127.0.0.2:6432/control?sslmode=verify-full",
        "postgresql+psycopg://user:password@[::1]:6432/control?sslmode=verify-full",
        "postgresql+psycopg://user:password@127.1:6432/control?sslmode=verify-full",
        "postgresql+psycopg://user:password@2130706433:6432/control?sslmode=verify-full",
        "postgresql+psycopg://user:password@017700000001:6432/control?sslmode=verify-full",
        "postgresql+psycopg://user:password@0x7f000001:6432/control?sslmode=verify-full",
    ],
)
def test_production_rejects_loopback_database_hosts(database_url: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        _production_settings(control_database_url=database_url)


@pytest.mark.parametrize(
    "loopback_host",
    ["127.0.0.1", "127.1"],
)
def test_production_rejects_loopback_in_any_database_host(
    loopback_host: str,
) -> None:
    database_url = (
        "postgresql+psycopg://user:url-secret-marker@db.example.com:5432,"
        f"{loopback_host}:5432/control?sslmode=verify-full"
    )

    with pytest.raises(ValidationError, match="loopback") as exc_info:
        _production_settings(control_database_url=database_url)

    assert "url-secret-marker" not in repr(exc_info.value.errors())
    assert "url-secret-marker" not in exc_info.value.json()
    assert exc_info.value.errors()[0]["input"] == "[redacted]"


def test_production_accepts_non_loopback_multi_host_database_url() -> None:
    database_url = (
        "postgresql+psycopg://user:password@db-a.example.com:5432,"
        "db-b.example.com:5432/control?sslmode=verify-full"
    )

    settings = _production_settings(control_database_url=database_url)

    assert settings.control_database_url.get_secret_value() == database_url


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://localhost:9100",
        "https://127.0.0.2:9100",
        "https://[::1]:9100",
    ],
)
def test_production_rejects_loopback_object_endpoint_hosts(endpoint_url: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        _production_settings(s3_endpoint_url=endpoint_url)


@pytest.mark.parametrize(
    ("origins", "error"),
    [
        ([], "at least one"),
        (["http://console.example.com"], "HTTPS"),
        (
            ["https://console.example.com", "http://admin.example.com"],
            "HTTPS",
        ),
    ],
)
def test_production_rejects_missing_or_insecure_origins(
    origins: list[str],
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        _production_settings(allowed_origins=origins)


def test_allowed_origins_uses_a_default_factory() -> None:
    assert Settings.model_fields["allowed_origins"].default_factory is not None


def test_allowed_origins_are_immutable_and_accept_list_input() -> None:
    settings = Settings(allowed_origins=["https://console.example.com"])

    assert isinstance(settings.allowed_origins, tuple)
    assert tuple(str(origin).rstrip("/") for origin in settings.allowed_origins) == (
        "https://console.example.com",
    )
    with pytest.raises(AttributeError):
        settings.allowed_origins.clear()


def test_cached_allowed_origins_cannot_be_polluted() -> None:
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        original_origins = settings.allowed_origins

        with pytest.raises(AttributeError):
            settings.allowed_origins.clear()

        assert config.get_settings().allowed_origins == original_origins
        app = create_app(testing=False)
        assert app.state.settings.allowed_origins == original_origins
    finally:
        config.get_settings.cache_clear()


def test_get_settings_is_cached() -> None:
    config.get_settings.cache_clear()
    try:
        assert config.get_settings() is config.get_settings()
    finally:
        config.get_settings.cache_clear()


def test_secret_values_are_not_exposed_in_repr_or_validation_errors() -> None:
    secret_value = "test-secret-value-that-must-not-leak"
    assert secret_value not in repr(Settings(proxy_secret=secret_value))

    with pytest.raises(ValidationError) as exc_info:
        _production_settings(proxy_secret=secret_value, session_secret="")
    assert secret_value not in str(exc_info.value)
    assert secret_value not in repr(exc_info.value)
    assert secret_value not in repr(exc_info.value.errors())
    assert secret_value not in exc_info.value.json()


def test_environment_secrets_are_not_exposed_in_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "test-environment-secret-that-must-not-leak"
    monkeypatch.setenv("PERFPILOT_APP_ENV", "production")
    monkeypatch.setenv("PERFPILOT_PROXY_SECRET", secret_value)
    monkeypatch.setenv("PERFPILOT_REDIS_URL", "redis://127.0.0.1:6379/0")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert secret_value not in repr(exc_info.value.errors())
    assert secret_value not in exc_info.value.json()


@pytest.mark.parametrize(
    "headers",
    [{}, {"x-request-id": ""}],
    ids=["missing", "empty"],
)
def test_server_generates_a_missing_request_id(headers: dict[str, str]) -> None:
    app = create_app(testing=True)

    @app.get("/v1/request-id")
    async def read_request_id(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    with TestClient(app) as client:
        response = client.get("/v1/request-id", headers=headers)

    request_id = response.headers["x-request-id"]
    assert request_id.strip()
    assert response.json() == {"request_id": request_id}


@pytest.mark.parametrize(
    "request_id",
    ["a", "a" * 128, ".leading", "_leading", ":leading", "-leading"],
    ids=[
        "one-character",
        "maximum-length",
        "leading-dot",
        "leading-underscore",
        "leading-colon",
        "leading-hyphen",
    ],
)
def test_valid_request_id_contract_is_preserved(request_id: str) -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get(
            "/v1/health",
            headers={"x-request-id": request_id},
        )

    assert response.headers["x-request-id"] == request_id


@pytest.mark.parametrize(
    "unsafe_request_id",
    ["a" * 129, "unsafe request id"],
    ids=["overlong", "unsafe-characters"],
)
def test_unsafe_request_id_is_replaced(unsafe_request_id: str) -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get(
            "/v1/health",
            headers={"x-request-id": unsafe_request_id},
        )

    generated_request_id = response.headers["x-request-id"]
    assert generated_request_id != unsafe_request_id
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", generated_request_id)


def test_api_error_uses_stable_error_shape() -> None:
    app = create_app(testing=True)

    @app.get("/v1/capacity")
    async def raise_api_error() -> None:
        raise errors.ApiError(
            "capacity_limited",
            "当前容量不足",
            429,
            True,
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/capacity", headers={"x-request-id": "req-api"})

    assert response.status_code == 429
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "capacity_limited",
            "message": "当前容量不足",
            "retryable": True,
            "request_id": "req-api",
        },
    }
    assert response.headers["x-request-id"] == "req-api"


def test_request_validation_error_uses_stable_error_shape() -> None:
    app = create_app(testing=True)

    @app.get("/v1/items/{item_id}")
    async def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    with TestClient(app) as client:
        response = client.get(
            "/v1/items/not-an-integer",
            headers={"x-request-id": "req-validation"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "request_validation_failed",
            "message": "请求参数校验失败",
            "retryable": False,
            "request_id": "req-validation",
        },
    }
    assert response.headers["x-request-id"] == "req-validation"


def test_http_exception_uses_stable_error_without_leaking_detail() -> None:
    app = create_app(testing=True)

    @app.get("/v1/unavailable")
    async def raise_http_exception() -> None:
        raise HTTPException(status_code=503, detail="internal upstream topology")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/v1/unavailable",
            headers={"x-request-id": "req-http"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "http_error",
            "message": "请求处理失败",
            "retryable": False,
            "request_id": "req-http",
        },
    }
    assert "internal upstream topology" not in response.text
    assert response.headers["x-request-id"] == "req-http"


def test_http_exception_preserves_authentication_headers() -> None:
    app = create_app(testing=True)

    @app.get("/v1/protected")
    async def raise_unauthorized() -> None:
        raise HTTPException(
            status_code=401,
            detail="internal authentication detail",
            headers={"WWW-Authenticate": 'Bearer realm="perfpilot"'},
        )

    with TestClient(app) as client:
        response = client.get(
            "/v1/protected",
            headers={"x-request-id": "req-auth"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="perfpilot"'
    assert response.json()["error"]["request_id"] == "req-auth"
    assert "internal authentication detail" not in response.text


def test_method_not_allowed_preserves_allow_header() -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.post(
            "/v1/health",
            headers={"x-request-id": "req-method"},
        )

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["error"]["request_id"] == "req-method"


def test_uncaught_exception_uses_stable_internal_error_shape() -> None:
    app = create_app(testing=True)
    secret_detail = "sensitive runtime failure detail"

    @app.get("/v1/crash")
    async def raise_runtime_error() -> None:
        raise RuntimeError(secret_detail)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/v1/crash",
            headers={"x-request-id": "req-crash"},
        )

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "internal_server_error",
            "message": "服务暂时不可用",
            "retryable": True,
            "request_id": "req-crash",
        },
    }
    assert secret_detail not in response.text
    assert response.headers["x-request-id"] == "req-crash"


def test_run_uses_the_uvicorn_application_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation: dict[str, object] = {}

    def fake_run(application: str, **kwargs: object) -> None:
        invocation["application"] = application
        invocation.update(kwargs)

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.run()

    assert invocation == {
        "application": "perfpilot_api.main:create_app",
        "factory": True,
    }
