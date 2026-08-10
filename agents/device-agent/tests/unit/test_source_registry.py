from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path, PureWindowsPath
from threading import Event
from uuid import UUID

import pytest

from perfpilot_agent.source_models import SourceWorkspace, ValidationProfile
from perfpilot_agent.source_registry import (
    SourceRegistryError,
    SourceWorkspaceRegistry,
    normalize_source_path,
)

WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000002")
PROFILE_ID = UUID("94000000-0000-4000-8000-000000000001")
OTHER_PROFILE_ID = UUID("94000000-0000-4000-8000-000000000002")


def _run_git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _run_git(path, "init", "--quiet", "--initial-branch=main")
    _run_git(path, "config", "user.name", "PerfPilot Test")
    _run_git(path, "config", "user.email", "perfpilot@example.test")
    (path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    wrapper = path / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    _run_git(path, "add", "tracked.txt", "gradlew")
    _run_git(path, "commit", "--quiet", "-m", "initial")
    return path


def _profile(
    *,
    profile_id: UUID = PROFILE_ID,
    argv: tuple[str, ...] = (
        "./gradlew",
        ":app:lintDebug",
        "--no-daemon",
        "--console=plain",
    ),
    working_directory: str = ".",
    timeout_seconds: int = 600,
    allowed_exit_codes: tuple[int, ...] = (0,),
) -> ValidationProfile:
    return ValidationProfile(
        profile_id=profile_id,
        name="Android check",
        argv=argv,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
        allowed_exit_codes=allowed_exit_codes,
    )


def test_models_are_immutable_slotted_and_private_in_repr(tmp_path: Path) -> None:
    repo = tmp_path / "private-source"
    profile = _profile()
    workspace = SourceWorkspace(WORKSPACE_ID, "Demo Android", repo, (profile,))

    with pytest.raises(FrozenInstanceError):
        workspace.name = "changed"  # type: ignore[misc]

    assert not hasattr(workspace, "__dict__")
    assert str(repo) not in repr(workspace)
    assert "./gradlew" not in repr(workspace)
    assert ":app:lintDebug" not in repr(workspace)
    assert workspace.public_document() == {
        "workspace_id": str(WORKSPACE_ID),
        "name": "Demo Android",
        "validation_profiles": [
            {"profile_id": str(PROFILE_ID), "name": "Android check"}
        ],
    }


def test_registers_lists_doctors_and_removes_without_exposing_private_data(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "真实源码")
    agent_root = tmp_path / "agent-state"
    identifiers = iter((WORKSPACE_ID, PROFILE_ID))
    registry = SourceWorkspaceRegistry(agent_root, uuid_factory=lambda: next(identifiers))

    workspace = registry.add(name="演示 Android", path=repo)
    profile = registry.add_validation(
        workspace_id=workspace.workspace_id,
        name="Android check",
        argv=("./gradlew", ":app:lintDebug", "--no-daemon", "--console=plain"),
        working_directory=".",
        timeout_seconds=600,
        allowed_exit_codes=(0,),
    )
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked-secret.txt").write_text("ignored\n", encoding="utf-8")

    loaded = registry.list()
    doctor = registry.doctor(workspace.workspace_id)
    public_json = json.dumps(doctor, ensure_ascii=False)

    assert workspace.workspace_id == WORKSPACE_ID
    assert profile.profile_id == PROFILE_ID
    assert loaded == (
        SourceWorkspace(WORKSPACE_ID, "演示 Android", repo.resolve(), (profile,)),
    )
    assert doctor == {
        "workspace_id": str(WORKSPACE_ID),
        "name": "演示 Android",
        "state": "ready",
        "git_branch": "main",
        "git_head": subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "tracked_dirty_count": 1,
        "snapshot_policy": "tracked_worktree",
        "validation_profiles": [
            {"profile_id": str(PROFILE_ID), "name": "Android check"}
        ],
    }
    assert registry.list_validation(WORKSPACE_ID) == (profile,)
    assert str(repo) not in repr(workspace)
    assert str(repo) not in public_json
    assert "./gradlew" not in public_json
    assert "untracked-secret" not in public_json
    if registry.registry_path.stat().st_mode:
        assert stat.S_IMODE(registry.registry_path.stat().st_mode) == 0o600

    registry.remove_validation(WORKSPACE_ID, PROFILE_ID)
    assert registry.list_validation(WORKSPACE_ID) == ()
    registry.remove(WORKSPACE_ID)
    assert registry.list() == ()


@pytest.mark.parametrize(
    "path_kind",
    ["relative", "non_git", "inside_agent_root"],
)
def test_registration_rejects_unsafe_workspace_paths_without_echoing_them(
    tmp_path: Path,
    path_kind: str,
) -> None:
    agent_root = tmp_path / "agent-state"
    registry = SourceWorkspaceRegistry(agent_root, uuid_factory=lambda: WORKSPACE_ID)
    if path_kind == "relative":
        candidate = Path("relative/repository")
    elif path_kind == "non_git":
        candidate = tmp_path / "not-a-repository"
        candidate.mkdir()
    else:
        candidate = _git_repo(agent_root / "nested-repository")

    with pytest.raises(SourceRegistryError) as captured:
        registry.add(name="Demo", path=candidate)

    assert str(candidate) not in str(captured.value)
    assert str(candidate) not in repr(captured.value)


def test_registration_rejects_plain_directory_despite_hostile_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_repo(tmp_path / "real-repository")
    candidate = tmp_path / "plain-directory"
    candidate.mkdir()
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(candidate))
    registry = SourceWorkspaceRegistry(
        tmp_path / "agent-state",
        uuid_factory=lambda: WORKSPACE_ID,
    )

    with pytest.raises(SourceRegistryError) as captured:
        registry.add(name="Demo", path=candidate)

    assert str(repo) not in str(captured.value)
    assert str(candidate) not in str(captured.value)


