from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import re
import resource
import signal
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID
from xml.etree import ElementTree

from sqlalchemy import select

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.services.analyses import (
    ApkInspectionError,
    ApkInspectionUnavailableError,
    InspectedApkMetadata,
)
from perfpilot_api.services.uploads import BucketResolver


_APK_MIME = "application/vnd.android.package-archive"
_ANDROID_NAMESPACE = "http://schemas.android.com/apk/res/android"
_ANDROID_ATTRIBUTE = f"{{{_ANDROID_NAMESPACE}}}"
_MAIN_ACTION = "android.intent.action.MAIN"
_LAUNCHER_CATEGORY = "android.intent.category.LAUNCHER"
_PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_COMPONENT_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z")
_ABI_ORDER = ("armeabi-v7a", "arm64-v8a", "x86", "x86_64")
_SUPPORTED_ABIS = frozenset(_ABI_ORDER)
_MAX_APK_BYTES = 5 * 1024 * 1024 * 1024
_MAX_RAW_MANIFEST_BYTES = 16 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_STDERR_LIMIT_BYTES = 64 * 1024
_COMMAND_ENVIRONMENT_KEYS = frozenset({"JAVA_HOME", "LANG", "LC_ALL", "PATH"})
_JAVA_RESOURCE_OPTIONS = "-Xms64m -Xmx1024m -XX:MaxMetaspaceSize=256m -XX:MaxDirectMemorySize=256m"


@dataclass(frozen=True, slots=True)
class LocatedApkArtifact:
    bucket: str = field(repr=False)
    key: str = field(repr=False)
    version_id: str = field(repr=False)
    size_bytes: int
    checksum_sha256_b64: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DownloadedApk:
    size_bytes: int
    checksum_sha256_b64: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommandResult:
    return_code: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommandResourceLimits:
    cpu_seconds: int = 60
    address_space_bytes: int = 4 * 1024 * 1024 * 1024
    file_size_bytes: int = 32 * 1024 * 1024
    max_processes: int = 64
    max_open_files: int = 128

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (
                self.cpu_seconds,
                self.address_space_bytes,
                self.file_size_bytes,
                self.max_processes,
                self.max_open_files,
            )
        ):
            raise ValueError("command resource limits are invalid")


