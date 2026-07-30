import base64
import hashlib
import re
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from starlette.responses import Response

from perfpilot_api.config import Settings
import perfpilot_api.security.csrf as csrf_security
import perfpilot_api.security.proxy_signature as proxy_security
import perfpilot_api.security.sessions as session_security
from perfpilot_api.security.passwords import (
    hash_password,
    normalize_username,
    verify_password,
)

_URLSAFE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


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
        "proxy_secret": "test-production-proxy-secret",
        "session_secret": "test-production-session-secret",
        "jws_signing_key_reference": "kms://keys/perfpilot-signing",
        "agent_registration_secret_reference": "vault://secrets/agent-registration",
        "allowed_origins": ["https://console.example.com"],
    }
    values.update(overrides)
    return Settings(**values)


def test_smartperfetto_settings_have_finite_positive_bounds() -> None:
    settings = Settings(
        smartperfetto_enabled=True,
        smartperfetto_base_url="http://127.0.0.1:3001",
        smartperfetto_credential_reference="development-smartperfetto-secret-ref",
    )

    assert settings.smartperfetto_connect_timeout_seconds > 0
    assert settings.smartperfetto_read_timeout_seconds > 0
    assert settings.smartperfetto_write_timeout_seconds > 0
    assert settings.smartperfetto_pool_timeout_seconds > 0
    assert settings.smartperfetto_max_trace_bytes > 0
    assert settings.smartperfetto_max_json_bytes > 0
    assert settings.smartperfetto_max_sse_event_bytes > 0
    assert settings.smartperfetto_sse_batch_events > 0
    assert settings.smartperfetto_sse_batch_seconds > 0


@pytest.mark.parametrize(
    "field_name",
    [
        "smartperfetto_connect_timeout_seconds",
        "smartperfetto_read_timeout_seconds",
        "smartperfetto_write_timeout_seconds",
        "smartperfetto_pool_timeout_seconds",
        "smartperfetto_max_trace_bytes",
        "smartperfetto_max_json_bytes",
        "smartperfetto_max_sse_event_bytes",
        "smartperfetto_sse_batch_events",
        "smartperfetto_sse_batch_seconds",
    ],
)
def test_smartperfetto_settings_reject_nonpositive_bounds(field_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field_name: 0})


def test_production_accepts_explicit_https_smartperfetto_service() -> None:
    settings = _production_settings(
        smartperfetto_enabled=True,
        smartperfetto_base_url="https://smartperfetto.example.com",
        smartperfetto_credential_reference="vault://services/smartperfetto",
    )

    assert settings.smartperfetto_enabled is True
    assert settings.smartperfetto_base_url.get_secret_value() == (
        "https://smartperfetto.example.com"
    )
    assert isinstance(settings.smartperfetto_credential_reference, SecretStr)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://smartperfetto.example.com",
        "https://user:password@smartperfetto.example.com",
        "https://smartperfetto.example.com?token=secret-marker",
        "https://smartperfetto.example.com#secret-marker",
        "https://127.0.0.1",
        "https://localhost",
    ],
)
def test_production_rejects_unsafe_smartperfetto_endpoints_without_leaking_them(
    base_url: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _production_settings(
            smartperfetto_enabled=True,
            smartperfetto_base_url=base_url,
            smartperfetto_credential_reference="vault://services/smartperfetto",
        )

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert "secret-marker" not in rendered
    assert "password" not in rendered


def test_production_requires_nonempty_smartperfetto_secret_reference_when_enabled() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _production_settings(
            smartperfetto_enabled=True,
            smartperfetto_base_url="https://smartperfetto.example.com",
            smartperfetto_credential_reference="   ",
        )

    assert "input_value" not in str(exc_info.value)


def test_smartperfetto_secret_reference_is_redacted_from_settings_repr() -> None:
    marker = "smartperfetto-secret-marker"
    settings = Settings(smartperfetto_credential_reference=marker)

    assert marker not in repr(settings)


def test_android_memory_settings_have_bounded_defaults() -> None:
    settings = Settings()

    assert settings.android_memory_enabled is False
    assert settings.android_memory_backend == "local"
    assert settings.android_memory_checkout_root == Path("/Users/ray/Android-App-Memory-Analysis")
    assert settings.android_memory_python_binary == Path("/usr/local/bin/python3")
    assert settings.android_memory_run_root == Path(".perfpilot/android-memory-runs")
    assert settings.android_memory_container_runtime == Path("/usr/bin/docker")
    assert settings.android_memory_max_files == 2048
    assert settings.android_memory_max_file_bytes == 5 * 1024**3
    assert settings.android_memory_max_total_bytes == 8 * 1024**3
    assert settings.android_memory_max_output_bytes == 32 * 1024**2
    assert settings.android_memory_timeout_seconds == 900
    assert settings.android_memory_cpu_limit == 4.0
    assert settings.android_memory_memory_bytes == 8 * 1024**3
    assert settings.android_memory_pids_limit == 128
    assert settings.android_memory_tmpfs_bytes == 1024**3


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("android_memory_max_files", 0),
        ("android_memory_max_files", 2049),
        ("android_memory_max_files", True),
        ("android_memory_max_file_bytes", 0),
        ("android_memory_max_file_bytes", True),
        ("android_memory_max_total_bytes", 0),
        ("android_memory_max_output_bytes", 0),
        ("android_memory_timeout_seconds", 0),
        ("android_memory_timeout_seconds", 3601),
        ("android_memory_cpu_limit", 0),
        ("android_memory_cpu_limit", 65),
        ("android_memory_cpu_limit", float("inf")),
        ("android_memory_cpu_limit", float("nan")),
        ("android_memory_cpu_limit", True),
        ("android_memory_memory_bytes", 0),
        ("android_memory_pids_limit", 15),
        ("android_memory_pids_limit", 4097),
        ("android_memory_pids_limit", True),
        ("android_memory_tmpfs_bytes", 0),
    ],
)
def test_android_memory_settings_reject_invalid_limits(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field_name: value})


