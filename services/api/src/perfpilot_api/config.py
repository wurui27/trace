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
_DEVELOPMENT_SMARTPERFETTO_BASE_URL = "http://127.0.0.1:3001"
_DEVELOPMENT_SMARTPERFETTO_CREDENTIAL_REFERENCE = (
    "development-only-smartperfetto-credential-reference"
)
_DEVELOPMENT_AI_BASE_URL = "http://127.0.0.1:4010/v1/"
_DEVELOPMENT_AI_CREDENTIAL_REFERENCE = "development-only-ai-credential-reference"
_DEVELOPMENT_ALLOWED_ORIGIN = "http://127.0.0.1:3000"
_REDACTED_VALIDATION_INPUT = "[redacted]"
_INVALID_PRODUCTION_SERVICE_URL = "production service URL configuration is invalid"
_INVALID_PRODUCTION_ARTIFACT_RUNTIME = "production artifact runtime configuration is invalid"
_INVALID_PRODUCTION_ANDROID_MEMORY = "production Android memory configuration is invalid"
_INVALID_PRODUCTION_AI = "production AI configuration is invalid"
_DEVELOPMENT_TENANT_CLUSTER_HOST = "127.0.0.1"
_DEVELOPMENT_SECRET_KEYRING_CONFIG = Path(".perfpilot/keyring.json")
_DEVELOPMENT_SECRET_STORE_ROOT = Path(".perfpilot/secrets")
_DEVELOPMENT_APKANALYZER_BINARY = Path("/opt/android-sdk/cmdline-tools/latest/bin/apkanalyzer")
_DEVELOPMENT_S3_REGION = "us-east-1"
_RUNTIME_HOST_PATTERN = re.compile(r"[^\s/?#@,\\]{1,253}\Z")
_S3_REGION_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_ANDROID_MEMORY_FIRST_REPOSITORY_COMPONENT = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?\Z"
)
_ANDROID_MEMORY_REPOSITORY_COMPONENT = re.compile(
    r"[a-z0-9]+(?:(?:[._-]|__)[a-z0-9]+)*\Z"
)
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


def _is_unsafe_ai_host(host: str | None) -> bool:
    if host is None:
        return True
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
    return not address.is_global or address.is_multicast


def _is_valid_ai_base_url(value: SecretStr, *, production: bool) -> bool:
    try:
        parsed_url = urlsplit(value.get_secret_value())
    except ValueError:
        return False
    if (
        parsed_url.scheme.casefold() not in {"http", "https"}
        or parsed_url.hostname is None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or not parsed_url.path.endswith("/v1/")
        or "\\" in parsed_url.path
    ):
        return False
    return not production or (
        parsed_url.scheme.casefold() == "https"
        and not _is_unsafe_ai_host(parsed_url.hostname)
    )


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


def _safe_absolute_runtime_path(value: Path) -> bool:
    rendered = str(value)
    return (
        value.is_absolute()
        and value != Path("/")
        and "," not in rendered
        and "\n" not in rendered
        and "\r" not in rendered
        and "\x00" not in rendered
    )


