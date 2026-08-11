from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from jsonschema import Draft202012Validator, FormatChecker

from perfpilot_agent import cli
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import SourceTaskExecuteResponse
from perfpilot_agent.credentials import AgentCredentials, TaskSigningKey
from perfpilot_agent.service import TaskLoop
from perfpilot_agent.source_registry import SourceWorkspaceRegistry
from perfpilot_agent.state import AgentRuntimeState


class SourceControl:
    def __init__(self, response, credentials) -> None:
        self.response = response
        self.credentials = credentials
        self.completions: list[dict[str, object]] = []

    async def poll_task(self, *, wait_seconds: int = 20):
        return self.response

    async def acknowledge_cancellation(self, **kwargs):
        raise AssertionError("unexpected cancellation")

    async def complete_source_task(self, **kwargs):
        self.completions.append(kwargs)
        return object()


class AdbCaptureProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, _task) -> None:
        self.calls += 1
        raise AssertionError("source tasks must not initialize ADB capture")


def _git(repo: Path, *arguments: str) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=main")
    source = path / "app/src/main/java/demo/Startup.kt"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package demo\nclass Startup { fun onCreate() = Thread.sleep(20) }\n",
        encoding="utf-8",
    )
    (path / "local.properties").write_text(
        "api_key=TRACKED_SECRET_SENTINEL\n", encoding="utf-8"
    )
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=PerfPilot Test",
        "-c",
        "user.email=perfpilot@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    _git(path, "remote", "add", "origin", "https://secret.example/private.git")
    (path / "untracked.kt").write_text("UNTRACKED_SECRET_SENTINEL\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_signed_source_context_executes_without_adb_and_emits_closed_completion(
    tmp_path: Path,
    signing_key,
) -> None:
    now = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
    agent_id = UUID("71000000-0000-4000-8000-000000000001")
    team_id = UUID("10000000-0000-4000-8000-000000000001")
    private = signing_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    credentials = AgentCredentials(
        schema_version="1.1",
        agent_id=agent_id,
        team_id=team_id,
        private_key_b64=base64.b64encode(private).decode(),
        access_token="ppat_" + "A" * 43,
        access_token_expires_at=now + timedelta(minutes=15),
        refresh_token="pprt_" + "B" * 43,
        refresh_token_expires_at=now + timedelta(days=30),
        task_signing_key=TaskSigningKey(
            kid="task-key-2026-08",
            public_key_b64=base64.b64encode(public).decode(),
        ),
        heartbeat_interval_seconds=10,
    )
    workspace_root = tmp_path / "agent"
    workspace = SourceWorkspaceRegistry(workspace_root).add(
        name="Demo",
        path=_repo(tmp_path / "private-source"),
    )
    snapshot = {
        "schema_version": "1.0",
        "aud": "perfpilot-agent",
        "task_type": "source_context",
        "execution_id": "93000000-0000-4000-8000-000000000001",
        "analysis_id": "82000000-0000-4000-8000-000000000001",
        "team_id": str(team_id),
        "agent_id": str(agent_id),
        "workspace_id": str(workspace.workspace_id),
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
        "lease_version": 1,
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "finding_hints": [],
        "limits": {"max_findings": 3, "max_files": 12, "max_bytes": 98_304},
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    control = SourceControl(
        SourceTaskExecuteResponse(
            schema_version="1.1",
            task_kind="source",
            lease_token="opaque-source-lease-token",
            snapshot=snapshot,
            signature_b64=base64.b64encode(signing_key.sign(canonical)).decode(),
        ),
        credentials,
    )
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("test ca", encoding="utf-8")
    config = AgentConfig(
        server_url="https://control.example.test",
        ca_bundle=ca_bundle,
        workspace_root=workspace_root,
    )
    capture = AdbCaptureProbe()
    loop = TaskLoop(
        control=control,
        executor=capture,
        source_executor=cli._source_task_runner(config=config, control=control),
        state=AgentRuntimeState(),
        clock=lambda: now,
    )

    handled = await loop.poll_once()

    assert handled is True
    assert capture.calls == 0
    completion = control.completions[0]["completion"]
    assert isinstance(completion, dict)
    wire = {
        **completion,
        "team_id": str(team_id),
        "agent_id": str(agent_id),
        "signature_b64": "A" * 86 + "==",
    }
    contract = json.loads(
        Path("contracts/v1/agents/source-task-completion.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(contract, format_checker=FormatChecker()).validate(wire)
    assert all(
        fragment["snapshot_hash"] == wire["result"]["snapshot_hash"]
        for fragment in wire["result"]["fragments"]
    )
    serialized = json.dumps(wire, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "secret.example" not in serialized
    assert "TRACKED_SECRET_SENTINEL" not in serialized
    assert "UNTRACKED_SECRET_SENTINEL" not in serialized