def test_android_memory_total_bytes_must_cover_one_file() -> None:
    with pytest.raises(ValidationError):
        Settings(
            android_memory_max_file_bytes=1024,
            android_memory_max_total_bytes=1023,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("android_memory_checkout_root", "relative/checkout"),
        ("android_memory_checkout_root", "/"),
        ("android_memory_python_binary", "python3"),
        ("android_memory_python_binary", "/"),
        ("android_memory_run_root", "relative/runs"),
        ("android_memory_run_root", "/"),
    ],
)
def test_enabled_android_memory_requires_absolute_nonroot_paths(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            android_memory_enabled=True,
            **{field_name: value},
        )


def test_android_memory_environment_aliases_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFPILOT_ANDROID_MEMORY_ENABLED", "true")
    monkeypatch.setenv("PERFPILOT_ANDROID_MEMORY_BACKEND", "oci")
    monkeypatch.setenv(
        "PERFPILOT_ANDROID_MEMORY_IMAGE_REFERENCE",
        "registry.example/android-memory@sha256:" + "a" * 64,
    )
    monkeypatch.setenv("PERFPILOT_ANDROID_MEMORY_RUN_ROOT", "/var/lib/perfpilot/memory")

    settings = Settings(_env_file=None)

    assert settings.android_memory_enabled is True
    assert settings.android_memory_backend == "oci"
    assert settings.android_memory_image_reference is not None


def test_production_rejects_local_android_memory_with_one_redacted_error() -> None:
    marker = "/private/android-memory-path-marker"
    with pytest.raises(ValidationError) as caught:
        _production_settings(
            android_memory_enabled=True,
            android_memory_backend="local",
            android_memory_checkout_root=marker,
            android_memory_run_root="/var/lib/perfpilot/android-memory",
        )

    rendered = f"{caught.value!s} {caught.value!r}"
    assert "production Android memory configuration is invalid" in rendered
    assert marker not in rendered


def test_production_accepts_digest_pinned_oci_android_memory() -> None:
    settings = _production_settings(
        android_memory_enabled=True,
        android_memory_backend="oci",
        android_memory_image_reference=("registry.example/android-memory@sha256:" + "a" * 64),
        android_memory_run_root="/var/lib/perfpilot/android-memory",
        android_memory_checkout_root="/opt/android-memory",
        android_memory_python_binary="/usr/local/bin/python3",
        android_memory_container_runtime="/usr/bin/docker",
    )

    assert settings.android_memory_backend == "oci"


