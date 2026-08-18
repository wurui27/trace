from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from perfpilot_agent.config import AgentConfig, load_config


def valid_paths(tmp_path: Path) -> tuple[Path, Path]:
    ca_bundle = tmp_path / "perfpilot-ca.crt"
    ca_bundle.write_text("test-ca", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    return ca_bundle, workspace


@pytest.mark.parametrize(
    "server_url",
    [
        "http://10.166.0.125",
        "https://user@10.166.0.125",
        "https://10.166.0.125?debug=1",
        "https://10.166.0.125#debug",
    ],
)
def test_config_requires_a_closed_https_origin(tmp_path: Path, server_url: str) -> None:
    ca_bundle, workspace = valid_paths(tmp_path)

    with pytest.raises(ValidationError):
        AgentConfig(
            schema_version="1.0",
            server_url=server_url,
            ca_bundle=ca_bundle,
            workspace_root=workspace,
        )


def test_config_requires_absolute_readable_ca_and_workspace(tmp_path: Path) -> None:
    ca_bundle, workspace = valid_paths(tmp_path)

    with pytest.raises(ValidationError):
        AgentConfig(
            schema_version="1.0",
            server_url="https://10.166.0.125",
            ca_bundle=Path("relative-ca.crt"),
            workspace_root=workspace,
        )
    with pytest.raises(ValidationError):
        AgentConfig(
            schema_version="1.0",
            server_url="https://10.166.0.125",
            ca_bundle=ca_bundle,
            workspace_root=Path("relative-work"),
        )


def test_load_config_rejects_unknown_bootstrap_fields(tmp_path: Path) -> None:
    ca_bundle, workspace = valid_paths(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "server_url": "https://10.166.0.125",
                "ca_bundle": str(ca_bundle),
                "adb_path": None,
                "workspace_root": str(workspace),
                "access_token": "must-not-be-bootstrap-config",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_valid_config_is_immutable(tmp_path: Path) -> None:
    ca_bundle, workspace = valid_paths(tmp_path)
    config = AgentConfig(
        schema_version="1.0",
        server_url="https://10.166.0.125",
        ca_bundle=ca_bundle,
        workspace_root=workspace,
    )

    with pytest.raises(ValidationError):
        config.server_url = "https://example.com"


def test_capture_script_root_must_be_an_absolute_real_directory(tmp_path: Path) -> None:
    ca_bundle, workspace = valid_paths(tmp_path)
    scripts = tmp_path / "capture-scripts"
    scripts.mkdir()

    config = AgentConfig(
        schema_version="1.0",
        server_url="https://10.166.0.125",
        ca_bundle=ca_bundle,
        workspace_root=workspace,
        capture_script_root=scripts,
    )
    assert config.capture_script_root == scripts

    alias = tmp_path / "capture-scripts-link"
    alias.symlink_to(scripts, target_is_directory=True)
    with pytest.raises(ValidationError):
        AgentConfig(
            schema_version="1.0",
            server_url="https://10.166.0.125",
            ca_bundle=ca_bundle,
            workspace_root=workspace,
            capture_script_root=alias,
        )
