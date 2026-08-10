from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4


MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
_ALLOWED_SUFFIXES = (".kt", ".java", ".xml", ".gradle", ".kts", ".properties")
_SENSITIVE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "google-services.json",
    "gradle.properties",
    "key.properties",
    "local.properties",
    "secrets.properties",
}
_SENSITIVE_SUFFIXES = (".jks", ".keystore", ".p12", ".pfx", ".pem", ".key")
_SENSITIVE_CONTENT = re.compile(
    rb"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|"
    rb"client[_-]?secret|password|private[_-]?key)[\"']?\s*(?::|=)\s*[\"']?"
    rb"[^\s\"']{4,}|(?:AKIA|AIza|ghp_|github_pat_)[A-Za-z0-9_\-]{12,})"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SourceSnapshotError(RuntimeError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("source snapshot operation failed")


@dataclass(frozen=True, slots=True)
class SourceExclusion:
    relative_path: str | None
    reason_code: str

    def document(self) -> dict[str, str | None]:
        return {"relative_path": self.relative_path, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    relative_path: str
    mode: str
    content: bytes
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    snapshot_id: UUID
    workspace_id: UUID
    snapshot_hash: str
    git_head: str
    git_commit: str
    tracked_dirty_count: int
    created_at: datetime
    cache_path: Path
    tree_path: Path
    files: tuple[SnapshotFile, ...]
    deleted_paths: tuple[str, ...]
    exclusions: tuple[SourceExclusion, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.relative_path for item in self.files)

    def _file(self, relative_path: str) -> SnapshotFile:
        for item in self.files:
            if item.relative_path == relative_path:
                return item
        raise KeyError(relative_path)

    def read_bytes(self, relative_path: str) -> bytes:
        return self._file(relative_path).content

    def read_text(self, relative_path: str) -> str:
        return self.read_bytes(relative_path).decode("utf-8", errors="strict")


@contextmanager
def _private_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _safe_relative_path(value: str) -> bool:
    if (
        not value
        or len(value.encode("utf-8")) > 1024
        or value.startswith("/")
        or "\\" in value
        or "//" in value
        or _CONTROL.search(value) is not None
    ):
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and all(part not in {"", ".", ".."} for part in pure.parts)


def _git_environment(*, controlled: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    if controlled:
        environment.update(controlled)
    return environment


class SourceSnapshotter:
    def __init__(
        self,
        *,
        cache_root: Path,
        uuid_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        terminal_ttl: timedelta = timedelta(hours=24),
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
        max_cache_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        if (
            not isinstance(cache_root, Path)
            or not cache_root.is_absolute()
            or max_snapshot_bytes <= 0
            or max_snapshot_bytes > MAX_SNAPSHOT_BYTES
            or max_cache_bytes <= 0
            or terminal_ttl.total_seconds() <= 0
        ):
            raise SourceSnapshotError
        self.cache_root = cache_root
        self._uuid_factory = uuid_factory
        self._clock = clock
        self._terminal_ttl = terminal_ttl
        self._max_snapshot_bytes = max_snapshot_bytes
        self._max_cache_bytes = max_cache_bytes
        self._prepare_cache_root()

    def _prepare_cache_root(self) -> None:
        try:
            with _private_umask():
                self.cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            if self.cache_root.is_symlink() or not self.cache_root.is_dir():
                raise SourceSnapshotError
            if os.name != "nt":
                self.cache_root.chmod(0o700)
                if stat.S_IMODE(self.cache_root.stat().st_mode) != 0o700:
                    raise SourceSnapshotError
        except SourceSnapshotError:
            raise
        except OSError:
            raise SourceSnapshotError from None

    def _git(
        self,
        repository: Path,
        *arguments: str,
        allowed_returncodes: tuple[int, ...] = (0,),
        controlled_environment: dict[str, str] | None = None,
        maximum_output_bytes: int = _MAX_GIT_OUTPUT_BYTES,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "core.hooksPath=/dev/null",
                    *arguments,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
                env=_git_environment(controlled=controlled_environment),
            )
        except (OSError, subprocess.SubprocessError):
            raise SourceSnapshotError from None
        if result.returncode not in allowed_returncodes or len(result.stdout) > maximum_output_bytes:
            raise SourceSnapshotError
        return result

    @staticmethod
    def _parse_index(payload: bytes) -> tuple[tuple[str, str], ...]:
        entries: list[tuple[str, str]] = []
        try:
            for raw in payload.split(b"\0"):
                if not raw:
                    continue
                metadata, encoded_path = raw.split(b"\t", 1)
                mode, _object_id, stage = metadata.decode("ascii").split(" ")
                path = encoded_path.decode("utf-8", errors="strict")
                if stage != "0" or mode not in {"100644", "100755", "120000", "160000"}:
                    raise SourceSnapshotError
                entries.append((path, mode))
        except SourceSnapshotError:
            raise
        except (UnicodeError, ValueError):
            raise SourceSnapshotError from None
        if len({path for path, _mode in entries}) != len(entries):
            raise SourceSnapshotError
        return tuple(entries)

    @staticmethod
    def _sensitive_name(relative_path: str) -> bool:
        name = PurePosixPath(relative_path).name.casefold()
        return name in _SENSITIVE_NAMES or name.endswith(_SENSITIVE_SUFFIXES)

    @staticmethod
    def _allowed_name(relative_path: str) -> bool:
        lowered = relative_path.casefold()
        return lowered.endswith(_ALLOWED_SUFFIXES) or PurePosixPath(lowered).name in {
            "build.gradle",
            "settings.gradle",
        }

    @staticmethod
    def _read_no_follow(repository: Path, relative_path: str, *, limit: int) -> bytes:
        parts = PurePosixPath(relative_path).parts
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        descriptors: list[int] = []
        try:
            if os.name == "nt" or not no_follow or not directory_flag:
                path = repository.joinpath(*parts)
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise OSError
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                descriptors.append(descriptor)
            else:
                directory = os.open(repository, os.O_RDONLY | directory_flag | no_follow)
                descriptors.append(directory)
                for part in parts[:-1]:
                    directory = os.open(
                        part,
                        os.O_RDONLY | directory_flag | no_follow,
                        dir_fd=directory,
                    )
                    descriptors.append(directory)
                descriptor = os.open(parts[-1], os.O_RDONLY | no_follow, dir_fd=directory)
                descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise OSError
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(content) > limit
                or len(content) != before.st_size
                or (before.st_dev, before.st_ino, before.st_mode, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_size)
            ):
                raise OSError
            return content
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _collect(
        self,
        repository: Path,
        entries: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[SnapshotFile, ...], tuple[str, ...], tuple[SourceExclusion, ...]]:
        files: list[SnapshotFile] = []
        deleted: list[str] = []
        exclusions: list[SourceExclusion] = []
        total_bytes = 0
        for relative_path, mode in sorted(entries):
            if not _safe_relative_path(relative_path):
                exclusions.append(SourceExclusion(None, "invalid_path"))
                continue
            if mode == "160000":
                exclusions.append(SourceExclusion(relative_path, "submodule"))
                continue
            if mode == "120000":
                exclusions.append(SourceExclusion(relative_path, "symlink"))
                continue
            path = repository.joinpath(*PurePosixPath(relative_path).parts)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                deleted.append(relative_path)
                continue
            except OSError:
                exclusions.append(SourceExclusion(relative_path, "file_unreadable"))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                exclusions.append(SourceExclusion(relative_path, "symlink"))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                exclusions.append(SourceExclusion(relative_path, "unsupported_file"))
                continue
            if self._sensitive_name(relative_path):
                exclusions.append(SourceExclusion(relative_path, "sensitive_file"))
                continue
            if not self._allowed_name(relative_path):
                exclusions.append(SourceExclusion(relative_path, "unsupported_file"))
                continue
            if metadata.st_size > self._max_snapshot_bytes:
                exclusions.append(SourceExclusion(relative_path, "file_too_large"))
                continue
            if total_bytes + metadata.st_size > self._max_snapshot_bytes:
                exclusions.append(SourceExclusion(relative_path, "snapshot_size_limit"))
                continue
            try:
                content = self._read_no_follow(
                    repository,
                    relative_path,
                    limit=self._max_snapshot_bytes - total_bytes,
                )
            except OSError:
                exclusions.append(SourceExclusion(relative_path, "file_unreadable"))
                continue
            try:
                after = path.lstat()
            except OSError:
                exclusions.append(SourceExclusion(relative_path, "file_changed"))
                continue
            if (
                len(content) != metadata.st_size
                or (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_size)
            ):
                exclusions.append(SourceExclusion(relative_path, "file_changed"))
                continue
            if b"\0" in content:
                exclusions.append(SourceExclusion(relative_path, "binary_file"))
                continue
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeError:
                exclusions.append(SourceExclusion(relative_path, "non_utf8"))
                continue
            if _SENSITIVE_CONTENT.search(content) is not None:
                exclusions.append(SourceExclusion(relative_path, "sensitive_content"))
                continue
            self._git(
                repository,
                "hash-object",
                "--no-filters",
                "--",
                relative_path,
                maximum_output_bytes=128,
            )
            digest = hashlib.sha256(content).hexdigest()
            files.append(SnapshotFile(relative_path, mode, content, digest))
            total_bytes += len(content)
        return tuple(files), tuple(sorted(deleted)), tuple(exclusions[:64])

    @staticmethod
    def _snapshot_hash(files: tuple[SnapshotFile, ...]) -> str:
        canonical = hashlib.sha256()
        for item in sorted(files, key=lambda current: current.relative_path):
            canonical.update(item.relative_path.encode("utf-8"))
            canonical.update(b"\0")
            canonical.update(item.mode.encode("ascii"))
            canonical.update(b"\0")
            canonical.update(item.content_sha256.encode("ascii"))
        return canonical.hexdigest()

    def _materialize(
        self,
        snapshot_path: Path,
        files: tuple[SnapshotFile, ...],
        *,
        created_at: datetime,
    ) -> tuple[Path, str]:
        tree = snapshot_path / "tree"
        with _private_umask():
            tree.mkdir(parents=True, mode=0o700)
            for item in files:
                destination = tree.joinpath(*PurePosixPath(item.relative_path).parts)
                destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                destination.write_bytes(item.content)
                destination.chmod(0o700 if item.mode == "100755" else 0o600)
        self._git(tree, "init", "--quiet", "--initial-branch=main")
        self._git(tree, "add", "-A")
        timestamp = created_at.astimezone(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        controlled = {
            "GIT_AUTHOR_NAME": "PerfPilot Source Snapshot",
            "GIT_AUTHOR_EMAIL": "snapshot@perfpilot.invalid",
            "GIT_COMMITTER_NAME": "PerfPilot Source Snapshot",
            "GIT_COMMITTER_EMAIL": "snapshot@perfpilot.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        self._git(
            tree,
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "PerfPilot bounded source snapshot",
            controlled_environment=controlled,
        )
        commit = self._git(tree, "rev-parse", "HEAD", maximum_output_bytes=128).stdout.decode(
            "ascii", errors="strict"
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise SourceSnapshotError
        return tree, commit

    def create(
        self,
        repository: Path,
        workspace_id: UUID,
        *,
        created_at: datetime | None = None,
    ) -> SourceSnapshot:
        if (
            not isinstance(repository, Path)
            or not repository.is_absolute()
            or not isinstance(workspace_id, UUID)
            or created_at is not None
            and (created_at.tzinfo is None or created_at.utcoffset() is None)
        ):
            raise SourceSnapshotError
        created = (created_at or self._clock()).astimezone(UTC)
        snapshot_id = self._uuid_factory()
        if not isinstance(snapshot_id, UUID) or snapshot_id.version != 4:
            raise SourceSnapshotError
        snapshot_path = self.cache_root / str(snapshot_id)
        if snapshot_path.exists():
            raise SourceSnapshotError
        try:
            root = self._git(
                repository,
                "rev-parse",
                "--show-toplevel",
                maximum_output_bytes=4096,
            ).stdout.decode("utf-8", errors="strict").rstrip("\r\n")
            if Path(root).resolve(strict=True) != repository.resolve(strict=True):
                raise SourceSnapshotError
            head = self._git(
                repository,
                "rev-parse",
                "--verify",
                "HEAD",
                maximum_output_bytes=128,
            ).stdout.decode("ascii", errors="strict").strip()
            if re.fullmatch(r"[0-9a-f]{40}", head) is None:
                raise SourceSnapshotError
            self._git(repository, "cat-file", "-e", f"{head}^{{commit}}", maximum_output_bytes=0)
            entries = self._parse_index(
                self._git(repository, "ls-files", "-s", "-z").stdout
            )
            files, deleted, exclusions = self._collect(repository, entries)
            status = self._git(
                repository,
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
            ).stdout.decode("utf-8", errors="strict")
            tracked_dirty_count = len(status.splitlines())
            with _private_umask():
                snapshot_path.mkdir(mode=0o700)
            tree, git_commit = self._materialize(snapshot_path, files, created_at=created)
            if self._directory_size(snapshot_path) > self._max_snapshot_bytes:
                raise SourceSnapshotError
            metadata = {
                "created_at": created.isoformat(),
                "snapshot_id": str(snapshot_id),
                "terminal_at": None,
                "workspace_id": str(workspace_id),
            }
            metadata_path = snapshot_path / "metadata.json"
            metadata_path.write_text(
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)
            result = SourceSnapshot(
                snapshot_id=snapshot_id,
                workspace_id=workspace_id,
                snapshot_hash=self._snapshot_hash(files),
                git_head=head,
                git_commit=git_commit,
                tracked_dirty_count=tracked_dirty_count,
                created_at=created,
                cache_path=snapshot_path,
                tree_path=tree,
                files=files,
                deleted_paths=deleted,
                exclusions=exclusions,
            )
            self.cleanup()
            if self._cache_size() > self._max_cache_bytes:
                raise SourceSnapshotError
            return result
        except SourceSnapshotError:
            if snapshot_path.exists():
                shutil.rmtree(snapshot_path, ignore_errors=True)
            raise
        except (OSError, UnicodeError, ValueError):
            if snapshot_path.exists():
                shutil.rmtree(snapshot_path, ignore_errors=True)
            raise SourceSnapshotError from None

    def _metadata(self, path: Path) -> dict[str, object] | None:
        try:
            document = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        return document

    def mark_terminal(self, snapshot_id: UUID, *, terminal_at: datetime | None = None) -> None:
        path = self.cache_root / str(snapshot_id)
        document = self._metadata(path)
        current = terminal_at or self._clock()
        if (
            document is None
            or document.get("snapshot_id") != str(snapshot_id)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise SourceSnapshotError
        document["terminal_at"] = current.astimezone(UTC).isoformat()
        metadata_path = path / "metadata.json"
        try:
            metadata_path.write_text(
                json.dumps(document, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)
        except OSError:
            raise SourceSnapshotError from None

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        try:
            for child in path.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
        except OSError:
            return MAX_CACHE_BYTES + 1
        return total

    def _cache_size(self) -> int:
        try:
            return sum(
                self._directory_size(path)
                for path in self.cache_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
        except OSError:
            raise SourceSnapshotError from None

    def cleanup(self) -> None:
        now = self._clock().astimezone(UTC)
        terminal: list[tuple[datetime, Path, int]] = []
        total = 0
        try:
            candidates = tuple(self.cache_root.iterdir())
        except OSError:
            raise SourceSnapshotError from None
        for path in candidates:
            if not path.is_dir() or path.is_symlink():
                continue
            size = self._directory_size(path)
            total += size
            document = self._metadata(path)
            if document is None or not isinstance(document.get("terminal_at"), str):
                continue
            try:
                ended = datetime.fromisoformat(document["terminal_at"]).astimezone(UTC)
            except (TypeError, ValueError):
                continue
            terminal.append((ended, path, size))
        terminal.sort(key=lambda item: (item[0], item[1].name))
        for ended, path, size in terminal:
            if now - ended < self._terminal_ttl and total <= self._max_cache_bytes:
                continue
            shutil.rmtree(path, ignore_errors=False)
            total -= size


__all__ = [
    "MAX_CACHE_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "SnapshotFile",
    "SourceExclusion",
    "SourceSnapshot",
    "SourceSnapshotError",
    "SourceSnapshotter",
]