@pytest.mark.parametrize(
    "image_reference",
    [
        None,
        "registry.example/android-memory:latest",
        "registry/image@sha256:" + "A" * 64,
        "--env=PRIVATE@sha256:" + "a" * 64,
        "registry.example/android=memory@sha256:" + "a" * 64,
    ],
)
def test_enabled_oci_android_memory_rejects_unpinned_images(
    image_reference: str | None,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            android_memory_enabled=True,
            android_memory_backend="oci",
            android_memory_image_reference=image_reference,
            android_memory_run_root="/tmp/perfpilot-memory",
        )


def test_invalid_android_memory_image_error_does_not_leak_reference() -> None:
    marker = "--env=PRIVATE-secret-marker@sha256:" + "a" * 64

    with pytest.raises(ValidationError) as caught:
        _production_settings(
            android_memory_enabled=True,
            android_memory_backend="oci",
            android_memory_image_reference=marker,
            android_memory_run_root="/var/lib/perfpilot/android-memory",
        )

    assert marker not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.parametrize(
    "image_reference",
    [
        "registry.example.com/team/android-memory@sha256:" + "a" * 64,
        "localhost:5000/team/android-memory@sha256:" + "a" * 64,
    ],
)
def test_enabled_oci_android_memory_accepts_safe_repository_references(
    image_reference: str,
) -> None:
    settings = Settings(
        android_memory_enabled=True,
        android_memory_backend="oci",
        android_memory_image_reference=image_reference,
        android_memory_run_root="/tmp/perfpilot-memory",
    )

    assert settings.android_memory_image_reference == image_reference


def test_android_memory_dockerfile_is_nonroot_and_requires_external_pinned_base() -> None:
    root = Path(__file__).resolve().parents[4]
    dockerfile = (root / "infra/engines/android-memory/Dockerfile").read_text()

    assert dockerfile.startswith("ARG PYTHON_BASE_IMAGE\nFROM ${PYTHON_BASE_IMAGE}\n")
    assert "ARG PYTHON_BASE_IMAGE=" not in dockerfile
    assert "65532" in dockerfile
    assert "COPY --chown=65532:65532 . /opt/android-memory" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["python3", "tools/ai_context.py"]' in dockerfile
    assert "sudo" not in dockerfile.casefold()
    assert "SECRET" not in dockerfile


def test_username_normalization_uses_nfkc_strip_and_casefold() -> None:
    assert normalize_username("  ＲＡＹ_ＷＵ  ") == "ray_wu"
    assert normalize_username("  Straße  ") == "strasse"


def test_argon2_password_hashes_are_salted_and_verify() -> None:
    password = "correct horse battery staple"

    first_hash = hash_password(password)
    second_hash = hash_password(password)

    assert first_hash.startswith("$argon2id$")
    assert first_hash != second_hash
    assert password not in first_hash
    assert verify_password(first_hash, password) is True
    assert verify_password(first_hash, "wrong password") is False


def test_password_verification_rejects_malformed_hash_without_leaking_secret() -> None:
    secret = "password-value-that-must-not-leak"

    assert verify_password("not-an-argon2-hash", secret) is False
    assert secret not in repr(verify_password)


@pytest.mark.parametrize(
    ("generate", "digest", "verify"),
    [
        (
            session_security.generate_session_token,
            session_security.digest_session_token,
            session_security.verify_session_token,
        ),
        (
            csrf_security.generate_csrf_token,
            csrf_security.digest_csrf_token,
            csrf_security.verify_csrf_token,
        ),
    ],
    ids=["session", "csrf"],
)
def test_session_and_csrf_tokens_are_32_random_urlsafe_bytes(
    generate: object,
    digest: object,
    verify: object,
) -> None:
    first = generate()
    second = generate()

    assert first != second
    assert _URLSAFE_TOKEN_PATTERN.fullmatch(first)
    assert len(base64.urlsafe_b64decode(first + "=")) == 32
    expected_digest = hashlib.sha256(first.encode("ascii")).hexdigest()
    assert digest(first) == expected_digest
    assert verify(first, expected_digest) is True
    assert verify(second, expected_digest) is False