def is_valid_android_memory_image_reference(value: object) -> bool:
    if not isinstance(value, str) or value.startswith("-"):
        return False
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        return False
    if any(character in value for character in (",", "=")):
        return False
    repository, separator, digest = value.partition("@sha256:")
    if separator != "@sha256:" or "@" in repository or len(digest) != 64:
        return False
    if not repository or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        return False
    components = repository.split("/")
    if any(not component for component in components):
        return False
    if _ANDROID_MEMORY_FIRST_REPOSITORY_COMPONENT.fullmatch(components[0]) is None:
        return False
    if any(
        _ANDROID_MEMORY_REPOSITORY_COMPONENT.fullmatch(component) is None
        for component in components[1:]
    ):
        return False
    if ":" in components[0]:
        port = int(components[0].rsplit(":", 1)[1])
        if port > 65535:
            return False
    return True


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
    smartperfetto_enabled: bool = False
    smartperfetto_base_url: SecretStr = SecretStr(_DEVELOPMENT_SMARTPERFETTO_BASE_URL)
    smartperfetto_credential_reference: SecretStr = SecretStr(
        _DEVELOPMENT_SMARTPERFETTO_CREDENTIAL_REFERENCE
    )
    smartperfetto_connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    smartperfetto_read_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    smartperfetto_write_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    smartperfetto_pool_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    smartperfetto_max_trace_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    smartperfetto_max_json_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    smartperfetto_max_sse_event_bytes: int = Field(default=256 * 1024, gt=0)
    smartperfetto_sse_batch_events: int = Field(default=64, gt=0, le=1024)
    smartperfetto_sse_batch_seconds: float = Field(
        default=2.0,
        gt=0,
        le=60,
        allow_inf_nan=False,
    )
    ai_enabled: bool = False
    ai_base_url: SecretStr = SecretStr(_DEVELOPMENT_AI_BASE_URL)
    ai_provider_name: str = Field(
        default="development-fake",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    ai_model: str = Field(default="fake-json-model", min_length=1, max_length=128)
    ai_credential_reference: SecretStr = SecretStr(_DEVELOPMENT_AI_CREDENTIAL_REFERENCE)
    ai_connect_timeout_seconds: float = Field(
        default=5.0, gt=0, le=120, allow_inf_nan=False
    )
    ai_read_timeout_seconds: float = Field(
        default=60.0, gt=0, le=120, allow_inf_nan=False
    )
    ai_write_timeout_seconds: float = Field(
        default=30.0, gt=0, le=120, allow_inf_nan=False
    )
    ai_pool_timeout_seconds: float = Field(
        default=5.0, gt=0, le=120, allow_inf_nan=False
    )
    ai_max_projection_bytes: int = Field(
        default=256 * 1024, ge=1024, le=256 * 1024, strict=True
    )
    ai_max_response_bytes: int = Field(
        default=128 * 1024, ge=1024, le=128 * 1024, strict=True
    )
    android_memory_enabled: bool = False
    android_memory_backend: Literal["local", "oci"] = "local"
    android_memory_image_reference: str | None = None
    android_memory_checkout_root: Path = Path("/Users/ray/Android-App-Memory-Analysis")
    android_memory_python_binary: Path = Path("/usr/local/bin/python3")
    android_memory_run_root: Path = Path(".perfpilot/android-memory-runs")
    android_memory_container_runtime: Path = Path("/usr/bin/docker")
    android_memory_max_files: int = Field(default=2048, ge=1, le=2048, strict=True)
    android_memory_max_file_bytes: int = Field(default=5 * 1024**3, gt=0, strict=True)
    android_memory_max_total_bytes: int = Field(default=8 * 1024**3, gt=0, strict=True)
    android_memory_max_output_bytes: int = Field(default=32 * 1024**2, gt=0, strict=True)
    android_memory_timeout_seconds: int = Field(default=900, ge=1, le=3600, strict=True)
    android_memory_cpu_limit: float = Field(
        default=4.0,
        gt=0,
        le=64,
        allow_inf_nan=False,
        strict=True,
    )
    android_memory_memory_bytes: int = Field(default=8 * 1024**3, gt=0, strict=True)
    android_memory_pids_limit: int = Field(default=128, ge=16, le=4096, strict=True)
    android_memory_tmpfs_bytes: int = Field(default=1024**3, gt=0, strict=True)
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
        android_memory_invalid = self.android_memory_max_total_bytes < (
            self.android_memory_max_file_bytes
        )
        if self.android_memory_enabled:
            android_memory_invalid = android_memory_invalid or not all(
                _safe_absolute_runtime_path(path)
                for path in (
                    self.android_memory_checkout_root,
                    self.android_memory_python_binary,
                    self.android_memory_run_root,
                )
            )
            if self.android_memory_backend == "oci":
                android_memory_invalid = (
                    android_memory_invalid
                    or not _safe_absolute_runtime_path(self.android_memory_container_runtime)
                    or not is_valid_android_memory_image_reference(
                        self.android_memory_image_reference
                    )
                )
        if android_memory_invalid:
            if self.app_env == "production":
                raise _production_validation_error(_INVALID_PRODUCTION_ANDROID_MEMORY)
            raise ValueError("Android memory configuration is invalid")

        if self.smartperfetto_enabled:
            raw_endpoint = self.smartperfetto_base_url.get_secret_value()
            raw_reference = self.smartperfetto_credential_reference.get_secret_value()
            try:
                parsed_smartperfetto_url = urlsplit(raw_endpoint)
            except ValueError:
                raise _production_validation_error(
                    "SmartPerfetto service configuration is invalid"
                ) from None
            if (
                parsed_smartperfetto_url.scheme.casefold() not in {"http", "https"}
                or parsed_smartperfetto_url.hostname is None
                or parsed_smartperfetto_url.username is not None
                or parsed_smartperfetto_url.password is not None
                or parsed_smartperfetto_url.query
                or parsed_smartperfetto_url.fragment
                or parsed_smartperfetto_url.path not in {"", "/"}
                or not raw_reference.strip()
            ):
                raise _production_validation_error(
                    "SmartPerfetto service configuration is invalid"
                )

        if self.app_env != "production":
            return self

        if self.ai_enabled and (
            not _is_valid_ai_base_url(self.ai_base_url, production=True)
            or not self.ai_credential_reference.get_secret_value().strip()
            or self.ai_credential_reference.get_secret_value()
            == _DEVELOPMENT_AI_CREDENTIAL_REFERENCE
        ):
            raise _production_validation_error(_INVALID_PRODUCTION_AI)

        if self.android_memory_enabled and self.android_memory_backend != "oci":
            raise _production_validation_error(_INVALID_PRODUCTION_ANDROID_MEMORY)

        if self.smartperfetto_enabled and (
            parsed_smartperfetto_url.scheme.casefold() != "https"
            or _is_loopback_host(parsed_smartperfetto_url.hostname)
            or self.smartperfetto_credential_reference.get_secret_value()
            == _DEVELOPMENT_SMARTPERFETTO_CREDENTIAL_REFERENCE
        ):
            raise _production_validation_error(
                "production SmartPerfetto configuration is invalid"
            )

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
