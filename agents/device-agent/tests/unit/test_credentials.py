from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_agent.credentials import (
    AgentCredentials,
    CredentialStore,
    CredentialStoreError,
    InMemoryCredentialBackend,
    TaskSigningKey,
)
from pydantic import ValidationError
from perfpilot_agent.platform.linux import LinuxFileCredentialBackend

AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
TASK_KID = "task-key-2026-08"


def encoded_private_key(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def encoded_public_key(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def credentials(key: Ed25519PrivateKey) -> AgentCredentials:
    now = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    return AgentCredentials(
        schema_version="1.0",
        agent_id=AGENT_ID,
        private_key_b64=encoded_private_key(key),
        access_token="ppat_" + "A" * 43,
        access_token_expires_at=now + timedelta(minutes=15),
        refresh_token="pprt_" + "B" * 43,
        refresh_token_expires_at=now + timedelta(days=30),
        task_signing_key=TaskSigningKey(
            kid=TASK_KID,
            public_key_b64=encoded_public_key(key),
        ),
        heartbeat_interval_seconds=10,
    )


def test_credential_store_round_trips_one_versioned_secret_value(
    signing_key: Ed25519PrivateKey,
) -> None:
    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend)
    expected = credentials(signing_key)

    store.save(expected)

    assert store.load() == expected
    assert backend.value is not None
    assert b'"schema_version":"1.0"' in backend.value
    store.delete()
    assert store.load() is None


def test_credentials_and_store_errors_do_not_reveal_secrets(
    signing_key: Ed25519PrivateKey,
) -> None:
    expected = credentials(signing_key)
    rendered = repr(expected)

    assert expected.access_token not in rendered
    assert expected.refresh_token not in rendered
    assert expected.private_key_b64 not in rendered

    backend = InMemoryCredentialBackend(b'{"refresh_token":"pprt_leak"}')
    with pytest.raises(CredentialStoreError) as caught:
        CredentialStore(backend).load()
    assert "pprt_leak" not in str(caught.value)


def test_source_capable_credentials_bind_team_only_in_version_1_1(
    signing_key: Ed25519PrivateKey,
) -> None:
    legacy = credentials(signing_key)
    source_capable = legacy.model_copy(
        update={
            "schema_version": "1.1",
            "team_id": UUID("10000000-0000-4000-8000-000000000001"),
        }
    )

    assert source_capable.team_id == UUID("10000000-0000-4000-8000-000000000001")
    with pytest.raises(ValidationError):
        AgentCredentials.model_validate(
            {**legacy.model_dump(mode="json"), "schema_version": "1.1"}
        )
    with pytest.raises(ValidationError):
        AgentCredentials.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "team_id": "10000000-0000-4000-8000-000000000001",
            }
        )


def test_linux_file_backend_replaces_atomically_with_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "credentials.json"
    backend = LinuxFileCredentialBackend(target, require_root_owner=False)

    backend.write(b"first")
    backend.write(b"second")

    assert backend.read() == b"second"
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [target]
