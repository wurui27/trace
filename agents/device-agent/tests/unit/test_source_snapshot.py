from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
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
    executable = repo / "app/src/main/java/demo/MainActivity.kt"
    executable.chmod(0o755)
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
    expected_mode = "100644" if os.name == "nt" else "100755"
    assert result._file("app/src/main/java/demo/MainActivity.kt").mode == expected_mode
    if os.name != "nt":
        assert result.tree_path.joinpath(
            "app/src/main/java/demo/MainActivity.kt"
        ).stat().st_mode & stat.S_IXUSR
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
    executable = repo / "app/src/main/java/demo/Committed.kt"
    executable.chmod(0o755)
    changed_mode = snapshotter.create(repo, WORKSPACE_ID, created_at=CREATED_AT)
    if os.name == "nt":
        assert changed_mode.snapshot_hash == first.snapshot_hash
    else:
        assert changed_mode.snapshot_hash != first.snapshot_hash
        assert changed_mode.git_commit != first.git_commit


def test_private_commit_ignores_hostile_git_config_and_preserves_crlf_blob(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    source = _write(repo, "app/src/main/java/demo/Windows.kt", b"line1\r\nline2\r\n")
    _commit(repo)
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    attributes = tmp_path / "global-attributes"
    attributes.write_text("*.kt text eol=lf\n", encoding="utf-8")
    (hostile_home / ".gitconfig").write_text(
        "[core]\n"
        "  autocrlf = true\n"
        f"  attributesFile = {attributes}\n"
        "[commit]\n"
        "  gpgsign = true\n"
        "[gpg]\n"
        "  program = /usr/bin/false\n",
        encoding="utf-8",
    )
    snapshotter = SourceSnapshotter(cache_root=tmp_path / "cache")
    baseline = snapshotter.create(repo, WORKSPACE_ID, created_at=CREATED_AT)
    monkeypatch.setenv("HOME", str(hostile_home))

    result = snapshotter.create(
        repo,
        WORKSPACE_ID,
        created_at=CREATED_AT,
    )

    assert result.read_bytes("app/src/main/java/demo/Windows.kt") == source.read_bytes()
    assert result.snapshot_hash == baseline.snapshot_hash
    assert result.git_commit == baseline.git_commit
    assert _git(
        result.tree_path,
        "cat-file",
        "-p",
        f"{result.git_commit}:app/src/main/java/demo/Windows.kt",
    ) == b"line1\r\nline2\r\n"


def test_snapshotter_rejects_cache_limits_above_hard_caps(tmp_path: Path) -> None:
    with pytest.raises(SourceSnapshotError):
        SourceSnapshotter(
            cache_root=tmp_path / "too-large",
            max_cache_bytes=2 * 1024 * 1024 * 1024 + 1,
        )
    with pytest.raises(SourceSnapshotError):
        SourceSnapshotter(
            cache_root=tmp_path / "too-old",
            terminal_ttl=timedelta(hours=24, microseconds=1),
        )


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


def test_cleaned_snapshot_id_tombstone_is_private_and_never_reused(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _write(repo, "Main.kt", b"class Main\n")
    _commit(repo)
    now = CREATED_AT
    snapshot_id = UUID("95000000-0000-4000-8000-000000000001")
    snapshotter = SourceSnapshotter(
        cache_root=tmp_path / "cache",
        uuid_factory=lambda: snapshot_id,
        clock=lambda: now,
    )
    first = snapshotter.create(repo, WORKSPACE_ID, created_at=now - timedelta(days=2))
    snapshotter.mark_terminal(first.snapshot_id, terminal_at=now - timedelta(hours=25))
    snapshotter.cleanup()
    assert not first.cache_path.exists()

    with pytest.raises(SourceSnapshotError):
        snapshotter.create(repo, WORKSPACE_ID, created_at=now)

    tombstone = snapshotter.cache_root / ".used-snapshot-ids" / str(snapshot_id)
    assert tombstone.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(tombstone.stat().st_mode) == 0o600


def test_cache_cleanup_cannot_race_in_progress_snapshot_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _write(repo, "Main.kt", b"class Main\n")
    _commit(repo)
    snapshotter = SourceSnapshotter(cache_root=tmp_path / "cache")
    started = threading.Event()
    release = threading.Event()
    original = snapshotter._materialize

    def blocking_materialize(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshotter, "_materialize", blocking_materialize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(snapshotter.create, repo, WORKSPACE_ID, created_at=CREATED_AT)
        assert started.wait(timeout=5)
        cleaning = pool.submit(snapshotter.cleanup)
        with pytest.raises(FutureTimeout):
            cleaning.result(timeout=0.1)
        release.set()
        creating.result(timeout=5)
        cleaning.result(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="O_NOFOLLOW is a POSIX boundary")
def test_cache_lock_rejects_symlink_without_mutating_its_target(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    snapshotter = SourceSnapshotter(cache_root=cache_root)
    victim = tmp_path / "victim"
    victim.write_text("unchanged\n", encoding="utf-8")
    victim.chmod(0o644)
    (cache_root / ".source-cache.lock").symlink_to(victim)

    with pytest.raises(SourceSnapshotError) as raised:
        snapshotter.cleanup()

    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert str(victim) not in str(raised.value)


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
