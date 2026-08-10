from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_agent.source_snapshot import (
    MAX_SNAPSHOT_BYTES,
    SourceSnapshotError,
    SourceSnapshotter,
)


WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def _git(repo: Path, *arguments: str) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    ).stdout


def _write(repo: Path, relative: str, content: bytes) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _commit(repo: Path, message: str = "fixture") -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=PerfPilot Test",
        "-c",
        "user.email=perfpilot@example.test",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _mixed_git_repo(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _write(repo, ".gitignore", b"ignored.kt\n")
    _write(repo, "app/src/main/java/demo/Committed.kt", b"class Committed\n")
    _write(repo, "app/src/main/java/demo/MainActivity.kt", b"committed\n")
    _write(repo, "app/src/main/java/demo/deleted.kt", b"delete me\n")
    _write(repo, "app/src/main/java/demo/Binary.kt", b"prefix\x00suffix")
    _write(repo, "app/src/main/java/demo/Secret.kt", b'val api_key = "SECRET_SENTINEL"\n')
    _write(repo, "local.properties", b"sdk.dir=/private/android\n")
    _write(repo, "windows\\Injected.kt", b"class Injected\n")
    unicode_path = _write(repo, "app/src/main/java/demo/启动.kt", "类 启动\r\n".encode())
    target = _write(repo, "outside.kt", b"outside\n")
    (repo / "app/src/main/java/demo/Alias.kt").symlink_to(target)
    _commit(repo)

    _write(repo, "app/src/main/java/demo/MainActivity.kt", b"staged\n")
    _git(repo, "add", "app/src/main/java/demo/MainActivity.kt")
    _write(repo, "app/src/main/java/demo/MainActivity.kt", b"staged + unstaged\n")
    (repo / "app/src/main/java/demo/deleted.kt").unlink()
    _write(repo, "untracked.kt", b"untracked sentinel\n")
    _write(repo, "ignored.kt", b"ignored sentinel\n")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/fake")
    assert unicode_path.read_bytes().endswith(b"\r\n")
    return repo


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        mode = path.lstat().st_mode
        digest.update(str(stat.S_IFMT(mode) | stat.S_IMODE(mode)).encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_snapshot_captures_current_tracked_tree_without_touching_real_git(
    tmp_path: Path,
) -> None:
    repo = _mixed_git_repo(tmp_path / "app")
    before = _tree_digest(repo)
    git_before = _tree_digest(repo / ".git")

    result = SourceSnapshotter(cache_root=tmp_path / "cache").create(
        repo,
        WORKSPACE_ID,
        created_at=CREATED_AT,
    )

    assert result.read_text("app/src/main/java/demo/MainActivity.kt") == (
        "staged + unstaged\n"
    )
    assert result.read_text("app/src/main/java/demo/启动.kt") == "类 启动\r\n"
    assert "app/src/main/java/demo/deleted.kt" in result.deleted_paths
    assert "untracked.kt" not in result.paths
    assert "ignored.kt" not in result.paths
    assert "app/src/main/java/demo/Alias.kt" not in result.paths
    assert "vendor/fake" not in result.paths
    assert "app/src/main/java/demo/Binary.kt" not in result.paths
    assert "app/src/main/java/demo/Secret.kt" not in result.paths
    assert "local.properties" not in result.paths
    assert "windows\\Injected.kt" not in result.paths
    assert {(item.relative_path, item.reason_code) for item in result.exclusions} >= {
        ("app/src/main/java/demo/Alias.kt", "symlink"),
        ("vendor/fake", "submodule"),
        ("app/src/main/java/demo/Binary.kt", "binary_file"),
        ("app/src/main/java/demo/Secret.kt", "sensitive_content"),
        ("local.properties", "sensitive_file"),
        (None, "invalid_path"),
    }
    assert _tree_digest(repo) == before
    assert _tree_digest(repo / ".git") == git_before
    assert stat.S_IMODE(result.cache_path.stat().st_mode) == 0o700
    assert _git(result.tree_path, "rev-parse", "HEAD").decode().strip() == result.git_commit


def test_snapshot_hash_is_deterministic_but_snapshot_ids_are_never_reused(
    tmp_path: Path,
) -> None:
    repo = _mixed_git_repo(tmp_path / "app")
    snapshotter = SourceSnapshotter(cache_root=tmp_path / "cache")

    first = snapshotter.create(repo, WORKSPACE_ID, created_at=CREATED_AT)
    second = snapshotter.create(repo, WORKSPACE_ID, created_at=CREATED_AT)

    assert first.snapshot_hash == second.snapshot_hash
    assert first.git_commit == second.git_commit
    assert first.snapshot_id != second.snapshot_id
    assert first.cache_path != second.cache_path


def test_snapshot_rejects_oversized_tracked_file_before_reading_it(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    huge = _write(repo, "Huge.kt", b"small\n")
    _commit(repo)
    huge.open("r+b").truncate(MAX_SNAPSHOT_BYTES + 1)

    result = SourceSnapshotter(cache_root=tmp_path / "cache").create(
        repo,
        WORKSPACE_ID,
        created_at=CREATED_AT,
    )

    assert "Huge.kt" not in result.paths
    assert ("Huge.kt", "file_too_large") in {
        (item.relative_path, item.reason_code) for item in result.exclusions
    }


def test_snapshot_strips_hostile_git_environment(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _write(repo, "Main.kt", b"class Main\n")
    _commit(repo)
    hostile = tmp_path / "hostile-index"
    hostile.write_bytes(b"not an index")
    monkeypatch.setenv("GIT_INDEX_FILE", str(hostile))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "missing-objects"))

    result = SourceSnapshotter(cache_root=tmp_path / "cache").create(
        repo,
        WORKSPACE_ID,
        created_at=CREATED_AT,
    )

    assert result.paths == ("Main.kt",)


def test_cleanup_removes_only_old_terminal_snapshots(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _write(repo, "Main.kt", b"class Main\n")
    _commit(repo)
    now = CREATED_AT
    snapshotter = SourceSnapshotter(
        cache_root=tmp_path / "cache",
        clock=lambda: now,
        terminal_ttl=timedelta(hours=24),
    )
    old = snapshotter.create(repo, WORKSPACE_ID, created_at=now - timedelta(days=2))
    active = snapshotter.create(repo, WORKSPACE_ID, created_at=now - timedelta(days=2))
    snapshotter.mark_terminal(old.snapshot_id, terminal_at=now - timedelta(hours=25))

    snapshotter.cleanup()

    assert not old.cache_path.exists()
    assert active.cache_path.exists()


def test_snapshot_rejects_non_repository_without_disclosing_path(tmp_path: Path) -> None:
    repo = tmp_path / "private-source"
    repo.mkdir()

    with pytest.raises(SourceSnapshotError) as raised:
        SourceSnapshotter(cache_root=tmp_path / "cache").create(
            repo,
            WORKSPACE_ID,
            created_at=CREATED_AT,
        )

    assert str(repo) not in str(raised.value)
