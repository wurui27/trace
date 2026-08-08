from __future__ import annotations

import base64
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from perfpilot_agent.source_registry import SourceWorkspaceRegistry

WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
PROFILE_ID = UUID("94000000-0000-4000-8000-000000000001")


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


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "-C", str(path), "init", "--quiet", "--initial-branch=main"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "PerfPilot Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "perfpilot@example.test"],
        check=True,
    )
    wrapper = path / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    subprocess.run(["git", "-C", str(path), "add", "gradlew"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--quiet", "-m", "initial"],
        check=True,
    )
    return path


def test_cli_exposes_the_exact_service_commands() -> None:
    parser = cli._parser()

    assert parser.parse_args(["run"]).command == "run"
    assert parser.parse_args(["status", "--json"]).command == "status"
    assert parser.parse_args(["doctor", "--json"]).command == "doctor"
    assert parser.parse_args(["unregister", "--local-only"]).command == "unregister"

    source_add = parser.parse_args(["source", "add", "--name", "Demo", "--path", "/repo"])
    assert (source_add.command, source_add.source_command) == ("source", "add")
    source_list = parser.parse_args(["source", "list", "--json"])
    assert (source_list.command, source_list.source_command) == ("source", "list")
    source_doctor = parser.parse_args(
        ["source", "doctor", "--workspace-id", str(WORKSPACE_ID), "--json"]
    )
    assert source_doctor.workspace_id == WORKSPACE_ID
    validation_add = parser.parse_args(
        [
            "source",
            "validation",
            "add",
            "--workspace-id",
            str(WORKSPACE_ID),
            "--name",
            "Android check",
            "--working-directory",
            ".",
            "--timeout-seconds",
            "600",
            "--allowed-exit-code",
            "0",
            "--",
            "./gradlew",
            ":app:lintDebug",
            "--no-daemon",
            "--console=plain",
        ]
    )
    assert (
        validation_add.source_command,
        validation_add.validation_command,
        validation_add.command_argv,
    ) == (
        "validation",
        "add",
        ["--", "./gradlew", ":app:lintDebug", "--no-daemon", "--console=plain"],
    )


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


def test_source_cli_keeps_validation_argv_separate_and_outputs_only_public_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    repo = _git_repo(tmp_path / "private-source")
    identifiers = iter((WORKSPACE_ID, PROFILE_ID))
    registry = SourceWorkspaceRegistry(
        tmp_path / "registry-state",
        uuid_factory=lambda: next(identifiers),
    )
    monkeypatch.setattr(cli, "_source_registry", lambda _config: registry)

    assert cli.main(
        ["--config", config_path, "source", "add", "--name", "Demo", "--path", str(repo)]
    ) == 0
    add_output = capsys.readouterr().out
    assert json.loads(add_output) == {
        "name": "Demo",
        "validation_profiles": [],
        "workspace_id": str(WORKSPACE_ID),
    }

    assert cli.main(
        [
            "--config",
            config_path,
            "source",
            "validation",
            "add",
            "--workspace-id",
            str(WORKSPACE_ID),
            "--name",
            "Android check",
            "--working-directory",
            ".",
            "--timeout-seconds",
            "600",
            "--allowed-exit-code",
            "0",
            "--",
            "./gradlew",
            ":app:lintDebug",
            "--no-daemon",
            "--console=plain",
        ]
    ) == 0
    validation_output = capsys.readouterr().out
    assert json.loads(validation_output) == {
        "name": "Android check",
        "profile_id": str(PROFILE_ID),
    }
    assert registry.list_validation(WORKSPACE_ID)[0].argv == (
        "./gradlew",
        ":app:lintDebug",
        "--no-daemon",
        "--console=plain",
    )

    assert cli.main(["--config", config_path, "source", "list", "--json"]) == 0
    list_output = capsys.readouterr().out
    listed = json.loads(list_output)
    assert listed[0]["workspace_id"] == str(WORKSPACE_ID)
    assert listed[0]["validation_profiles"] == [
        {"name": "Android check", "profile_id": str(PROFILE_ID)}
    ]
    assert str(repo) not in list_output
    assert "./gradlew" not in list_output
    assert ":app:lintDebug" not in list_output


def test_source_validation_cli_requires_explicit_separator(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    registry = SourceWorkspaceRegistry(
        tmp_path / "registry-state",
        uuid_factory=lambda: WORKSPACE_ID,
    )
    registry.add(name="Demo", path=repo)
    monkeypatch.setattr(cli, "_source_registry", lambda _config: registry)

    result = cli.main(
        [
            "--config",
            config_path,
            "source",
            "validation",
            "add",
            "--workspace-id",
            str(WORKSPACE_ID),
            "--name",
            "Android check",
            "--working-directory",
            ".",
            "--timeout-seconds",
            "600",
            "--allowed-exit-code",
            "0",
            "./gradlew",
            "lint",
        ]
    )

    assert result == 2
    assert registry.list_validation(WORKSPACE_ID) == ()
    assert str(repo) not in capsys.readouterr().err


def test_source_cli_errors_never_echo_private_paths(tmp_path: Path, capsys) -> None:
    config_path = _config(tmp_path)
    private_path = tmp_path / "not-a-git-repository"
    private_path.mkdir()

    result = cli.main(
        [
            "--config",
            config_path,
            "source",
            "add",
            "--name",
            "Demo",
            "--path",
            str(private_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert str(private_path) not in captured.out
    assert str(private_path) not in captured.err