def test_stale_workspace_can_be_listed_published_and_removed(
    tmp_path: Path,
) -> None:
    stale_repo = _git_repo(tmp_path / "stale-repository")
    healthy_repo = _git_repo(tmp_path / "healthy-repository")
    identifiers = iter((WORKSPACE_ID, OTHER_WORKSPACE_ID))
    registry = SourceWorkspaceRegistry(
        tmp_path / "agent-state",
        uuid_factory=lambda: next(identifiers),
    )
    stale = registry.add(name="Stale", path=stale_repo)
    healthy = registry.add(name="Healthy", path=healthy_repo)

    shutil.rmtree(stale_repo)

    assert registry.list() == (stale, healthy)
    public = registry.public_workspaces()
    assert public[0] == {
        "workspace_id": str(WORKSPACE_ID),
        "name": "Stale",
        "state": "invalid",
        "git_branch": None,
        "git_head": "0" * 40,
        "tracked_dirty_count": 0,
        "snapshot_policy": "tracked_worktree",
        "validation_profiles": [],
    }
    assert public[1]["workspace_id"] == str(OTHER_WORKSPACE_ID)
    assert public[1]["state"] == "ready"
    assert str(stale_repo) not in json.dumps(public)

    registry.remove(WORKSPACE_ID)

    assert registry.list() == (healthy,)


def test_registration_rejects_duplicate_names_and_non_v4_generated_ids(tmp_path: Path) -> None:
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    registry = SourceWorkspaceRegistry(tmp_path / "agent", uuid_factory=lambda: WORKSPACE_ID)
    registry.add(name="Demo", path=first)

    with pytest.raises(SourceRegistryError):
        registry.add(name="Demo", path=second)

    invalid = SourceWorkspaceRegistry(
        tmp_path / "other-agent",
        uuid_factory=lambda: UUID("92000000-0000-1000-8000-000000000001"),
    )
    with pytest.raises(SourceRegistryError):
        invalid.add(name="Other", path=second)


@pytest.mark.parametrize(
    ("argv", "working_directory", "timeout_seconds", "exit_codes"),
    [
        ("./gradlew :app:lintDebug", ".", 600, (0,)),
        (("/usr/bin/gradle", "lint"), ".", 600, (0,)),
        (("../gradlew", "lint"), ".", 600, (0,)),
        (("./gradlew", "lint", "|", "tee", "out"), ".", 600, (0,)),
        (("./gradlew", "$(touch /tmp/unsafe)"), ".", 600, (0,)),
        (("./gradlew", "lint"), "/tmp", 600, (0,)),
        (("./gradlew", "lint"), "../outside", 600, (0,)),
        (("./gradlew", "lint"), ".", 0, (0,)),
        (("./gradlew", "lint"), ".", 1201, (0,)),
        (("./gradlew", "lint"), ".", 600, (0, 0)),
        (("./gradlew", "lint"), ".", 600, (-1,)),
        (("./gradlew", "lint"), ".", 600, (256,)),
    ],
)
def test_validation_profile_rejects_shell_and_boundary_violations(
    tmp_path: Path,
    argv: object,
    working_directory: str,
    timeout_seconds: int,
    exit_codes: tuple[int, ...],
) -> None:
    repo = _git_repo(tmp_path / "repo")
    registry = SourceWorkspaceRegistry(tmp_path / "agent", uuid_factory=lambda: WORKSPACE_ID)

    with pytest.raises(SourceRegistryError):
        registry.add(
            name="Demo",
            path=repo,
            validation_profiles=(
                ValidationProfile(
                    profile_id=PROFILE_ID,
                    name="Unsafe",
                    argv=argv,  # type: ignore[arg-type]
                    working_directory=working_directory,
                    timeout_seconds=timeout_seconds,
                    allowed_exit_codes=exit_codes,
                ),
            ),
        )