class ApkArtifactLocator(Protocol):
    async def locate(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> LocatedApkArtifact: ...


class VersionedObjectReader(Protocol):
    async def download_to_file(
        self,
        *,
        artifact: LocatedApkArtifact,
        destination: Path,
    ) -> DownloadedApk: ...


class CommandRunner(Protocol):
    async def run(
        self,
        *,
        argv: tuple[str, ...],
        timeout_seconds: float,
        output_limit_bytes: int,
        working_directory: Path,
    ) -> CommandResult: ...


class CommandOutputLimitError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("command output exceeded its limit")


class CommandTimeoutError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("command timed out")


class CommandUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("command is unavailable")


class _ObjectContentMismatchError(RuntimeError):
    pass


class _ObjectReadUnavailableError(RuntimeError):
    pass


def _canonical_sha256_b64(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 32:
        return None
    canonical = base64.b64encode(decoded).decode("ascii")
    return canonical if hmac.compare_digest(canonical, value) else None


def _safe_nonempty_text(value: object, *, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


class SQLAlchemyApkArtifactLocator:
    """Resolve an immutable initial APK without accepting storage coordinates from callers."""

    def __init__(
        self,
        *,
        tenant_router: TenantRouter,
        bucket_resolver: BucketResolver,
    ) -> None:
        self._tenant_router = tenant_router
        self._bucket_resolver = bucket_resolver

    async def locate(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> LocatedApkArtifact:
        if not all(
            isinstance(identifier, UUID) for identifier in (team_id, analysis_id, artifact_id)
        ):
            raise ApkInspectionUnavailableError("APK inspection is unavailable")
        try:
            tenant = await self._bucket_resolver.active_for_team(team_id)
            async with self._tenant_router.session(team_id) as session:
                if session.info.get("tenant_resource_version") != tenant.resource_version:
                    raise ApkInspectionUnavailableError("APK inspection is unavailable")
                row = await session.scalar(
                    select(Artifact)
                    .join(Analysis, Analysis.id == Artifact.analysis_id)
                    .where(
                        Artifact.id == artifact_id,
                        Artifact.analysis_id == analysis_id,
                        Artifact.artifact_kind == "apk",
                        Artifact.mime_type == _APK_MIME,
                        Artifact.idempotency_key == "initial-apk",
                        Artifact.state == "finalized",
                        Artifact.version_id.is_not(None),
                        Artifact.deleted_at.is_(None),
                        Analysis.id == analysis_id,
                        Analysis.tombstoned_at.is_(None),
                        Analysis.state != "deleted",
                    )
                )
        except ApkInspectionUnavailableError:
            raise
        except Exception:
            raise ApkInspectionUnavailableError("APK inspection is unavailable") from None

        bucket = _safe_nonempty_text(getattr(tenant, "bucket", None), maximum=255)
        key = _safe_nonempty_text(getattr(row, "object_key", None), maximum=1024)
        version_id = _safe_nonempty_text(getattr(row, "version_id", None), maximum=1024)
        checksum = _canonical_sha256_b64(getattr(row, "sha256_b64", None))
        size = getattr(row, "size_bytes", None)
        if (
            row is None
            or bucket is None
            or key is None
            or version_id is None
            or version_id == "null"
            or checksum is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= _MAX_APK_BYTES
        ):
            raise ApkInspectionUnavailableError("APK inspection is unavailable")
        return LocatedApkArtifact(
            bucket=bucket,
            key=key,
            version_id=version_id,
            size_bytes=size,
            checksum_sha256_b64=checksum,
        )


class S3VersionedObjectReader:
    """Stream one exact S3 object version to disk while calculating trusted metadata."""

    def __init__(self, *, client: Any) -> None:
        self._client = client

    async def download_to_file(
        self,
        *,
        artifact: LocatedApkArtifact,
        destination: Path,
    ) -> DownloadedApk:
        try:
            return await asyncio.to_thread(self._download_sync, artifact, destination)
        except (_ObjectContentMismatchError, _ObjectReadUnavailableError):
            raise
        except Exception:
            raise _ObjectReadUnavailableError from None

    def _download_sync(
        self,
        artifact: LocatedApkArtifact,
        destination: Path,
    ) -> DownloadedApk:
        try:
            response = self._client.get_object(
                Bucket=artifact.bucket,
                Key=artifact.key,
                VersionId=artifact.version_id,
                ChecksumMode="ENABLED",
            )
        except Exception:
            raise _ObjectReadUnavailableError from None
        if not isinstance(response, Mapping):
            raise _ObjectReadUnavailableError

        body = response.get("Body")
        content_length = response.get("ContentLength")
        returned_version = response.get("VersionId")
        returned_checksum = response.get("ChecksumSHA256")
        if not callable(getattr(body, "read", None)):
            raise _ObjectReadUnavailableError
        if (
            returned_version != artifact.version_id
            or not isinstance(content_length, int)
            or isinstance(content_length, bool)
            or content_length != artifact.size_bytes
            or response.get("DeleteMarker", False) is not False
            or returned_checksum is not None
            and (
                _canonical_sha256_b64(returned_checksum) is None
                or not hmac.compare_digest(returned_checksum, artifact.checksum_sha256_b64)
            )
        ):
            self._close_body(body)
            raise _ObjectContentMismatchError

        digest = hashlib.sha256()
        size = 0
        try:
            with destination.open("xb") as output:
                while True:
                    chunk = body.read(_DOWNLOAD_CHUNK_BYTES)
                    if not isinstance(chunk, bytes):
                        raise _ObjectReadUnavailableError
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > artifact.size_bytes:
                        raise _ObjectContentMismatchError
                    digest.update(chunk)
                    output.write(chunk)
        except (_ObjectContentMismatchError, _ObjectReadUnavailableError):
            raise
        except Exception:
            raise _ObjectReadUnavailableError from None
        finally:
            self._close_body(body)

        checksum = base64.b64encode(digest.digest()).decode("ascii")
        if size != artifact.size_bytes or not hmac.compare_digest(
            checksum,
            artifact.checksum_sha256_b64,
        ):
            raise _ObjectContentMismatchError
        return DownloadedApk(size_bytes=size, checksum_sha256_b64=checksum)

    @staticmethod
    def _close_body(body: object) -> None:
        close = getattr(body, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            pass


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(min(64 * 1024, limit + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise CommandOutputLimitError


class AsyncioCommandRunner:
    """Run a command with separated arguments and bounded stdout/stderr capture."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        resource_limits: CommandResourceLimits = CommandResourceLimits(),
    ) -> None:
        supplied_environment = environment or {}
        if (
            not isinstance(supplied_environment, Mapping)
            or not set(supplied_environment) <= _COMMAND_ENVIRONMENT_KEYS
            or any(
                _safe_nonempty_text(key, maximum=64) is None
                or _safe_nonempty_text(value, maximum=4096) is None
                for key, value in supplied_environment.items()
            )
            or not isinstance(resource_limits, CommandResourceLimits)
        ):
            raise ValueError("command runner configuration is invalid")
        base_environment = {
            "JAVA_TOOL_OPTIONS": _JAVA_RESOURCE_OPTIONS,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        base_environment.update(supplied_environment)
        self._environment = base_environment
        self._resource_limits = resource_limits

    async def run(
        self,
        *,
        argv: tuple[str, ...],
        timeout_seconds: float,
        output_limit_bytes: int,
        working_directory: Path,
    ) -> CommandResult:
        executable = Path(argv[0]) if argv else None
        if (
            not argv
            or any(_safe_nonempty_text(argument, maximum=4096) is None for argument in argv)
            or executable is None
            or not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or not isinstance(output_limit_bytes, int)
            or isinstance(output_limit_bytes, bool)
            or output_limit_bytes < 1
            or not isinstance(working_directory, Path)
            or not working_directory.is_absolute()
            or not working_directory.is_dir()
        ):
            raise CommandUnavailableError
        command_environment = {
            **self._environment,
            "HOME": str(working_directory),
            "TMPDIR": str(working_directory),
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(working_directory),
                env=command_environment,
                start_new_session=True,
                preexec_fn=self._apply_resource_limits,
            )
        except Exception:
            raise CommandUnavailableError from None
        if process.stdout is None or process.stderr is None:
            self._terminate_process_group(process)
            await process.wait()
            raise CommandUnavailableError

        readers = (
            asyncio.create_task(_read_bounded(process.stdout, output_limit_bytes)),
            asyncio.create_task(
                _read_bounded(process.stderr, min(output_limit_bytes, _STDERR_LIMIT_BYTES))
            ),
        )
        try:
            async with asyncio.timeout(float(timeout_seconds)):
                stdout, stderr = await asyncio.gather(*readers)
                return_code = await process.wait()
                if len(stdout) + len(stderr) > output_limit_bytes:
                    raise CommandOutputLimitError
        except CommandOutputLimitError:
            self._terminate_process_group(process)
            await self._finish_failed_process(process, readers)
            raise
        except TimeoutError:
            self._terminate_process_group(process)
            await self._finish_failed_process(process, readers)
            raise CommandTimeoutError from None
        except asyncio.CancelledError:
            self._terminate_process_group(process)
            await self._finish_failed_process(process, readers)
            raise
        except Exception:
            self._terminate_process_group(process)
            await self._finish_failed_process(process, readers)
            raise CommandUnavailableError from None
        return CommandResult(return_code=return_code, stdout=stdout, stderr=stderr)

    def _apply_resource_limits(self) -> None:
        limits = self._resource_limits
        self._set_resource_limit(resource.RLIMIT_CPU, limits.cpu_seconds)
        if sys.platform != "darwin":
            self._set_resource_limit(resource.RLIMIT_AS, limits.address_space_bytes)
        self._set_resource_limit(resource.RLIMIT_FSIZE, limits.file_size_bytes)
        self._set_resource_limit(resource.RLIMIT_NPROC, limits.max_processes)
        self._set_resource_limit(resource.RLIMIT_NOFILE, limits.max_open_files)
        self._set_resource_limit(resource.RLIMIT_CORE, 0)

    @staticmethod
    def _set_resource_limit(kind: int, requested: int) -> None:
        _, hard_limit = resource.getrlimit(kind)
        applied = requested if hard_limit == resource.RLIM_INFINITY else min(requested, hard_limit)
        # Darwin rejects lowering an infinite soft and hard limit in one operation.
        resource.setrlimit(kind, (applied, hard_limit))
        resource.setrlimit(kind, (applied, applied))

    @staticmethod
    def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
        process_id = getattr(process, "pid", None)
        if isinstance(process_id, int) and not isinstance(process_id, bool) and process_id > 0:
            try:
                os.killpg(process_id, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        try:
            process.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _finish_failed_process(
        process: asyncio.subprocess.Process,
        readers: tuple[asyncio.Task[bytes], asyncio.Task[bytes]],
    ) -> None:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (Exception, asyncio.CancelledError):
            pass


def _invalid(code: str) -> ApkInspectionError:
    return ApkInspectionError("APK inspection failed", code=code)


def _archive_metadata(
    apk_path: Path,
    max_entries: int,
) -> tuple[tuple[str, ...], bool, str]:
    try:
        with zipfile.ZipFile(apk_path) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= max_entries:
                raise _invalid("apk_archive_invalid")
            names: set[str] = set()
            abis: set[str] = set()
            manifest_entry: zipfile.ZipInfo | None = None
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (
                    not name
                    or len(name) > 4096
                    or "\\" in name
                    or path.is_absolute()
                    or ".." in path.parts
                    or name in names
                ):
                    raise _invalid("apk_archive_invalid")
                names.add(name)
                if name == "AndroidManifest.xml":
                    if (
                        entry.is_dir()
                        or entry.flag_bits & 0x1
                        or not 1 <= entry.file_size <= _MAX_RAW_MANIFEST_BYTES
                    ):
                        raise _invalid("apk_archive_invalid")
                    manifest_entry = entry
                if len(path.parts) >= 3 and path.parts[0] == "lib" and name.endswith(".so"):
                    abi = path.parts[1]
                    if abi not in _SUPPORTED_ABIS:
                        raise _invalid("apk_archive_invalid")
                    abis.add(abi)
            if manifest_entry is None:
                raise _invalid("apk_archive_invalid")
            manifest_digest = hashlib.sha256()
            manifest_size = 0
            with archive.open(manifest_entry, "r") as manifest:
                while True:
                    chunk = manifest.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    manifest_size += len(chunk)
                    if manifest_size > _MAX_RAW_MANIFEST_BYTES:
                        raise _invalid("apk_archive_invalid")
                    manifest_digest.update(chunk)
            if manifest_size != manifest_entry.file_size:
                raise _invalid("apk_archive_invalid")
    except ApkInspectionError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error):
        raise _invalid("apk_archive_invalid") from None
    ordered = tuple(abi for abi in _ABI_ORDER if abi in abis)
    return ordered, bool(ordered), manifest_digest.hexdigest()


def _android_attribute(element: ElementTree.Element, name: str) -> str | None:
    value = element.get(f"{_ANDROID_ATTRIBUTE}{name}")
    return value if value is not None else element.get(f"android:{name}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _required_nonnegative_int(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise _invalid("apk_manifest_invalid")
    result = int(value)
    if result < 0 or result > 2**63 - 1:
        raise _invalid("apk_manifest_invalid")
    return result


def _optional_sdk_int(value: str | None) -> int | None:
    if value is None:
        return None
    result = _required_nonnegative_int(value)
    if result < 1 or result > 10000:
        raise _invalid("apk_manifest_invalid")
    return result


def _normalize_component(package_name: str, value: str) -> str:
    if value.startswith("."):
        normalized = f"{package_name}{value}"
    elif "." not in value:
        normalized = f"{package_name}.{value}"
    else:
        normalized = value
    if len(normalized) > 512 or _COMPONENT_NAME.fullmatch(normalized) is None:
        raise _invalid("apk_manifest_invalid")
    return normalized


def _launcher_activity(root: ElementTree.Element, package_name: str) -> str | None:
    application = next(
        (element for element in root if _local_name(element.tag) == "application"),
        None,
    )
    if application is None:
        return None
    for component in application:
        if _local_name(component.tag) not in ("activity", "activity-alias"):
            continue
        for intent_filter in component:
            if _local_name(intent_filter.tag) != "intent-filter":
                continue
            actions = {
                _android_attribute(child, "name")
                for child in intent_filter
                if _local_name(child.tag) == "action"
            }
            categories = {
                _android_attribute(child, "name")
                for child in intent_filter
                if _local_name(child.tag) == "category"
            }
            if _MAIN_ACTION in actions and _LAUNCHER_CATEGORY in categories:
                value = _android_attribute(component, "name")
                if value is None:
                    raise _invalid("apk_manifest_invalid")
                return _normalize_component(package_name, value)
    return None


def _parse_manifest(manifest: bytes) -> InspectedApkMetadata:
    lowered = manifest.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _invalid("apk_manifest_invalid")
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        raise _invalid("apk_manifest_invalid") from None
    if _local_name(root.tag) != "manifest":
        raise _invalid("apk_manifest_invalid")
    package_name = root.get("package")
    if package_name is None or _PACKAGE_NAME.fullmatch(package_name) is None:
        raise _invalid("apk_manifest_invalid")
    version_code = _required_nonnegative_int(_android_attribute(root, "versionCode"))
    version_name = _android_attribute(root, "versionName")
    if version_name == "":
        version_name = None
    if version_name is not None and len(version_name) > 255:
        raise _invalid("apk_manifest_invalid")
    uses_sdk = next((element for element in root if _local_name(element.tag) == "uses-sdk"), None)
    min_sdk = _optional_sdk_int(
        None if uses_sdk is None else _android_attribute(uses_sdk, "minSdkVersion")
    )
    target_sdk = _optional_sdk_int(
        None if uses_sdk is None else _android_attribute(uses_sdk, "targetSdkVersion")
    )
    if min_sdk is not None and target_sdk is not None and min_sdk > target_sdk:
        raise _invalid("apk_manifest_invalid")
    return InspectedApkMetadata(
        package_name=package_name,
        version_name=version_name,
        version_code=version_code,
        launch_activity=_launcher_activity(root, package_name),
        min_sdk=min_sdk,
        target_sdk=target_sdk,
        supported_abis=(),
        has_native_libraries=False,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    )


class S3ApkInspector:
    """Inspect an immutable APK using bounded disk, ZIP and subprocess operations."""

    def __init__(
        self,
        *,
        locator: ApkArtifactLocator,
        object_reader: VersionedObjectReader,
        apkanalyzer_binary: str,
        command_runner: CommandRunner | None = None,
        manifest_timeout_seconds: float = 30.0,
        max_manifest_output_bytes: int = 256 * 1024,
        max_zip_entries: int = 100_000,
    ) -> None:
        analyzer_path = Path(apkanalyzer_binary)
        if (
            _safe_nonempty_text(apkanalyzer_binary, maximum=4096) is None
            or not analyzer_path.is_absolute()
            or not analyzer_path.is_file()
            or not os.access(analyzer_path, os.X_OK)
            or not isinstance(manifest_timeout_seconds, (int, float))
            or isinstance(manifest_timeout_seconds, bool)
            or not 0 < manifest_timeout_seconds <= 120
            or not isinstance(max_manifest_output_bytes, int)
            or isinstance(max_manifest_output_bytes, bool)
            or not 1 <= max_manifest_output_bytes <= 8 * 1024 * 1024
            or not isinstance(max_zip_entries, int)
            or isinstance(max_zip_entries, bool)
            or not 1 <= max_zip_entries <= 1_000_000
        ):
            raise ValueError("APK inspector configuration is invalid")
        self._locator = locator
        self._object_reader = object_reader
        self._command_runner = command_runner or AsyncioCommandRunner()
        self._apkanalyzer_binary = apkanalyzer_binary
        self._manifest_timeout_seconds = float(manifest_timeout_seconds)
        self._max_manifest_output_bytes = max_manifest_output_bytes
        self._max_zip_entries = max_zip_entries

    async def inspect(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        apk_sha256_b64: str,
    ) -> InspectedApkMetadata:
        expected_checksum = _canonical_sha256_b64(apk_sha256_b64)
        if expected_checksum is None:
            raise _invalid("apk_metadata_mismatch")
        try:
            artifact = await self._locator.locate(
                team_id=team_id,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )
        except ApkInspectionUnavailableError:
            raise
        except Exception:
            raise ApkInspectionUnavailableError("APK inspection is unavailable") from None
        if not hmac.compare_digest(artifact.checksum_sha256_b64, expected_checksum):
            raise _invalid("apk_metadata_mismatch")

        try:
            with tempfile.TemporaryDirectory(prefix="perfpilot-apk-") as temporary_directory:
                apk_path = Path(temporary_directory) / "input.apk"
                try:
                    downloaded = await self._object_reader.download_to_file(
                        artifact=artifact,
                        destination=apk_path,
                    )
                except _ObjectContentMismatchError:
                    raise _invalid("apk_metadata_mismatch") from None
                except ApkInspectionError:
                    raise
                except Exception:
                    raise ApkInspectionUnavailableError("APK inspection is unavailable") from None
                if downloaded.size_bytes != artifact.size_bytes or not hmac.compare_digest(
                    downloaded.checksum_sha256_b64,
                    artifact.checksum_sha256_b64,
                ):
                    raise _invalid("apk_metadata_mismatch")

                supported_abis, has_native_libraries, manifest_sha256 = await asyncio.to_thread(
                    _archive_metadata,
                    apk_path,
                    self._max_zip_entries,
                )
                try:
                    result = await self._command_runner.run(
                        argv=(
                            self._apkanalyzer_binary,
                            "manifest",
                            "print",
                            str(apk_path),
                        ),
                        timeout_seconds=self._manifest_timeout_seconds,
                        output_limit_bytes=self._max_manifest_output_bytes,
                        working_directory=apk_path.parent,
                    )
                except CommandOutputLimitError:
                    raise _invalid("apk_manifest_too_large") from None
                except ApkInspectionError:
                    raise
                except Exception:
                    raise ApkInspectionUnavailableError("APK inspection is unavailable") from None
                if len(result.stdout) > self._max_manifest_output_bytes:
                    raise _invalid("apk_manifest_too_large")
                if result.return_code != 0 or not result.stdout:
                    raise _invalid("apk_manifest_invalid")
                metadata = _parse_manifest(result.stdout)
                return InspectedApkMetadata(
                    package_name=metadata.package_name,
                    version_name=metadata.version_name,
                    version_code=metadata.version_code,
                    launch_activity=metadata.launch_activity,
                    min_sdk=metadata.min_sdk,
                    target_sdk=metadata.target_sdk,
                    supported_abis=supported_abis,
                    has_native_libraries=has_native_libraries,
                    manifest_sha256=manifest_sha256,
                )
        except (ApkInspectionError, ApkInspectionUnavailableError):
            raise
        except Exception:
            raise ApkInspectionUnavailableError("APK inspection is unavailable") from None


__all__ = [
    "ApkArtifactLocator",
    "AsyncioCommandRunner",
    "CommandOutputLimitError",
    "CommandResourceLimits",
    "CommandResult",
    "CommandRunner",
    "DownloadedApk",
    "LocatedApkArtifact",
    "S3ApkInspector",
    "S3VersionedObjectReader",
    "SQLAlchemyApkArtifactLocator",
    "VersionedObjectReader",
]
