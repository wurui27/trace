import re
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from socket import inet_aton
from typing import Any, Literal, TypeVar
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AnyHttpUrl,
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    TypeAdapter,
    ValidationError,
    ModelWrapValidatorHandler,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_CONTROL_DATABASE_URL = (
    "postgresql+psycopg://perfpilot:perfpilot@127.0.0.1:5432/perfpilot_control"
)
_DEVELOPMENT_REDIS_URL = "redis://127.0.0.1:6379/0"
_DEVELOPMENT_S3_ENDPOINT_URL = "http://127.0.0.1:9000"
_DEVELOPMENT_PROXY_SECRET = "development-only-proxy-secret"
_DEVELOPMENT_SESSION_SECRET = "development-only-session-secret"
_DEVELOPMENT_JWS_SIGNING_KEY_REFERENCE = "development-only-jws-signing-key-reference"
_DEVELOPMENT_AGENT_REGISTRATION_SECRET_REFERENCE = (
    "development-only-agent-registration-secret-reference"
)
_DEVELOPMENT_ALLOWED_ORIGIN = "http://127.0.0.1:3000"
_REDACTED_VALIDATION_INPUT = "[redacted]"
_INVALID_PRODUCTION_SERVICE_URL = "production service URL configuration is invalid"
_INVALID_PRODUCTION_ARTIFACT_RUNTIME = "production artifact runtime configuration is invalid"
_DEVELOPMENT_TENANT_CLUSTER_HOST = "127.0.0.1"
_DEVELOPMENT_SECRET_KEYRING_CONFIG = Path(".perfpilot/keyring.json")
_DEVELOPMENT_SECRET_STORE_ROOT = Path(".perfpilot/secrets")
_DEVELOPMENT_APKANALYZER_BINARY = Path("/opt/android-sdk/cmdline-tools/latest/bin/apkanalyzer")
_DEVELOPMENT_S3_REGION = "us-east-1"
_RUNTIME_HOST_PATTERN = re.compile(r"[^\s/?#@,\\]{1,253}\Z")
_S3_REGION_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_PRODUCTION_RUNTIME_FIELDS = frozenset(
    {
        "s3_region",
        "tenant_cluster_host",
        "tenant_cluster_port",
        "tenant_cluster_sslmode",
        "secret_keyring_config",
        "secret_store_root",
    }
)

_Dsn = TypeVar("_Dsn", PostgresDsn, RedisDsn)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False

    normalized_host = host.strip("[]").rstrip(".").casefold()
    if normalized_host == "localhost":
        return True
    try:
        address = ip_address(normalized_host)
    except ValueError:
        try:
            address = ip_address(inet_aton(normalized_host))
        except OSError:
            return False
    return address.is_loopback


def _production_validation_error(message: str) -> ValidationError:
    error = InitErrorDetails(
        type=PydanticCustomError("production_configuration", message),
        loc=(),
        input=_REDACTED_VALIDATION_INPUT,
    )
    return ValidationError.from_exception_data(
        "Settings",
        [error],
        hide_input=True,
    )


def _parse_production_service_url(
    value: SecretStr,
    allowed_schemes: frozenset[str],
    dsn_type: type[_Dsn],
) -> _Dsn:
    try:
        raw_url = value.get_secret_value()
        parsed_url = urlsplit(raw_url)
        hostname = parsed_url.hostname
        validated_url = TypeAdapter(dsn_type).validate_python(raw_url)
    except (ValueError, ValidationError):
        raise _production_validation_error(_INVALID_PRODUCTION_SERVICE_URL) from None

    if parsed_url.scheme.casefold() not in allowed_schemes or hostname is None:
        raise _production_validation_error(_INVALID_PRODUCTION_SERVICE_URL)
    return validated_url


