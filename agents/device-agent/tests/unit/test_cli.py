from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_agent import cli
from perfpilot_agent.credentials import (
    AgentCredentials,
    CredentialStore,
    InMemoryCredentialBackend,
    TaskSigningKey,
)


def _credentials() -> AgentCredentials:
    now = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    private_key = Ed25519PrivateKey.generate()
    private = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return AgentCredentials(
        schema_version="1.0",
        agent_id=UUID("71000000-0000-4000-8000-000000000001"),
        private_key_b64=base64.b64encode(private).decode("ascii"),
        access_token="ppat_" + "A" * 43,
        access_token_expires_at=now + timedelta(minutes=15),
        refresh_token="pprt_" + "B" * 43,
        refresh_token_expires_at=now + timedelta(days=30),
        task_signing_key=TaskSigningKey(
            kid="task-key-2026-08",
            public_key_b64=base64.b64encode(public).decode("ascii"),
        ),
        heartbeat_interval_seconds=10,
    )


def _config(tmp_path) -> str:
    ca = tmp_path / "ca.crt"
    ca.write_text("test", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = tmp_path / "agent.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "server_url": "https://control.example.test",
                "ca_bundle": str(ca),
                "adb_path": None,
                "workspace_root": str(workspace),
            }
        ),
        encoding="utf-8",
    )
    return str(config)


def test_cli_exposes_the_exact_service_commands() -> None:
    parser = cli._parser()

    assert parser.parse_args(["run"]).command == "run"
    assert parser.parse_args(["status", "--json"]).command == "status"
    assert parser.parse_args(["doctor", "--json"]).command == "doctor"
    assert parser.parse_args(["unregister", "--local-only"]).command == "unregister"


def test_status_json_never_contains_credentials(tmp_path, monkeypatch, capsys) -> None:
    backend = InMemoryCredentialBackend()
    credentials = _credentials()
    CredentialStore(backend).save(credentials)
    monkeypatch.setattr(cli, "_credential_backend", lambda: backend)

    result = cli.main(["--config", _config(tmp_path), "status", "--json"])

    output = capsys.readouterr().out
    document = json.loads(output)
    assert result == 0
    assert document == {
        "agent_id": str(credentials.agent_id),
        "registered": True,
        "schema_version": "1.0",
        "state": "stopped",
    }
    assert credentials.access_token not in output
    assert credentials.refresh_token not in output
    assert credentials.private_key_b64 not in output


def test_local_unregister_requires_typed_confirmation(tmp_path, monkeypatch) -> None:
    backend = InMemoryCredentialBackend()
    CredentialStore(backend).save(_credentials())
    monkeypatch.setattr(cli, "_credential_backend", lambda: backend)
    monkeypatch.setattr("builtins.input", lambda _: "UNREGISTER")

    result = cli.main(["--config", _config(tmp_path), "unregister", "--local-only"])

    assert result == 0
    assert CredentialStore(backend).load() is None