def test_session_cookie_is_secure_host_only_and_can_be_cleared() -> None:
    response = Response()
    session_security.set_session_cookie(response, "session-token", max_age=3600)

    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    session_cookie = cookie[session_security.COOKIE_NAME]
    assert session_cookie.value == "session-token"
    assert session_cookie["max-age"] == "3600"
    assert session_cookie["path"] == "/"
    assert session_cookie["secure"] is True
    assert session_cookie["httponly"] is True
    assert session_cookie["samesite"].casefold() == "lax"
    assert session_cookie["domain"] == ""

    clear_response = Response()
    session_security.clear_session_cookie(clear_response)
    cleared = SimpleCookie()
    cleared.load(clear_response.headers["set-cookie"])
    cleared_cookie = cleared[session_security.COOKIE_NAME]
    assert cleared_cookie.value == ""
    assert cleared_cookie["max-age"] == "0"
    assert cleared_cookie["expires"]
    assert cleared_cookie["path"] == "/"
    assert cleared_cookie["secure"] is True
    assert cleared_cookie["httponly"] is True
    assert cleared_cookie["samesite"].casefold() == "lax"
    assert cleared_cookie["domain"] == ""


def test_origin_allowlist_uses_strict_canonical_origin_matching() -> None:
    allowed_origins = ["https://app.example/", "http://127.0.0.1:3000"]

    assert csrf_security.is_allowed_origin("https://app.example", allowed_origins)
    assert csrf_security.is_allowed_origin("https://APP.EXAMPLE:443", allowed_origins)
    assert csrf_security.is_allowed_origin("http://127.0.0.1:3000", allowed_origins)

    for rejected_origin in (
        None,
        "null",
        "https://app.example.evil",
        "https://app.example/path",
        "https://user@app.example",
        "http://app.example",
        "https://app.example, https://evil.example",
    ):
        assert not csrf_security.is_allowed_origin(rejected_origin, allowed_origins)


def test_origin_error_does_not_echo_an_untrusted_origin() -> None:
    untrusted_origin = "https://url-secret-marker.example"

    with pytest.raises(csrf_security.OriginNotAllowedError) as exc_info:
        csrf_security.require_allowed_origin(
            untrusted_origin,
            ["https://app.example"],
        )

    assert "url-secret-marker" not in str(exc_info.value)
    assert "url-secret-marker" not in repr(exc_info.value)


def test_proxy_signature_matches_the_exact_raw_request_contract() -> None:
    secret = b"proxy-secret-value"
    timestamp = 1_700_000_000
    request_id = "req.proxy:1"
    raw_path = b"/v1/teams/team%2Fid/analyses"
    raw_query = b"b=two%20words&a=1"
    body = b'{"mode":"device"}'
    path_and_query = raw_path + b"?" + raw_query
    body_sha256 = hashlib.sha256(body).hexdigest().encode("ascii")
    canonical = b"\n".join(
        (
            str(timestamp).encode("ascii"),
            request_id.encode("ascii"),
            b"POST",
            path_and_query,
            body_sha256,
        )
    )
    expected = base64.urlsafe_b64encode(
        __import__("hmac").new(secret, canonical, hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")

    signature = proxy_security.sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id=request_id,
        method="post",
        raw_path=raw_path,
        raw_query=raw_query,
        body=body,
    )

    assert signature == expected
    assert "=" not in signature
    assert signature != proxy_security.sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id=request_id,
        method="post",
        raw_path=raw_path,
        raw_query=b"a=1&b=two%20words",
        body=body,
    )


