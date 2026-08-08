from __future__ import annotations

import errno
import json
import ntpath
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import RFC_4122, UUID, uuid4

from perfpilot_agent.source_models import SourceWorkspace, ValidationProfile

_MAXIMUM_REGISTRY_BYTES = 1024 * 1024
_MAXIMUM_WORKSPACES = 32
_MAXIMUM_PROFILES = 8
_MAXIMUM_ARGUMENTS = 64
_MAXIMUM_ARGUMENT_BYTES = 16 * 1024
_SHELL_METACHARACTERS = re.compile(r"[|&;<>\r\n\x00]")


class SourceRegistryError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("source workspace registry operation failed")


def _platform_name() -> str:
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def normalize_source_path(
    path: str | os.PathLike[str] | PurePath,
    *,
    platform_name: str | None = None,
) -> str:
    """Return a comparison key without resolving or disclosing the path."""

    current = platform_name or _platform_name()
    value = os.fspath(path)
    if current == "windows":
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.normpath(value))


def _valid_v4(identifier: object) -> bool:
    return (
        isinstance(identifier, UUID)
        and identifier.version == 4
        and identifier.variant == RFC_4122
    )


def _parse_v4(value: object) -> UUID:
    if not isinstance(value, str):
        raise SourceRegistryError
    try:
        identifier = UUID(value)
    except (ValueError, AttributeError):
        raise SourceRegistryError from None
    if not _valid_v4(identifier) or str(identifier) != value:
        raise SourceRegistryError
    return identifier


def _validate_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SourceRegistryError
    return value


