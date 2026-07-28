from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import resource
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest


TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("20000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("30000000-0000-4000-8000-000000000001")


def _apk_bytes(*, entries: tuple[tuple[str, bytes], ...] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, contents in entries or (
            ("AndroidManifest.xml", b"binary-manifest"),
            ("classes.dex", b"dex"),
            ("lib/arm64-v8a/libperfpilot.so", b"native"),
        ):
            archive.writestr(name, contents)
    return output.getvalue()


def _checksum(contents: bytes) -> str:
    return base64.b64encode(hashlib.sha256(contents).digest()).decode("ascii")


def _manifest(
    *,
    package_name: str = "com.example.demo",
    activity_name: str = ".MainActivity",
) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{package_name}" android:versionCode="42" android:versionName="2.4.2">
  <uses-sdk android:minSdkVersion="23" android:targetSdkVersion="35" />
  <application>
    <activity android:name="{activity_name}">
      <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
      </intent-filter>
    </activity>
  </application>
</manifest>""".encode()


class FixedLocator:
    def __init__(self, contents: bytes, *, checksum: str | None = None) -> None:
        self.contents = contents
        self.checksum = checksum or _checksum(contents)
        self.received: dict[str, object] = {}

    async def locate(self, **kwargs: object) -> Any:
        from perfpilot_api.services.apk_inspection import LocatedApkArtifact

        self.received = kwargs
        return LocatedApkArtifact(
            bucket="pp-private-team-bucket",
            key="raw/private/object-key",
            version_id="immutable-version-7",
            size_bytes=len(self.contents),
            checksum_sha256_b64=self.checksum,
        )


class WritingObjectReader:
    def __init__(self, contents: bytes) -> None:
        self.contents = contents
        self.received: Any = None
        self.destination: Path | None = None

    async def download_to_file(self, *, artifact: Any, destination: Path) -> Any:
        from perfpilot_api.services.apk_inspection import DownloadedApk

        self.received = artifact
        self.destination = destination
        destination.write_bytes(self.contents)
        return DownloadedApk(
            size_bytes=len(self.contents),
            checksum_sha256_b64=_checksum(self.contents),
        )


class RecordingRunner:
    def __init__(self, stdout: bytes, *, return_code: int = 0) -> None:
        self.stdout = stdout
        self.return_code = return_code
        self.calls: list[dict[str, object]] = []
        self.apk_path: Path | None = None

    async def run(self, **kwargs: object) -> Any:
        from perfpilot_api.services.apk_inspection import CommandResult

        self.calls.append(kwargs)
        argv = kwargs["argv"]
        assert isinstance(argv, tuple)
        self.apk_path = Path(argv[-1])
        assert self.apk_path.is_file()
        return CommandResult(
            return_code=self.return_code,
            stdout=self.stdout,
            stderr=b"tool diagnostics must stay private",
        )


def _inspector(
    contents: bytes,
    *,
    manifest: bytes | None = None,
    locator_checksum: str | None = None,
    max_manifest_output_bytes: int = 256 * 1024,
) -> tuple[Any, FixedLocator, WritingObjectReader, RecordingRunner]:
    from perfpilot_api.services.apk_inspection import S3ApkInspector

    locator = FixedLocator(contents, checksum=locator_checksum)
    reader = WritingObjectReader(contents)
    runner = RecordingRunner(manifest or _manifest())
    return (
        S3ApkInspector(
            locator=locator,
            object_reader=reader,
            command_runner=runner,
            apkanalyzer_binary="/bin/echo",
            max_manifest_output_bytes=max_manifest_output_bytes,
        ),
        locator,
        reader,
        runner,
    )


@pytest.mark.asyncio
async def test_valid_apk_is_streamed_from_exact_version_and_parsed() -> None:
    contents = _apk_bytes()
    inspector, locator, reader, runner = _inspector(contents)

    metadata = await inspector.inspect(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        apk_sha256_b64=_checksum(contents),
    )

    assert locator.received == {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "artifact_id": ARTIFACT_ID,
    }
    assert reader.received.version_id == "immutable-version-7"
    assert runner.calls == [
        {
            "argv": (
                "/bin/echo",
                "manifest",
                "print",
                str(runner.apk_path),
            ),
            "timeout_seconds": 30.0,
            "output_limit_bytes": 256 * 1024,
            "working_directory": runner.apk_path.parent,
        }
    ]
    assert metadata.package_name == "com.example.demo"
    assert metadata.version_code == 42
    assert metadata.version_name == "2.4.2"
    assert metadata.min_sdk == 23
    assert metadata.target_sdk == 35
    assert metadata.launch_activity == "com.example.demo.MainActivity"
    assert metadata.supported_abis == ("arm64-v8a",)
    assert metadata.has_native_libraries is True
    assert metadata.manifest_sha256 == hashlib.sha256(b"binary-manifest").hexdigest()
    assert runner.apk_path is not None
    assert not runner.apk_path.exists()


@pytest.mark.asyncio
async def test_launcher_activity_may_be_an_external_fully_qualified_component() -> None:
    contents = _apk_bytes()
    inspector, _, _, _ = _inspector(
        contents,
        manifest=_manifest(activity_name="org.example.launcher.ExternalActivity"),
    )

    metadata = await inspector.inspect(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        apk_sha256_b64=_checksum(contents),
    )

    assert metadata.launch_activity == "org.example.launcher.ExternalActivity"


@pytest.mark.asyncio
async def test_manifest_identity_hash_uses_raw_apk_entry_not_tool_rendering() -> None:
    raw_manifest = b"stable-binary-android-manifest"
    contents = _apk_bytes(
        entries=(
            ("AndroidManifest.xml", raw_manifest),
            ("classes.dex", b"dex"),
        )
    )
    inspector, _, _, _ = _inspector(contents, manifest=_manifest())

    metadata = await inspector.inspect(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        apk_sha256_b64=_checksum(contents),
    )

    assert metadata.manifest_sha256 == hashlib.sha256(raw_manifest).hexdigest()


@pytest.mark.asyncio
async def test_checksum_mismatch_is_invalid_and_never_runs_tool() -> None:
    from perfpilot_api.services.analyses import ApkInspectionError

    contents = _apk_bytes()
    trusted_checksum = _checksum(contents)
    inspector, _, _, runner = _inspector(
        contents + b"tampered",
        locator_checksum=trusted_checksum,
    )

    with pytest.raises(ApkInspectionError) as captured:
        await inspector.inspect(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            apk_sha256_b64=trusted_checksum,
        )

    assert captured.value.code == "apk_metadata_mismatch"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "private" not in str(captured.value).lower()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_manifest_output_over_limit_is_invalid_and_redacted() -> None:
    from perfpilot_api.services.analyses import ApkInspectionError

    contents = _apk_bytes()
    inspector, _, _, runner = _inspector(
        contents,
        manifest=b"private-path-and-secret" * 20,
        max_manifest_output_bytes=64,
    )

    with pytest.raises(ApkInspectionError) as captured:
        await inspector.inspect(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            apk_sha256_b64=_checksum(contents),
        )

    assert runner.calls[0]["output_limit_bytes"] == 64
    assert captured.value.code == "apk_manifest_too_large"
    assert "private-path" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_tool_failure_is_invalid_but_tool_unavailable_is_redacted() -> None:
    from perfpilot_api.services.analyses import (
        ApkInspectionError,
        ApkInspectionUnavailableError,
    )

    contents = _apk_bytes()
    inspector, _, _, runner = _inspector(contents)
    runner.return_code = 1

    with pytest.raises(ApkInspectionError) as invalid:
        await inspector.inspect(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            apk_sha256_b64=_checksum(contents),
        )
    assert invalid.value.code == "apk_manifest_invalid"
    assert "diagnostics" not in str(invalid.value)

    class FailingRunner:
        async def run(self, **kwargs: object) -> Any:
            raise OSError("/private/sdk/path and secret")

    inspector._command_runner = FailingRunner()
    with pytest.raises(ApkInspectionUnavailableError) as unavailable:
        await inspector.inspect(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            apk_sha256_b64=_checksum(contents),
        )
    assert "private" not in str(unavailable.value).lower()
    assert unavailable.value.__cause__ is None


@pytest.mark.asyncio
async def test_zip_entry_limit_rejects_hostile_archive_before_tool_result_is_used() -> None:
    from perfpilot_api.services.analyses import ApkInspectionError
    from perfpilot_api.services.apk_inspection import S3ApkInspector

    contents = _apk_bytes(
        entries=(
            ("AndroidManifest.xml", b"manifest"),
            ("classes.dex", b"dex"),
            ("assets/a", b"a"),
        )
    )
    locator = FixedLocator(contents)
    reader = WritingObjectReader(contents)
    runner = RecordingRunner(_manifest())
    inspector = S3ApkInspector(
        locator=locator,
        object_reader=reader,
        command_runner=runner,
        apkanalyzer_binary="/bin/echo",
        max_zip_entries=2,
    )

    with pytest.raises(ApkInspectionError) as captured:
        await inspector.inspect(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            artifact_id=ARTIFACT_ID,
            apk_sha256_b64=_checksum(contents),
        )

    assert captured.value.code == "apk_archive_invalid"


class _Body:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def read(self, amount: int) -> bytes:
        assert amount <= 1024 * 1024
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


class _S3Client:
    def __init__(self, contents: bytes) -> None:
        self.contents = contents
        self.body = _Body([contents[:3], contents[3:]])
        self.received: dict[str, object] = {}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.received = kwargs
        return {
            "Body": self.body,
            "VersionId": "immutable-version-7",
            "ContentLength": len(self.contents),
            "ChecksumSHA256": _checksum(self.contents),
            "DeleteMarker": False,
        }


@pytest.mark.asyncio
async def test_s3_reader_streams_an_exact_version_to_disk(tmp_path: Path) -> None:
    from perfpilot_api.services.apk_inspection import (
        LocatedApkArtifact,
        S3VersionedObjectReader,
    )

    contents = _apk_bytes()
    client = _S3Client(contents)
    reader = S3VersionedObjectReader(client=client)
    destination = tmp_path / "input.apk"
    artifact = LocatedApkArtifact(
        bucket="pp-private-team-bucket",
        key="raw/private/object-key",
        version_id="immutable-version-7",
        size_bytes=len(contents),
        checksum_sha256_b64=_checksum(contents),
    )

    downloaded = await reader.download_to_file(artifact=artifact, destination=destination)

    assert client.received == {
        "Bucket": "pp-private-team-bucket",
        "Key": "raw/private/object-key",
        "VersionId": "immutable-version-7",
        "ChecksumMode": "ENABLED",
    }
    assert destination.read_bytes() == contents
    assert downloaded.size_bytes == len(contents)
    assert downloaded.checksum_sha256_b64 == _checksum(contents)
    assert client.body.closed is True


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode = 0
        self.killed = False
        self.pid = 987654321

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_asyncio_runner_passes_arguments_without_shell_or_concatenation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from perfpilot_api.services.apk_inspection import (
        AsyncioCommandRunner,
        CommandResourceLimits,
    )
    from perfpilot_api.services import apk_inspection

    process = _FakeProcess(b"manifest")
    received: dict[str, object] = {}

    async def create(*args: object, **kwargs: object) -> _FakeProcess:
        received["args"] = args
        received["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    injected_path = str(tmp_path / "input with spaces;$(touch never).apk")

    result = await AsyncioCommandRunner(
        environment={"JAVA_HOME": "/fixed/jdk", "PATH": "/fixed/jdk/bin:/usr/bin"},
        resource_limits=CommandResourceLimits(
            cpu_seconds=17,
            address_space_bytes=1024 * 1024 * 1024,
            file_size_bytes=8 * 1024 * 1024,
            max_processes=23,
            max_open_files=47,
        ),
    ).run(
        argv=("/bin/echo", "manifest", "print", injected_path),
        timeout_seconds=5.0,
        output_limit_bytes=1024,
        working_directory=tmp_path,
    )

    assert received["args"] == ("/bin/echo", "manifest", "print", injected_path)
    assert "shell" not in received["kwargs"]
    assert received["kwargs"]["cwd"] == str(tmp_path)
    assert received["kwargs"]["start_new_session"] is True
    assert received["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert received["kwargs"]["env"] == {
        "HOME": str(tmp_path),
        "JAVA_HOME": "/fixed/jdk",
        "JAVA_TOOL_OPTIONS": (
            "-Xms64m -Xmx1024m -XX:MaxMetaspaceSize=256m -XX:MaxDirectMemorySize=256m"
        ),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/fixed/jdk/bin:/usr/bin",
        "TMPDIR": str(tmp_path),
    }
    limiter = received["kwargs"]["preexec_fn"]
    assert callable(limiter)

    applied_limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(apk_inspection.sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (resource.RLIM_INFINITY,) * 2)
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda kind, value: applied_limits.append((kind, value)),
    )
    limiter()

    assert (resource.RLIMIT_CPU, (17, resource.RLIM_INFINITY)) in applied_limits
    assert (resource.RLIMIT_CPU, (17, 17)) in applied_limits
    assert (
        resource.RLIMIT_AS,
        (1024 * 1024 * 1024, resource.RLIM_INFINITY),
    ) in applied_limits
    assert (resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2) in applied_limits
    assert (resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2) in applied_limits
    assert (resource.RLIMIT_NPROC, (23, 23)) in applied_limits
    assert (resource.RLIMIT_NOFILE, (47, 47)) in applied_limits
    assert (resource.RLIMIT_CORE, (0, 0)) in applied_limits
    assert result.stdout == b"manifest"


def test_darwin_skips_unusable_address_space_rlimit_but_keeps_other_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from perfpilot_api.services import apk_inspection
    from perfpilot_api.services.apk_inspection import AsyncioCommandRunner

    runner = AsyncioCommandRunner()
    applied_kinds: list[int] = []
    monkeypatch.setattr(apk_inspection.sys, "platform", "darwin")
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (resource.RLIM_INFINITY,) * 2)
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda kind, value: applied_kinds.append(kind),
    )

    runner._apply_resource_limits()

    assert resource.RLIMIT_AS not in applied_kinds
    assert set(applied_kinds) == {
        resource.RLIMIT_CPU,
        resource.RLIMIT_FSIZE,
        resource.RLIMIT_NPROC,
        resource.RLIMIT_NOFILE,
        resource.RLIMIT_CORE,
    }


@pytest.mark.asyncio
async def test_asyncio_runner_kills_process_when_output_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from perfpilot_api.services.apk_inspection import (
        AsyncioCommandRunner,
        CommandOutputLimitError,
    )

    process = _FakeProcess(b"x" * 65)

    async def create(*args: object, **kwargs: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    killed_groups: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, signal: killed_groups.append((pid, signal)))

    with pytest.raises(CommandOutputLimitError):
        await AsyncioCommandRunner().run(
            argv=("/bin/echo", "manifest", "print", str(tmp_path / "input.apk")),
            timeout_seconds=5.0,
            output_limit_bytes=64,
            working_directory=tmp_path,
        )

    assert killed_groups == [(process.pid, 9)]


def test_inspector_rejects_relative_missing_or_non_executable_tool_paths(tmp_path: Path) -> None:
    from perfpilot_api.services.apk_inspection import S3ApkInspector

    contents = _apk_bytes()
    locator = FixedLocator(contents)
    reader = WritingObjectReader(contents)
    runner = RecordingRunner(_manifest())
    not_executable = tmp_path / "apkanalyzer"
    not_executable.write_text("tool")

    for invalid_path in ("apkanalyzer", str(tmp_path / "missing"), str(not_executable)):
        with pytest.raises(ValueError, match="configuration is invalid"):
            S3ApkInspector(
                locator=locator,
                object_reader=reader,
                command_runner=runner,
                apkanalyzer_binary=invalid_path,
            )


class _ScalarSession:
    def __init__(self, row: object, resource_version: int = 11) -> None:
        self.row = row
        self.info = {"tenant_resource_version": resource_version}
        self.statement: object | None = None

    async def scalar(self, statement: object) -> object:
        self.statement = statement
        return self.row


class _Router:
    def __init__(self, session: _ScalarSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self, team_id: UUID) -> Any:
        assert team_id == TEAM_ID
        yield self._session


class _BucketResolver:
    async def active_for_team(self, team_id: UUID) -> Any:
        from perfpilot_api.services.uploads import TenantBucket

        assert team_id == TEAM_ID
        return TenantBucket(team_id=TEAM_ID, bucket="pp-private-team-bucket", resource_version=11)


@pytest.mark.asyncio
async def test_sql_locator_resolves_only_server_owned_finalized_apk_metadata() -> None:
    from types import SimpleNamespace

    from perfpilot_api.services.apk_inspection import SQLAlchemyApkArtifactLocator

    contents = _apk_bytes()
    row = SimpleNamespace(
        object_key="raw/private/object-key",
        version_id="immutable-version-7",
        size_bytes=len(contents),
        sha256_b64=_checksum(contents),
    )
    session = _ScalarSession(row)
    locator = SQLAlchemyApkArtifactLocator(
        tenant_router=_Router(session),
        bucket_resolver=_BucketResolver(),
    )

    located = await locator.locate(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
    )

    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "artifacts.id =" in compiled
    assert "artifacts.analysis_id =" in compiled
    assert "artifacts.artifact_kind = 'apk'" in compiled
    assert "artifacts.idempotency_key = 'initial-apk'" in compiled
    assert "artifacts.state = 'finalized'" in compiled
    assert located.bucket == "pp-private-team-bucket"
    assert located.key == "raw/private/object-key"
    assert located.version_id == "immutable-version-7"
    assert "pp-private" not in repr(located)