def test_proxy_client_identity_uses_separate_domain_bound_attestation() -> None:
    secret = b"proxy-secret-value"
    timestamp = 1_700_000_000
    request_id = "req-client-identity"
    canonical_ip = "2001:db8::1"
    expected_client_id = base64.urlsafe_b64encode(
        __import__("hmac").new(
            secret,
            f"perfpilot-client-id-v1\n{canonical_ip}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")
    expected_attestation = base64.urlsafe_b64encode(
        __import__("hmac").new(
            secret,
            (
                "perfpilot-client-attestation-v1\n"
                f"{timestamp}\n{request_id}\n{expected_client_id}"
            ).encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")

    identity = proxy_security.sign_proxy_client_identity(
        secret,
        client_address="2001:0DB8:0:0:0:0:0:1",
        timestamp=timestamp,
        request_id=request_id,
    )

    assert identity == f"{expected_client_id}.{expected_attestation}"
    assert proxy_security.verify_proxy_client_identity(
        secret,
        identity_header=identity,
        timestamp_header=str(timestamp),
        request_id_header=request_id,
    ) == expected_client_id
    assert proxy_security.sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/me",
    ) == proxy_security.sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/me",
    )


@pytest.mark.parametrize(
    ("timestamp_header", "request_id_header", "mutate_identity"),
    [
        ("1700000001", "req-client-identity", False),
        ("1700000000", "req-client-identity-other", False),
        ("1700000000", "req-client-identity", True),
        (None, "req-client-identity", False),
    ],
)
def test_proxy_client_identity_rejects_tampering_or_rebinding(
    timestamp_header: str | None,
    request_id_header: str,
    mutate_identity: bool,
) -> None:
    secret = b"proxy-secret-value"
    identity = proxy_security.sign_proxy_client_identity(
        secret,
        client_address="198.51.100.10",
        timestamp=1_700_000_000,
        request_id="req-client-identity",
    )
    if mutate_identity:
        identity = f"{'a' if identity[0] != 'a' else 'b'}{identity[1:]}"

    with pytest.raises(proxy_security.InvalidProxyClientIdentityError):
        proxy_security.verify_proxy_client_identity(
            secret,
            identity_header=identity,
            timestamp_header=timestamp_header,
            request_id_header=request_id_header,
        )


@pytest.mark.parametrize(
    "client_address",
    ["198.51.100.1:443", " 198.51.100.1", "fe80::1%eth0", "not-an-ip"],
)
def test_proxy_client_identity_signer_requires_canonicalizable_ip(
    client_address: str,
) -> None:
    with pytest.raises(ValueError, match="invalid client address"):
        proxy_security.sign_proxy_client_identity(
            b"proxy-secret-value",
            client_address=client_address,
            timestamp=1_700_000_000,
            request_id="req-client-identity",
        )


@pytest.mark.asyncio
async def test_proxy_verification_accepts_fresh_signature_once() -> None:
    timestamp = 1_700_000_000
    secret = b"proxy-secret-value"
    replay_store = proxy_security.InMemoryReplayStore(clock=lambda: float(timestamp))
    signature = proxy_security.sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id="req-replay",
        method="GET",
        raw_path=b"/v1/me",
        raw_query=b"",
        body=b"",
    )

    result = await proxy_security.verify_proxy_request(
        secret,
        timestamp_header=str(timestamp),
        request_id_header="req-replay",
        signature_header=signature,
        method="GET",
        raw_path=b"/v1/me",
        raw_query=b"",
        body=b"",
        replay_store=replay_store,
        clock=lambda: float(timestamp),
    )

    assert result is None
    with pytest.raises(proxy_security.ProxyReplayError):
        await proxy_security.verify_proxy_request(
            secret,
            timestamp_header=str(timestamp),
            request_id_header="req-replay",
            signature_header=signature,
            method="GET",
            raw_path=b"/v1/me",
            raw_query=b"",
            body=b"",
            replay_store=replay_store,
            clock=lambda: float(timestamp),
        )


@pytest.mark.asyncio
async def test_proxy_timestamp_accepts_60_seconds_and_rejects_61() -> None:
    now = 1_700_000_000
    secret = b"proxy-secret-value"

    for timestamp in (now - 60, now + 60):
        request_id = f"req-{timestamp}"
        signature = proxy_security.sign_proxy_request(
            secret,
            timestamp=timestamp,
            request_id=request_id,
            method="GET",
            raw_path=b"/v1/me",
        )
        await proxy_security.verify_proxy_request(
            secret,
            timestamp_header=str(timestamp),
            request_id_header=request_id,
            signature_header=signature,
            method="GET",
            raw_path=b"/v1/me",
            replay_store=proxy_security.InMemoryReplayStore(clock=lambda: float(now)),
            clock=lambda: float(now),
        )

    for timestamp in (now - 61, now + 61):
        request_id = f"req-{timestamp}"
        signature = proxy_security.sign_proxy_request(
            secret,
            timestamp=timestamp,
            request_id=request_id,
            method="GET",
            raw_path=b"/v1/me",
        )
        with pytest.raises(proxy_security.StaleProxySignatureError):
            await proxy_security.verify_proxy_request(
                secret,
                timestamp_header=str(timestamp),
                request_id_header=request_id,
                signature_header=signature,
                method="GET",
                raw_path=b"/v1/me",
                replay_store=proxy_security.InMemoryReplayStore(
                    clock=lambda: float(now)
                ),
                clock=lambda: float(now),
            )