class SourceWorkspaceRegistry:
    def __init__(
        self,
        workspace_root: Path,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
        replace: Callable[[Path, Path], object] | None = None,
        platform_name: str | None = None,
    ) -> None:
        if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
            raise SourceRegistryError
        self._workspace_root = workspace_root.resolve(strict=False)
        self.registry_path = self._workspace_root / "source-workspaces.json"
        self._uuid_factory = uuid_factory
        self._replace = replace or os.replace
        self._platform_name = platform_name or _platform_name()

    def __repr__(self) -> str:
        return "SourceWorkspaceRegistry()"

    def _git(
        self,
        workspace: Path,
        *arguments: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            raise SourceRegistryError from None
        if result.returncode not in allowed_returncodes:
            raise SourceRegistryError
        if len(result.stdout) > _MAXIMUM_REGISTRY_BYTES:
            raise SourceRegistryError
        return result

    def _validate_workspace_path(self, value: object) -> Path:
        try:
            path = value if isinstance(value, Path) else Path(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OSError):
            raise SourceRegistryError from None
        if not path.is_absolute():
            raise SourceRegistryError
        try:
            canonical = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise SourceRegistryError from None
        if not canonical.is_dir():
            raise SourceRegistryError
        if canonical == self._workspace_root or self._workspace_root in canonical.parents:
            raise SourceRegistryError
        root_result = self._git(canonical, "rev-parse", "--show-toplevel")
        try:
            root_text = root_result.stdout.decode("utf-8", errors="strict").rstrip("\r\n")
            git_root = Path(root_text).resolve(strict=True)
        except (UnicodeError, OSError, RuntimeError, ValueError):
            raise SourceRegistryError from None
        if not root_text or git_root != canonical:
            raise SourceRegistryError
        self._git_metadata(canonical)
        return canonical

    def _validate_working_directory(self, workspace: Path, value: object) -> Path:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 512
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise SourceRegistryError
        pure: PurePath
        if self._platform_name == "windows":
            pure = PureWindowsPath(value)
        else:
            pure = PurePosixPath(value)
        if pure.is_absolute() or pure.drive or ".." in pure.parts:
            raise SourceRegistryError
        try:
            resolved = (workspace / Path(value)).resolve(strict=True)
        except (OSError, RuntimeError):
            raise SourceRegistryError from None
        if not resolved.is_dir() or (resolved != workspace and workspace not in resolved.parents):
            raise SourceRegistryError
        return resolved

    def _validate_profile(
        self,
        workspace: Path,
        profile: object,
    ) -> ValidationProfile:
        if not isinstance(profile, ValidationProfile):
            raise SourceRegistryError
        if not _valid_v4(profile.profile_id):
            raise SourceRegistryError
        name = _validate_name(profile.name)
        argv = profile.argv
        if not isinstance(argv, tuple) or not 1 <= len(argv) <= _MAXIMUM_ARGUMENTS:
            raise SourceRegistryError
        if any(
            not isinstance(argument, str)
            or not argument
            or len(argument.encode("utf-8")) > 2_048
            or _SHELL_METACHARACTERS.search(argument) is not None
            or "$(" in argument
            or "`" in argument
            for argument in argv
        ):
            raise SourceRegistryError
        if sum(len(argument.encode("utf-8")) for argument in argv) > _MAXIMUM_ARGUMENT_BYTES:
            raise SourceRegistryError
        expected_wrapper = "gradlew.bat" if self._platform_name == "windows" else "./gradlew"
        if argv[0] != expected_wrapper:
            raise SourceRegistryError
        working_directory = self._validate_working_directory(
            workspace,
            profile.working_directory,
        )
        wrapper_name = "gradlew.bat" if self._platform_name == "windows" else "gradlew"
        try:
            wrapper = (working_directory / wrapper_name).resolve(strict=True)
        except (OSError, RuntimeError):
            raise SourceRegistryError from None
        if (
            not wrapper.is_file()
            or (wrapper != workspace and workspace not in wrapper.parents)
            or (self._platform_name != "windows" and not os.access(wrapper, os.X_OK))
        ):
            raise SourceRegistryError
        timeout = profile.timeout_seconds
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 1_200:
            raise SourceRegistryError
        exit_codes = profile.allowed_exit_codes
        if (
            not isinstance(exit_codes, tuple)
            or not exit_codes
            or len(exit_codes) > 32
            or len(set(exit_codes)) != len(exit_codes)
            or any(
                not isinstance(code, int)
                or isinstance(code, bool)
                or not 0 <= code <= 255
                for code in exit_codes
            )
        ):
            raise SourceRegistryError
        return ValidationProfile(
            profile_id=profile.profile_id,
            name=name,
            argv=argv,
            working_directory=profile.working_directory,
            timeout_seconds=timeout,
            allowed_exit_codes=exit_codes,
        )

    def _validate_profiles(
        self,
        workspace: Path,
        profiles: object,
    ) -> tuple[ValidationProfile, ...]:
        if not isinstance(profiles, tuple) or len(profiles) > _MAXIMUM_PROFILES:
            raise SourceRegistryError
        normalized = tuple(self._validate_profile(workspace, profile) for profile in profiles)
        if len({profile.profile_id for profile in normalized}) != len(normalized):
            raise SourceRegistryError
        if len({profile.name.casefold() for profile in normalized}) != len(normalized):
            raise SourceRegistryError
        return normalized

    def _validate_workspace(self, workspace: object) -> SourceWorkspace:
        if not isinstance(workspace, SourceWorkspace) or not _valid_v4(workspace.workspace_id):
            raise SourceRegistryError
        name = _validate_name(workspace.name)
        path = self._validate_workspace_path(workspace.path)
        profiles = self._validate_profiles(path, workspace.validation_profiles)
        return SourceWorkspace(workspace.workspace_id, name, path, profiles)

    def _validate_registry(
        self,
        workspaces: object,
    ) -> tuple[SourceWorkspace, ...]:
        if not isinstance(workspaces, tuple) or len(workspaces) > _MAXIMUM_WORKSPACES:
            raise SourceRegistryError
        normalized = tuple(self._validate_workspace(workspace) for workspace in workspaces)
        if len({workspace.workspace_id for workspace in normalized}) != len(normalized):
            raise SourceRegistryError
        if len({workspace.name.casefold() for workspace in normalized}) != len(normalized):
            raise SourceRegistryError
        path_keys = {
            normalize_source_path(workspace.path, platform_name=self._platform_name)
            for workspace in normalized
        }
        if len(path_keys) != len(normalized):
            raise SourceRegistryError
        profile_ids = [
            profile.profile_id
            for workspace in normalized
            for profile in workspace.validation_profiles
        ]
        if len(set(profile_ids)) != len(profile_ids):
            raise SourceRegistryError
        return normalized

    def _decode_profile(self, document: object) -> ValidationProfile:
        if not isinstance(document, dict) or set(document) != {
            "profile_id",
            "name",
            "argv",
            "working_directory",
            "timeout_seconds",
            "allowed_exit_codes",
        }:
            raise SourceRegistryError
        argv = document["argv"]
        exit_codes = document["allowed_exit_codes"]
        if not isinstance(argv, list) or not isinstance(exit_codes, list):
            raise SourceRegistryError
        return ValidationProfile(
            profile_id=_parse_v4(document["profile_id"]),
            name=document["name"],  # type: ignore[arg-type]
            argv=tuple(argv),
            working_directory=document["working_directory"],  # type: ignore[arg-type]
            timeout_seconds=document["timeout_seconds"],  # type: ignore[arg-type]
            allowed_exit_codes=tuple(exit_codes),
        )

    def _decode_workspace(self, document: object) -> SourceWorkspace:
        if not isinstance(document, dict) or set(document) != {
            "workspace_id",
            "name",
            "path",
            "validation_profiles",
        }:
            raise SourceRegistryError
        profiles = document["validation_profiles"]
        if not isinstance(profiles, list) or not isinstance(document["path"], str):
            raise SourceRegistryError
        return SourceWorkspace(
            workspace_id=_parse_v4(document["workspace_id"]),
            name=document["name"],  # type: ignore[arg-type]
            path=Path(document["path"]),
            validation_profiles=tuple(self._decode_profile(profile) for profile in profiles),
        )

    def _load(self) -> tuple[SourceWorkspace, ...]:
        if not self.registry_path.exists():
            return ()
        try:
            metadata = self.registry_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceRegistryError
            if self._platform_name != "windows" and stat.S_IMODE(metadata.st_mode) != 0o600:
                raise SourceRegistryError
            with self.registry_path.open("rb") as source:
                payload = source.read(_MAXIMUM_REGISTRY_BYTES + 1)
            if not payload or len(payload) > _MAXIMUM_REGISTRY_BYTES:
                raise SourceRegistryError
            document = json.loads(payload.decode("utf-8", errors="strict"))
            if (
                not isinstance(document, dict)
                or set(document) != {"schema_version", "workspaces"}
                or document["schema_version"] != "1.0"
                or not isinstance(document["workspaces"], list)
            ):
                raise SourceRegistryError
            return self._validate_registry(
                tuple(self._decode_workspace(item) for item in document["workspaces"])
            )
        except SourceRegistryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise SourceRegistryError from None

    @staticmethod
    def _profile_document(profile: ValidationProfile) -> dict[str, object]:
        return {
            "profile_id": str(profile.profile_id),
            "name": profile.name,
            "argv": list(profile.argv),
            "working_directory": profile.working_directory,
            "timeout_seconds": profile.timeout_seconds,
            "allowed_exit_codes": list(profile.allowed_exit_codes),
        }

    def _payload(self, workspaces: Iterable[SourceWorkspace]) -> bytes:
        document = {
            "schema_version": "1.0",
            "workspaces": [
                {
                    "workspace_id": str(workspace.workspace_id),
                    "name": workspace.name,
                    "path": str(workspace.path),
                    "validation_profiles": [
                        self._profile_document(profile)
                        for profile in workspace.validation_profiles
                    ],
                }
                for workspace in workspaces
            ],
        }
        try:
            payload = json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise SourceRegistryError from None
        if not payload or len(payload) > _MAXIMUM_REGISTRY_BYTES:
            raise SourceRegistryError
        return payload

    def _protect_windows_file(self, path: Path) -> None:
        try:
            from perfpilot_agent.platform.windows import restrict_file_to_current_user

            restrict_file_to_current_user(path)
        except (ImportError, OSError, RuntimeError):
            raise SourceRegistryError from None

    @staticmethod
    def _sync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            if error.errno not in {
                errno.EINVAL,
                errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }:
                raise

    def _save(self, workspaces: tuple[SourceWorkspace, ...]) -> None:
        normalized = self._validate_registry(workspaces)
        payload = self._payload(normalized)
        temporary: Path | None = None
        try:
            self._workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".source-workspaces-",
                suffix=".tmp",
                dir=self._workspace_root,
            )
            temporary = Path(raw_path)
            try:
                if self._platform_name != "windows":
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            if self._platform_name == "windows":
                self._protect_windows_file(temporary)
            self._replace(temporary, self.registry_path)
            temporary = None
            if self._platform_name != "windows":
                if stat.S_IMODE(self.registry_path.stat().st_mode) != 0o600:
                    raise SourceRegistryError
                self._sync_directory(self._workspace_root)
        except SourceRegistryError:
            raise
        except OSError:
            raise SourceRegistryError from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _new_uuid(self, existing: set[UUID]) -> UUID:
        identifier = self._uuid_factory()
        if not _valid_v4(identifier) or identifier in existing:
            raise SourceRegistryError
        return identifier

    @staticmethod
    def _find(
        workspaces: tuple[SourceWorkspace, ...],
        workspace_id: UUID,
    ) -> tuple[int, SourceWorkspace]:
        if not _valid_v4(workspace_id):
            raise SourceRegistryError
        for index, workspace in enumerate(workspaces):
            if workspace.workspace_id == workspace_id:
                return index, workspace
        raise SourceRegistryError

    def add(
        self,
        *,
        name: str,
        path: Path,
        validation_profiles: tuple[ValidationProfile, ...] = (),
    ) -> SourceWorkspace:
        workspaces = self._load()
        normalized_name = _validate_name(name)
        if any(workspace.name.casefold() == normalized_name.casefold() for workspace in workspaces):
            raise SourceRegistryError
        normalized_path = self._validate_workspace_path(path)
        path_key = normalize_source_path(normalized_path, platform_name=self._platform_name)
        if any(
            normalize_source_path(workspace.path, platform_name=self._platform_name) == path_key
            for workspace in workspaces
        ):
            raise SourceRegistryError
        if len(workspaces) >= _MAXIMUM_WORKSPACES:
            raise SourceRegistryError
        profiles = self._validate_profiles(normalized_path, validation_profiles)
        existing_ids = {workspace.workspace_id for workspace in workspaces}
        workspace = SourceWorkspace(
            workspace_id=self._new_uuid(existing_ids),
            name=normalized_name,
            path=normalized_path,
            validation_profiles=profiles,
        )
        self._save((*workspaces, workspace))
        return workspace

    def list(self) -> tuple[SourceWorkspace, ...]:
        return self._load()

    def remove(self, workspace_id: UUID) -> None:
        workspaces = self._load()
        index, _workspace = self._find(workspaces, workspace_id)
        self._save(workspaces[:index] + workspaces[index + 1 :])

    def add_validation(
        self,
        *,
        workspace_id: UUID,
        name: str,
        argv: tuple[str, ...],
        working_directory: str,
        timeout_seconds: int,
        allowed_exit_codes: tuple[int, ...],
    ) -> ValidationProfile:
        workspaces = self._load()
        index, workspace = self._find(workspaces, workspace_id)
        normalized_name = _validate_name(name)
        if (
            len(workspace.validation_profiles) >= _MAXIMUM_PROFILES
            or any(
                profile.name.casefold() == normalized_name.casefold()
                for profile in workspace.validation_profiles
            )
        ):
            raise SourceRegistryError
        existing_ids = {
            profile.profile_id
            for item in workspaces
            for profile in item.validation_profiles
        }
        profile = self._validate_profile(
            workspace.path,
            ValidationProfile(
                profile_id=self._new_uuid(existing_ids),
                name=normalized_name,
                argv=argv,
                working_directory=working_directory,
                timeout_seconds=timeout_seconds,
                allowed_exit_codes=allowed_exit_codes,
            ),
        )
        updated = SourceWorkspace(
            workspace.workspace_id,
            workspace.name,
            workspace.path,
            (*workspace.validation_profiles, profile),
        )
        self._save(workspaces[:index] + (updated,) + workspaces[index + 1 :])
        return profile

    def list_validation(self, workspace_id: UUID) -> tuple[ValidationProfile, ...]:
        _index, workspace = self._find(self._load(), workspace_id)
        return workspace.validation_profiles

    def remove_validation(self, workspace_id: UUID, profile_id: UUID) -> None:
        workspaces = self._load()
        index, workspace = self._find(workspaces, workspace_id)
        if not _valid_v4(profile_id):
            raise SourceRegistryError
        profiles = tuple(
            profile for profile in workspace.validation_profiles if profile.profile_id != profile_id
        )
        if len(profiles) == len(workspace.validation_profiles):
            raise SourceRegistryError
        updated = SourceWorkspace(
            workspace.workspace_id,
            workspace.name,
            workspace.path,
            profiles,
        )
        self._save(workspaces[:index] + (updated,) + workspaces[index + 1 :])

    def _git_metadata(self, workspace: Path) -> tuple[str | None, str, int]:
        head_result = self._git(workspace, "rev-parse", "--verify", "HEAD")
        try:
            head = head_result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeError:
            raise SourceRegistryError from None
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise SourceRegistryError
        branch_result = self._git(
            workspace,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            allowed_returncodes=(0, 1),
        )
        if branch_result.returncode == 0:
            try:
                branch = branch_result.stdout.decode("utf-8", errors="strict").strip()
            except UnicodeError:
                raise SourceRegistryError from None
            if (
                not 1 <= len(branch) <= 255
                or any(ord(character) < 32 or ord(character) == 127 for character in branch)
            ):
                raise SourceRegistryError
        else:
            branch = None
        status_result = self._git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        )
        try:
            status = status_result.stdout.decode("utf-8", errors="strict")
        except UnicodeError:
            raise SourceRegistryError from None
        dirty_count = len(status.splitlines())
        return branch, head, dirty_count

    def _public_workspace(self, workspace: SourceWorkspace) -> dict[str, Any]:
        branch, head, dirty_count = self._git_metadata(workspace.path)
        return {
            "workspace_id": str(workspace.workspace_id),
            "name": workspace.name,
            "state": "ready",
            "git_branch": branch,
            "git_head": head,
            "tracked_dirty_count": dirty_count,
            "snapshot_policy": "tracked_worktree",
            "validation_profiles": [
                profile.public_document() for profile in workspace.validation_profiles
            ],
        }

    def doctor(self, workspace_id: UUID) -> dict[str, Any]:
        _index, workspace = self._find(self._load(), workspace_id)
        return self._public_workspace(workspace)

    def public_workspaces(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._public_workspace(workspace) for workspace in self._load())

    add_validation_profile = add_validation
    list_validation_profiles = list_validation
    remove_validation_profile = remove_validation


__all__ = [
    "SourceRegistryError",
    "SourceWorkspaceRegistry",
    "normalize_source_path",
]