def test_deserialization_accepts_crlf_unicode_and_revalidates_ids_and_structure(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "unicode-repo")
    root = tmp_path / "agent"
    root.mkdir()
    registry_path = root / "source-workspaces.json"
    document = {
        "schema_version": "1.0",
        "workspaces": [
            {
                "workspace_id": str(WORKSPACE_ID),
                "name": "源码库",
                "path": str(repo),
                "validation_profiles": [],
            }
        ],
    }
    registry_path.write_bytes(
        json.dumps(document, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")
    )
    registry_path.chmod(0o600)
    registry = SourceWorkspaceRegistry(root)

    assert registry.list()[0].name == "源码库"

    document["workspaces"][0]["workspace_id"] = "92000000-0000-1000-8000-000000000001"
    registry_path.write_text(json.dumps(document), encoding="utf-8")
    registry_path.chmod(0o600)
    with pytest.raises(SourceRegistryError) as captured:
        registry.list()
    assert str(repo) not in str(captured.value)


def test_atomic_replace_failure_leaves_previous_registry_intact(tmp_path: Path) -> None:
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    root = tmp_path / "agent"
    SourceWorkspaceRegistry(root, uuid_factory=lambda: WORKSPACE_ID).add(
        name="First",
        path=first,
    )
    before = (root / "source-workspaces.json").read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated atomic replacement failure")

    failing = SourceWorkspaceRegistry(
        root,
        uuid_factory=lambda: OTHER_WORKSPACE_ID,
        replace=fail_replace,
    )
    with pytest.raises(SourceRegistryError):
        failing.add(name="Second", path=second)

    assert (root / "source-workspaces.json").read_bytes() == before
    assert SourceWorkspaceRegistry(root).list()[0].name == "First"


def test_concurrent_adds_are_serialized_without_losing_updates(tmp_path: Path) -> None:
    first_repo = _git_repo(tmp_path / "first")
    second_repo = _git_repo(tmp_path / "second")
    root = tmp_path / "agent"
    first_replace_entered = Event()
    release_first_replace = Event()
    second_replace_completed = Event()

    def blocking_replace(source: Path, destination: Path) -> None:
        first_replace_entered.set()
        if not release_first_replace.wait(timeout=5):
            raise OSError("timed out waiting to release replacement")
        os.replace(source, destination)

    def observed_replace(source: Path, destination: Path) -> None:
        os.replace(source, destination)
        second_replace_completed.set()

    first = SourceWorkspaceRegistry(
        root,
        uuid_factory=lambda: WORKSPACE_ID,
        replace=blocking_replace,
    )
    second = SourceWorkspaceRegistry(
        root,
        uuid_factory=lambda: OTHER_WORKSPACE_ID,
        replace=observed_replace,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.add, name="First", path=first_repo)
        assert first_replace_entered.wait(timeout=2)
        second_future = executor.submit(second.add, name="Second", path=second_repo)
        try:
            assert not second_replace_completed.wait(timeout=0.5)
        finally:
            release_first_replace.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    assert {workspace.name for workspace in first.list()} == {"First", "Second"}
    assert stat.S_IMODE((root / ".source-workspaces.lock").stat().st_mode) == 0o600


def test_windows_drive_path_normalization_is_case_insensitive() -> None:
    first = normalize_source_path(PureWindowsPath(r"C:\Source\Demo"), platform_name="windows")
    second = normalize_source_path(PureWindowsPath(r"c:\source\.\DEMO"), platform_name="windows")

    assert first == second == r"c:\source\demo"
