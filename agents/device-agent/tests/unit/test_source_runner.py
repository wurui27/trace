from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_agent.security import SourceLimits, VerifiedSourceContextTask
from perfpilot_agent.source_registry import SourceWorkspaceRegistry
from perfpilot_agent.source_runner import SourceTaskRunner


class RecordingControl:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete_source_task(self, **kwargs):
        self.calls.append(kwargs)
        return object()


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
    source.write_text("class Startup { fun start() = Thread.sleep(1) }\n", encoding="utf-8")
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
    return path


def _task(workspace_id: UUID) -> VerifiedSourceContextTask:
    return VerifiedSourceContextTask(
        schema_version="1.0",
        aud="perfpilot-agent",
        task_type="source_context",
        execution_id=UUID("93000000-0000-4000-8000-000000000001"),
        analysis_id=UUID("82000000-0000-4000-8000-000000000001"),
        team_id=UUID("10000000-0000-4000-8000-000000000001"),
        agent_id=UUID("71000000-0000-4000-8000-000000000001"),
        workspace_id=workspace_id,
        snapshot_policy="tracked_worktree",
        validation_profile_id=None,
        lease_version=1,
        expires_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        finding_hints=(),
        limits=SourceLimits(max_findings=3, max_files=12, max_bytes=98_304),
    )


@pytest.mark.asyncio
async def test_runner_validates_workspace_and_completes_bounded_context(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    registry = SourceWorkspaceRegistry(root)
    workspace = registry.add(name="Demo", path=_repo(tmp_path / "private-source"))
    control = RecordingControl()
    runner = SourceTaskRunner(control=control, registry=registry, cache_root=root / "source-cache")

    await runner.run(_task(workspace.workspace_id), lease_token="opaque-token")

    call = control.calls[0]
    completion = call["completion"]
    assert call["lease_token"] == "opaque-token"
    assert isinstance(completion, dict)
    assert completion["state"] == "completed"
    assert completion["workspace_id"] == str(workspace.workspace_id)
    result = completion["result"]
    assert isinstance(result, dict)
    assert result["fragments"][0]["relative_path"].endswith("Startup.kt")
    assert str(tmp_path) not in repr(completion)
    assert len(runner.canonical_completion_bytes(completion)) <= 128 * 1024


@pytest.mark.asyncio
async def test_runner_returns_closed_failure_for_unknown_workspace(tmp_path: Path) -> None:
    control = RecordingControl()
    runner = SourceTaskRunner(
        control=control,
        registry=SourceWorkspaceRegistry(tmp_path / "agent"),
        cache_root=tmp_path / "agent/source-cache",
    )

    await runner.run(
        _task(UUID("92000000-0000-4000-8000-000000000099")),
        lease_token="opaque-token",
    )

    completion = control.calls[0]["completion"]
    assert completion["state"] == "failed"
    assert completion["result"] == {
        "failure_code": "source_workspace_unavailable",
        "retryable": False,
    }
    assert str(tmp_path) not in repr(completion)