def _has_unambiguous_verify_full(parsed_url: Any) -> bool:
    try:
        query_items = parse_qsl(
            parsed_url.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    return query_items == [("sslmode", "verify-full")]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PERFPILOT_",
        env_file=".env",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    control_database_url: SecretStr = SecretStr(_DEVELOPMENT_CONTROL_DATABASE_URL)
    redis_url: SecretStr = SecretStr(_DEVELOPMENT_REDIS_URL)
    s3_endpoint_url: AnyHttpUrl = AnyHttpUrl(_DEVELOPMENT_S3_ENDPOINT_URL)
    s3_region: str = _DEVELOPMENT_S3_REGION
    tenant_cluster_host: str = _DEVELOPMENT_TENANT_CLUSTER_HOST
    tenant_cluster_port: int = Field(default=5432, ge=1, le=65535)
    tenant_cluster_sslmode: Literal[
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    ] = "disable"
    secret_keyring_config: Path = _DEVELOPMENT_SECRET_KEYRING_CONFIG
    secret_store_root: Path = _DEVELOPMENT_SECRET_STORE_ROOT
    apkanalyzer_binary: Path = _DEVELOPMENT_APKANALYZER_BINARY
    proxy_secret: SecretStr = SecretStr(_DEVELOPMENT_PROXY_SECRET)
    session_secret: SecretStr = SecretStr(_DEVELOPMENT_SESSION_SECRET)
    jws_signing_key_reference: SecretStr = SecretStr(_DEVELOPMENT_JWS_SIGNING_KEY_REFERENCE)
    agent_registration_secret_reference: SecretStr = SecretStr(
        _DEVELOPMENT_AGENT_REGISTRATION_SECRET_REFERENCE
    )
    allowed_origins: tuple[AnyHttpUrl, ...] = Field(
        default_factory=lambda: (AnyHttpUrl(_DEVELOPMENT_ALLOWED_ORIGIN),)
    )

    @model_validator(mode="wrap")
    @classmethod
    def redact_production_field_errors(
        cls,
        values: Any,
        handler: ModelWrapValidatorHandler["Settings"],
    ) -> "Settings":
        try:
            return handler(values)
        except ValidationError as exc:
            app_env = values.get("app_env") if isinstance(values, dict) else None
            leaks_input = any(
                error.get("input") != _REDACTED_VALIDATION_INPUT for error in exc.errors()
            )
            if app_env == "production" and leaks_input:
                raise _production_validation_error(_INVALID_PRODUCTION_SERVICE_URL) from None
            raise

    @model_validator(mode="after")
    def validate_production_configuration(self) -> "Settings":
        if self.app_env != "production":
            return self

        required_secrets = (
            self.proxy_secret.get_secret_value(),
            self.session_secret.get_secret_value(),
            self.jws_signing_key_reference.get_secret_value(),
            self.agent_registration_secret_reference.get_secret_value(),
        )
        uses_development_default = (
            self.control_database_url.get_secret_value() == _DEVELOPMENT_CONTROL_DATABASE_URL
            or self.redis_url.get_secret_value() == _DEVELOPMENT_REDIS_URL
            or str(self.s3_endpoint_url).rstrip("/") == _DEVELOPMENT_S3_ENDPOINT_URL
            or self.proxy_secret.get_secret_value() == _DEVELOPMENT_PROXY_SECRET
            or self.session_secret.get_secret_value() == _DEVELOPMENT_SESSION_SECRET
            or self.jws_signing_key_reference.get_secret_value()
            == _DEVELOPMENT_JWS_SIGNING_KEY_REFERENCE
            or self.agent_registration_secret_reference.get_secret_value()
            == _DEVELOPMENT_AGENT_REGISTRATION_SECRET_REFERENCE
            or (
                len(self.allowed_origins) == 1
                and str(self.allowed_origins[0]).rstrip("/") == _DEVELOPMENT_ALLOWED_ORIGIN
            )
        )
        if uses_development_default or any(not value.strip() for value in required_secrets):
            raise _production_validation_error("production secret configuration is required")

        if not _PRODUCTION_RUNTIME_FIELDS.issubset(self.model_fields_set):
            raise _production_validation_error(_INVALID_PRODUCTION_ARTIFACT_RUNTIME)
        if (
            _RUNTIME_HOST_PATTERN.fullmatch(self.tenant_cluster_host) is None
            or _is_loopback_host(self.tenant_cluster_host)
            or _S3_REGION_PATTERN.fullmatch(self.s3_region) is None
            or not self.secret_keyring_config.is_absolute()
            or self.secret_keyring_config == Path("/")
            or not self.secret_store_root.is_absolute()
            or self.secret_store_root == Path("/")
        ):
            raise _production_validation_error(_INVALID_PRODUCTION_ARTIFACT_RUNTIME)
        if self.tenant_cluster_sslmode != "verify-full":
            raise _production_validation_error("production tenant cluster requires verify-full")

        database_url = _parse_production_service_url(
            self.control_database_url,
            frozenset({"postgresql+psycopg"}),
            PostgresDsn,
        )
        _parse_production_service_url(
            self.redis_url,
            frozenset({"rediss"}),
            RedisDsn,
        )
        parsed_database_url = urlsplit(self.control_database_url.get_secret_value())
        if (
            not _has_unambiguous_verify_full(parsed_database_url)
            or self.s3_endpoint_url.scheme.casefold() != "https"
        ):
            raise _production_validation_error(_INVALID_PRODUCTION_SERVICE_URL)
        database_hosts = (host["host"] for host in database_url.hosts())
        if any(_is_loopback_host(host) for host in database_hosts) or _is_loopback_host(
            self.s3_endpoint_url.host
        ):
            raise _production_validation_error(
                "production endpoints must not use loopback or localhost hosts"
            )

        if not self.allowed_origins:
            raise _production_validation_error("production requires at least one allowed origin")
        if any(origin.scheme.casefold() != "https" for origin in self.allowed_origins):
            raise _production_validation_error("production allowed origins must use HTTPS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