@pytest.mark.parametrize(
    ("header_name", "invalid_value"),
    [
        ("timestamp_header", None),
        ("timestamp_header", " 1700000000"),
        ("timestamp_header", "+1700000000"),
        ("request_id_header", None),
        ("request_id_header", "unsafe request id"),
        ("request_id_header", "a" * 129),
        ("signature_header", None),
        ("signature_header", "not-base64="),
    ],
)
@pytest.mark.asyncio
async def test_proxy_verification_rejects_invalid_headers(
    header_name: str,
    invalid_value: str | None,
) -> None:
    timestamp = 1_700_000_000
    secret = b"proxy-secret-value"
    request_id = "req-valid"
    signature = proxy_security.sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/me",
    )
    headers: dict[str, str | None] = {
        "timestamp_header": str(timestamp),
        "request_id_header": request_id,
        "signature_header": signature,
    }
    headers[header_name] = invalid_value

    with pytest.raises(proxy_security.InvalidProxySignatureError):
        await proxy_security.verify_proxy_request(
            secret,
            timestamp_header=headers["timestamp_header"],
            request_id_header=headers["request_id_header"],
            signature_header=headers["signature_header"],
            method="GET",
            raw_path=b"/v1/me",
            replay_store=proxy_security.InMemoryReplayStore(
                clock=lambda: float(timestamp)
            ),
            clock=lambda: float(timestamp),
        )


@pytest.mark.asyncio
async def test_invalid_proxy_signature_error_does_not_leak_request_material() -> None:
    timestamp = 1_700_000_000
    secret = b"proxy-secret-marker"
    signature = "a" * 43

    with pytest.raises(proxy_security.InvalidProxySignatureError) as exc_info:
        await proxy_security.verify_proxy_request(
            secret,
            timestamp_header=str(timestamp),
            request_id_header="req-secret-marker",
            signature_header=signature,
            method="POST",
            raw_path=b"/url-secret-marker",
            body=b"body-secret-marker",
            replay_store=proxy_security.InMemoryReplayStore(
                clock=lambda: float(timestamp)
            ),
            clock=lambda: float(timestamp),
        )

    rendered_error = f"{exc_info.value!s} {exc_info.value!r}"
    assert "secret-marker" not in rendered_error
    assert signature not in rendered_error


@pytest.mark.asyncio
async def test_in_memory_replay_store_uses_injected_clock_for_expiry() -> None:
    current_time = [100.0]
    store = proxy_security.InMemoryReplayStore(clock=lambda: current_time[0])

    assert await store.reserve("req-id", ttl_seconds=10) is True
    assert await store.reserve("req-id", ttl_seconds=10) is False
    current_time[0] = 111.0
    assert await store.reserve("req-id", ttl_seconds=10) is True


@pytest.mark.asyncio
async def test_redis_replay_store_uses_atomic_set_nx_ex_without_network() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, bool, int]] = []
            self.result: bool | None = True

        async def set(
            self,
            name: str,
            value: str,
            *,
            nx: bool,
            ex: int,
        ) -> bool | None:
            self.calls.append((name, value, nx, ex))
            return self.result

    redis = FakeRedis()
    store = proxy_security.RedisReplayStore(redis, key_prefix="test:proxy:")

    assert await store.reserve("req-id", ttl_seconds=121) is True
    assert redis.calls == [("test:proxy:req-id", "1", True, 121)]
    redis.result = None
    assert await store.reserve("req-id", ttl_seconds=121) is False
