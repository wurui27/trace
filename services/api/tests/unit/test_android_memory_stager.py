from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import shutil
import tarfile
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

import perfpilot_api.engines.android_memory_stager as stager_module
from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.engines.android_memory_stager import (
    AndroidMemoryStagingError,
    AndroidMemoryStager,
    StagedMemoryInput,
)
from perfpilot_api.engines.contracts import EngineInput
from perfpilot_api.engines.errors import EngineAdapterError


ANALYSIS_ID = UUID("a1000000-0000-4000-8000-000000000001")
CAPTURE_ID = UUID("a2000000-0000-4000-8000-000000000001")
MANIFEST_ID = UUID("a3000000-0000-4000-8000-000000000001")
MEMINFO_ID = UUID("a4000000-0000-4000-8000-000000000001")
LOG_ID = UUID("a4000000-0000-4000-8000-000000000002")
EXTRA_ID = UUID("a4000000-0000-4000-8000-000000000003")
MEMINFO_BYTES = b"Applications Memory Usage (in Kilobytes):\nTOTAL 12345\n"
LOG_BYTES = b"07-30 08:00:00.000 I/Memory: capture complete\n"
PRIVATE_FAILURE_MARKER = "private-failure-marker"


def _hash(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _manifest_bytes(
    references: Iterable[tuple[UUID, str]],
    *,
    phase: str = "single",
) -> bytes:
    return MemoryCaptureManifest.model_validate(
        {
            "schema_version": "1.0",
            "analysis_id": ANALYSIS_ID,
            "capture_id": CAPTURE_ID,
            "phase": phase,
            "source": "manual_upload",
            "captured_at": None,
            "subject": {
                "package": "com.private.package.marker",
                "android_sdk": 37,
            },
            "artifacts": [
                {"artifact_id": artifact_id, "role": role} for artifact_id, role in references
            ],
        }
    ).canonical_bytes()


def _input(
    artifact_id: UUID,
    *,
    kind: str,
    payload: bytes,
    path: str,
    mime: str = "application/octet-stream",
    size_bytes: int | None = None,
    sha256_b64: str | None = None,
) -> EngineInput:
    return EngineInput(
        artifact_id=artifact_id,
        kind=kind,
        mime=mime,
        size_bytes=len(payload) if size_bytes is None else size_bytes,
        sha256_b64=_hash(payload) if sha256_b64 is None else sha256_b64,
        download_url=SecretStr(
            f"https://objects.example/private/{path}?signature=secret-query-marker"
        ),
    )


def _manifest_input(
    payload: bytes,
    *,
    artifact_id: UUID = MANIFEST_ID,
    size_bytes: int | None = None,
    sha256_b64: str | None = None,
) -> EngineInput:
    return _input(
        artifact_id,
        kind="memory_capture_manifest",
        payload=payload,
        path="manifest.json",
        mime="application/json",
        size_bytes=size_bytes,
        sha256_b64=sha256_b64,
    )


def _meminfo_input(
    *,
    artifact_id: UUID = MEMINFO_ID,
    kind: str = "memory_evidence",
    payload: bytes = MEMINFO_BYTES,
    size_bytes: int | None = None,
    sha256_b64: str | None = None,
    path: str = "attacker-name/original-meminfo-secret.txt",
) -> EngineInput:
    return _input(
        artifact_id,
        kind=kind,
        payload=payload,
        path=path,
        mime="text/plain",
        size_bytes=size_bytes,
        sha256_b64=sha256_b64,
    )


def _tar_bytes(
    files: dict[str, bytes],
    *,
    symbolic_link: tuple[str, str] | None = None,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(payload))
        if symbolic_link is not None:
            name, target = symbolic_link
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            archive.addfile(member)
    return output.getvalue()


def _handoff_input(payload: bytes) -> EngineInput:
    return _input(
        MEMINFO_ID,
        kind="memory_evidence",
        payload=payload,
        path="agent-memory-evidence.tar",
        mime="application/x-tar",
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def _stager(
    client: httpx.AsyncClient,
    workspace_root: Path,
    *,
    max_files: int = 2048,
    max_file_bytes: int = 2 * 1024 * 1024,
    max_total_bytes: int = 4 * 1024 * 1024,
) -> AndroidMemoryStager:
    return AndroidMemoryStager(
        client=client,
        workspace_root=workspace_root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def _responses(
    payloads: dict[str, bytes],
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, content=payloads[request.url.path])

    return handler


async def _stage_error(
    stager: AndroidMemoryStager,
    inputs: Iterable[EngineInput],
    *,
    stable_code: str,
    run_id: str = "memory-run-error",
) -> EngineAdapterError:
    with pytest.raises(EngineAdapterError) as caught:
        await stager.stage(run_id=run_id, inputs=inputs)

    error = caught.value
    assert isinstance(error, AndroidMemoryStagingError)
    assert error.stable_code == stable_code
    assert "objects.example" not in str(error)
    assert "secret-query-marker" not in repr(error)
    assert PRIVATE_FAILURE_MARKER not in str(error)
    assert PRIVATE_FAILURE_MARKER not in repr(error)
    assert str(stager._workspace_root) not in str(error)  # type: ignore[attr-defined]
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


@pytest.mark.asyncio
async def test_stager_downloads_manifest_first_and_materializes_only_references(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(
        ((MEMINFO_ID, "meminfo"), (LOG_ID, "android_log")),
        phase="single",
    )
    manifest = _manifest_input(manifest_payload)
    meminfo = _meminfo_input()
    android_log = _input(
        LOG_ID,
        kind="log",
        payload=LOG_BYTES,
        path="original-device-filename.log",
        mime="text/plain",
    )
    requests: list[httpx.Request] = []
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
                "/private/original-device-filename.log": LOG_BYTES,
            },
            requests,
        )
    )

    try:
        staged = await _stager(client, tmp_path).stage(
            run_id="memory-run-success",
            inputs=(android_log, meminfo, manifest),
        )
        files = sorted(
            path.relative_to(staged.input_dir).as_posix()
            for path in staged.input_dir.rglob("*")
            if path.is_file()
        )

        assert [request.url.path for request in requests] == [
            "/private/manifest.json",
            "/private/attacker-name/original-meminfo-secret.txt",
            "/private/original-device-filename.log",
        ]
        assert all(request.method == "GET" for request in requests)
        assert files == [
            f"logs/android-log-{LOG_ID}.txt",
            f"meminfo/meminfo-{MEMINFO_ID}.txt",
        ]
        assert (staged.input_dir / files[0]).read_bytes() == LOG_BYTES
        assert (staged.input_dir / files[1]).read_bytes() == MEMINFO_BYTES
        assert staged.manifest.phase == "single"
        assert staged.input_dir.resolve().is_relative_to(tmp_path.resolve())
        assert "memory-run-success" not in "/".join(files)
        assert "com.private.package.marker" not in "/".join(files)
        assert client.is_closed is False

        with pytest.raises(FrozenInstanceError):
            staged.input_dir = tmp_path  # type: ignore[misc]
        await staged.cleanup()
        assert not (tmp_path / "memory-run-success").exists()
        assert client.is_closed is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stager_preserves_and_safely_expands_agent_handoff_archive(
    tmp_path: Path,
) -> None:
    archive_payload = _tar_bytes(
        {
            "meminfo/meminfo-000.txt": MEMINFO_BYTES,
            "metadata.json": b'{"schema_version":"1.0"}',
            "summary.json": b'{"delta_pss_kb":20}',
        }
    )
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "handoff_archive"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/agent-memory-evidence.tar": archive_payload,
            }
        )
    )

    try:
        staged = await _stager(client, tmp_path).stage(
            run_id="agent-handoff-success",
            inputs=(_manifest_input(manifest_payload), _handoff_input(archive_payload)),
        )
        files = sorted(
            path.relative_to(staged.input_dir).as_posix()
            for path in staged.input_dir.rglob("*")
            if path.is_file()
        )

        assert files == [
            f"archives/handoff-{MEMINFO_ID}.tar",
            f"handoff/{MEMINFO_ID}/meminfo/meminfo-000.txt",
            f"handoff/{MEMINFO_ID}/metadata.json",
            f"handoff/{MEMINFO_ID}/summary.json",
        ]
        assert (
            staged.input_dir / f"handoff/{MEMINFO_ID}/meminfo/meminfo-000.txt"
        ).read_bytes() == MEMINFO_BYTES
        assert (
            staged.input_dir / f"archives/handoff-{MEMINFO_ID}.tar"
        ).read_bytes() == archive_payload
        await staged.cleanup()
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "archive_payload",
    [
        _tar_bytes({"../private-marker.txt": b"outside"}),
        _tar_bytes(
            {"meminfo/meminfo-000.txt": MEMINFO_BYTES},
            symbolic_link=("meminfo/latest.txt", "meminfo-000.txt"),
        ),
        _tar_bytes({"/absolute/private-marker.txt": b"outside"}),
    ],
)
@pytest.mark.asyncio
async def test_stager_rejects_unsafe_agent_handoff_members(
    tmp_path: Path,
    archive_payload: bytes,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "handoff_archive"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/agent-memory-evidence.tar": archive_payload,
            }
        )
    )

    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _handoff_input(archive_payload)),
            stable_code="manifest_invalid",
            run_id="unsafe-agent-handoff",
        )
    finally:
        await client.aclose()

    assert not (tmp_path / "unsafe-agent-handoff").exists()


@pytest.mark.asyncio
async def test_stager_counts_expanded_handoff_members_against_file_limit(
    tmp_path: Path,
) -> None:
    archive_payload = _tar_bytes(
        {
            "meminfo/meminfo-000.txt": MEMINFO_BYTES,
            "metadata.json": b'{"schema_version":"1.0"}',
        }
    )
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "handoff_archive"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/agent-memory-evidence.tar": archive_payload,
            }
        )
    )

    try:
        await _stage_error(
            _stager(client, tmp_path, max_files=2),
            (_manifest_input(manifest_payload), _handoff_input(archive_payload)),
            stable_code="input_limit_exceeded",
            run_id="oversized-agent-handoff",
        )
    finally:
        await client.aclose()

    assert not (tmp_path / "oversized-agent-handoff").exists()


@pytest.mark.parametrize("target", ["manifest", "evidence"])
@pytest.mark.parametrize(
    ("failure", "stable_code"),
    [
        ("short", "integrity_mismatch"),
        ("hash", "integrity_mismatch"),
        ("base64", "integrity_mismatch"),
        ("overflow", "input_limit_exceeded"),
    ],
)
@pytest.mark.asyncio
async def test_manifest_and_evidence_enforce_size_hash_and_strict_base64(
    tmp_path: Path,
    target: str,
    failure: str,
    stable_code: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    manifest_response = manifest_payload
    evidence_response = MEMINFO_BYTES
    manifest_kwargs: dict[str, object] = {}
    evidence_kwargs: dict[str, object] = {}
    selected_payload = manifest_payload if target == "manifest" else MEMINFO_BYTES
    selected_kwargs = manifest_kwargs if target == "manifest" else evidence_kwargs
    if failure == "short":
        selected_kwargs["size_bytes"] = len(selected_payload) + 1
    elif failure == "hash":
        selected_kwargs["sha256_b64"] = _hash(b"different-private-payload")
    elif failure == "base64":
        selected_kwargs["sha256_b64"] = "not+strict/base64===private-marker"
    else:
        if target == "manifest":
            manifest_response += b"overflow"
        else:
            evidence_response += b"overflow"

    manifest = _manifest_input(manifest_payload, **manifest_kwargs)  # type: ignore[arg-type]
    evidence = _meminfo_input(**evidence_kwargs)  # type: ignore[arg-type]
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_response,
                "/private/attacker-name/original-meminfo-secret.txt": evidence_response,
            }
        )
    )

    try:
        await _stage_error(
            _stager(client, tmp_path),
            (manifest, evidence),
            stable_code=stable_code,
        )
        assert not (tmp_path / "memory-run-error").exists()
    finally:
        await client.aclose()


@pytest.mark.parametrize("status_code", [302, 404, 500])
@pytest.mark.asyncio
async def test_redirects_and_non_success_responses_are_retryable_and_closed(
    tmp_path: Path,
    status_code: int,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    stream = TrackingStream([b"private-response-payload"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"Location": "https://evil.example/private-redirect"},
            stream=stream,
        )

    client = _client(handler)
    try:
        error = await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
        )
    finally:
        await client.aclose()

    assert error.retryable is True
    assert "evil.example" not in repr(error)
    assert stream.closed


@pytest.mark.asyncio
async def test_manifest_must_be_strict_valid_json_before_evidence_download(
    tmp_path: Path,
) -> None:
    invalid_manifest = b'{"schema_version":"1.0","private_payload":"marker"}'
    requests: list[httpx.Request] = []
    client = _client(
        _responses(
            {
                "/private/manifest.json": invalid_manifest,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            },
            requests,
        )
    )

    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(invalid_manifest), _meminfo_input()),
            stable_code="manifest_invalid",
        )
    finally:
        await client.aclose()

    assert [request.url.path for request in requests] == ["/private/manifest.json"]
    assert not (tmp_path / "memory-run-error").exists()


@pytest.mark.parametrize("case", ["zero", "multiple", "duplicate_uuid"])
@pytest.mark.asyncio
async def test_input_identity_and_manifest_cardinality_are_checked_before_http(
    tmp_path: Path,
    case: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    manifest = _manifest_input(manifest_payload)
    meminfo = _meminfo_input()
    if case == "zero":
        inputs = (meminfo,)
    elif case == "multiple":
        inputs = (
            manifest,
            _manifest_input(
                manifest_payload,
                artifact_id=UUID("a3000000-0000-4000-8000-000000000002"),
            ),
            meminfo,
        )
    else:
        inputs = (manifest, meminfo, _meminfo_input(kind="log"))

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid inputs must not perform HTTP")

    client = _client(forbidden)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            inputs,
            stable_code="manifest_invalid",
        )
    finally:
        await client.aclose()


@pytest.mark.parametrize("case", ["missing", "extra", "role_kind_mismatch"])
@pytest.mark.asyncio
async def test_manifest_references_must_match_every_evidence_input_exactly(
    tmp_path: Path,
    case: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    manifest = _manifest_input(manifest_payload)
    meminfo = _meminfo_input(kind="log" if case == "role_kind_mismatch" else "memory_evidence")
    extra = _input(
        EXTRA_ID,
        kind="log",
        payload=LOG_BYTES,
        path="extra.log",
        mime="text/plain",
    )
    inputs = (manifest,) if case == "missing" else (manifest, meminfo, extra)
    if case == "role_kind_mismatch":
        inputs = (manifest, meminfo)
    requests: list[httpx.Request] = []
    client = _client(_responses({"/private/manifest.json": manifest_payload}, requests))

    try:
        await _stage_error(
            _stager(client, tmp_path),
            inputs,
            stable_code="manifest_invalid",
        )
    finally:
        await client.aclose()

    assert [request.url.path for request in requests] == ["/private/manifest.json"]


@pytest.mark.asyncio
async def test_declared_single_and_total_limits_fail_before_http(tmp_path: Path) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    manifest = _manifest_input(manifest_payload)
    meminfo = _meminfo_input()

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("declared limit failure must not perform HTTP")

    client = _client(forbidden)
    try:
        await _stage_error(
            _stager(
                client,
                tmp_path,
                max_file_bytes=len(manifest_payload) - 1,
                max_total_bytes=len(manifest_payload) + len(MEMINFO_BYTES),
            ),
            (manifest, meminfo),
            stable_code="input_limit_exceeded",
            run_id="single-limit",
        )
        await _stage_error(
            _stager(
                client,
                tmp_path,
                max_file_bytes=len(manifest_payload) + 1,
                max_total_bytes=len(manifest_payload) + len(MEMINFO_BYTES) - 1,
            ),
            (manifest, meminfo),
            stable_code="input_limit_exceeded",
            run_id="total-limit",
        )
    finally:
        await client.aclose()

    assert not (tmp_path / "single-limit").exists()
    assert not (tmp_path / "total-limit").exists()


@pytest.mark.asyncio
async def test_exact_total_byte_limit_is_allowed(tmp_path: Path) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    stager = _stager(
        client,
        tmp_path,
        max_file_bytes=len(manifest_payload),
        max_total_bytes=len(manifest_payload) + len(MEMINFO_BYTES),
    )

    try:
        staged = await stager.stage(
            run_id="exact-total",
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        assert (staged.input_dir / f"meminfo/meminfo-{MEMINFO_ID}.txt").is_file()
        await staged.cleanup()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_maximum_2048_evidence_files_are_allowed(tmp_path: Path) -> None:
    references = tuple((UUID(int=index + 1), "android_log") for index in range(2048))
    manifest_payload = _manifest_bytes(references)
    manifest = _manifest_input(manifest_payload)
    evidence = tuple(
        _input(
            artifact_id,
            kind="log",
            payload=b"x",
            path=f"evidence/{artifact_id}",
            mime="text/plain",
        )
        for artifact_id, _ in references
    )
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            content=manifest_payload if request.url.path == "/private/manifest.json" else b"x",
        )

    client = _client(handler)
    try:
        staged = await _stager(
            client,
            tmp_path,
            max_files=2048,
            max_file_bytes=len(manifest_payload),
            max_total_bytes=len(manifest_payload) + 2048,
        ).stage(run_id="max-files", inputs=(manifest, *evidence))
        assert sum(path.is_file() for path in staged.input_dir.rglob("*")) == 2048
        assert request_count == 2049
        await staged.cleanup()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_2049th_evidence_input_is_rejected_with_bounded_consumption(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    yielded = 0

    def unbounded_inputs() -> Iterable[EngineInput]:
        nonlocal yielded
        yielded += 1
        yield _manifest_input(manifest_payload)
        index = 1
        while True:
            yielded += 1
            artifact_id = UUID(int=10_000 + index)
            yield _input(
                artifact_id,
                kind="log",
                payload=b"x",
                path=f"unbounded/{artifact_id}",
            )
            index += 1

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("input count failure must not perform HTTP")

    client = _client(forbidden)
    try:
        await _stage_error(
            _stager(client, tmp_path, max_files=2048),
            unbounded_inputs(),
            stable_code="input_limit_exceeded",
        )
    finally:
        await client.aclose()

    assert yielded == 2050


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_files": 0},
        {"max_files": -1},
        {"max_files": True},
        {"max_files": 2049},
        {"max_file_bytes": 0},
        {"max_file_bytes": False},
        {"max_total_bytes": -1},
        {"max_total_bytes": True},
    ],
)
def test_constructor_requires_strict_positive_bounded_limits(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    client = _client(lambda _: httpx.Response(200))
    values: dict[str, object] = {
        "max_files": 2048,
        "max_file_bytes": 1024,
        "max_total_bytes": 2048,
        **kwargs,
    }
    try:
        with pytest.raises(ValueError):
            AndroidMemoryStager(
                client=client,
                workspace_root=tmp_path,
                **values,  # type: ignore[arg-type]
            )
    finally:
        assert client.is_closed is False


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        ".",
        "..",
        "../escape",
        "/absolute",
        "nested/run",
        "windows\\path",
        "nul\x00byte",
        "a" * 129,
    ],
)
@pytest.mark.asyncio
async def test_run_id_cannot_control_filesystem_paths(
    tmp_path: Path,
    run_id: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid run id must not perform HTTP")

    client = _client(forbidden)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="manifest_invalid",
            run_id=run_id,
        )
    finally:
        await client.aclose()

    assert not (tmp_path.parent / "escape").exists()


ROLE_CASES: tuple[tuple[str, str, str], ...] = (
    ("auto", "memory_evidence", "unclassified/{id}.bin"),
    ("meminfo", "memory_evidence", "meminfo/meminfo-{id}.txt"),
    ("smaps", "memory_evidence", "smaps/smaps-{id}.txt"),
    ("showmap", "memory_evidence", "showmap/showmap-{id}.txt"),
    ("hprof", "memory_evidence", "hprof/hprof-{id}.hprof"),
    ("gfxinfo", "memory_evidence", "gfxinfo/gfxinfo-{id}.txt"),
    ("proc_meminfo", "memory_evidence", "proc-meminfo/proc-meminfo-{id}.txt"),
    ("pressure_memory", "memory_evidence", "pressure-memory/pressure-memory-{id}.txt"),
    ("zram", "memory_evidence", "zram/zram-{id}.txt"),
    ("dmabuf", "memory_evidence", "dmabuf/dmabuf-{id}.txt"),
    ("exit_info", "memory_evidence", "exit-info/exit-info-{id}.txt"),
    ("analysis_report", "memory_evidence", "reports/analysis-report-{id}.json"),
    ("comparison_report", "memory_evidence", "reports/comparison-report-{id}.json"),
    ("perfetto_trace", "trace", "traces/perfetto-trace-{id}.pftrace"),
    (
        "native_heap_profile",
        "memory_evidence",
        "native-heap/native-heap-profile-{id}.heapprofd",
    ),
    ("phase_metadata", "capture_manifest", "metadata/phase-metadata-{id}.json"),
    ("device_context", "memory_evidence", "metadata/device-context-{id}.json"),
    ("previous_ai_context", "memory_evidence", "context/previous-ai-context-{id}.json"),
    (
        "previous_analysis_report",
        "memory_evidence",
        "reports/previous-analysis-report-{id}.json",
    ),
    ("android_log", "log", "logs/android-log-{id}.txt"),
    ("qa_screenshot", "screenshot", "screenshots/qa-screenshot-{id}.png"),
)


@pytest.mark.asyncio
async def test_every_allowed_role_uses_a_fixed_kind_and_generated_path(
    tmp_path: Path,
) -> None:
    references = tuple(
        (UUID(int=100 + index), role) for index, (role, _, _) in enumerate(ROLE_CASES)
    )
    manifest_payload = _manifest_bytes(references)
    evidence = tuple(
        _input(
            artifact_id,
            kind=kind,
            payload=b"x",
            path=f"private/original-name-{index}.dangerous",
        )
        for index, ((artifact_id, _), (_, kind, _)) in enumerate(
            zip(references, ROLE_CASES, strict=True)
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=manifest_payload if request.url.path == "/private/manifest.json" else b"x",
        )

    client = _client(handler)
    try:
        staged = await _stager(client, tmp_path).stage(
            run_id="all-roles",
            inputs=(_manifest_input(manifest_payload), *evidence),
        )
        actual = {
            path.relative_to(staged.input_dir).as_posix()
            for path in staged.input_dir.rglob("*")
            if path.is_file()
        }
        expected = {
            template.format(id=artifact_id)
            for (artifact_id, _), (_, _, template) in zip(references, ROLE_CASES, strict=True)
        }
        assert actual == expected
        assert all("original-name" not in path for path in actual)
        await staged.cleanup()
    finally:
        await client.aclose()


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class FaultingStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        iteration_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.chunks = chunks
        self.iteration_error = iteration_error
        self.close_error = close_error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.iteration_error is not None:
            raise self.iteration_error
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FaultyDestination:
    def __init__(self, destination: object, failure: str) -> None:
        self._destination = destination
        self._failure = failure
        self.flush_calls = 0
        self.close_calls = 0

    def __enter__(self) -> FaultyDestination:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> bool:
        self.close()
        return False

    def write(self, payload: bytes) -> int:
        if self._failure == "write":
            raise OSError(f"write failed: {PRIVATE_FAILURE_MARKER}")
        return self._destination.write(payload)  # type: ignore[union-attr,no-any-return]

    def flush(self) -> None:
        self.flush_calls += 1
        if self._failure == "flush":
            raise OSError(f"flush failed: {PRIVATE_FAILURE_MARKER}")
        self._destination.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        self.close_calls += 1
        self._destination.close()  # type: ignore[union-attr]
        if self._failure == "close":
            raise OSError(f"close failed: {PRIVATE_FAILURE_MARKER}")

    def seek(self, offset: int) -> int:
        return self._destination.seek(offset)  # type: ignore[union-attr,no-any-return]

    def read(self) -> bytes:
        return self._destination.read()  # type: ignore[union-attr,no-any-return]


@pytest.mark.parametrize("failure", ["runtime", "connect"])
@pytest.mark.asyncio
async def test_transport_exceptions_are_stable_redacted_and_context_free(
    tmp_path: Path,
    failure: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))

    def handler(request: httpx.Request) -> httpx.Response:
        message = f"{PRIVATE_FAILURE_MARKER}: {request.url}"
        if failure == "connect":
            raise httpx.ConnectError(message, request=request)
        raise RuntimeError(message)

    client = _client(handler)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id=f"transport-{failure}",
        )
    finally:
        await client.aclose()

    assert not (tmp_path / f"transport-{failure}").exists()


@pytest.mark.parametrize("operation", ["build_request", "send"])
@pytest.mark.asyncio
async def test_url_build_and_send_exceptions_are_stable_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(lambda _: httpx.Response(200, content=manifest_payload))
    message = (
        f"{PRIVATE_FAILURE_MARKER}: https://objects.example/private?signature=secret-query-marker"
    )

    if operation == "build_request":

        def fail_build(*_: object, **__: object) -> httpx.Request:
            raise RuntimeError(message)

        monkeypatch.setattr(client, "build_request", fail_build)
    else:

        async def fail_send(*_: object, **__: object) -> httpx.Response:
            raise RuntimeError(message)

        monkeypatch.setattr(client, "send", fail_send)

    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id=f"client-{operation}",
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_http_stream_iteration_exception_is_stable_and_response_closes(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    stream = FaultingStream(
        [],
        iteration_error=RuntimeError(
            f"stream failed: {PRIVATE_FAILURE_MARKER}: secret-query-marker"
        ),
    )
    client = _client(lambda _: httpx.Response(200, stream=stream))

    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id="stream-runtime",
        )
    finally:
        await client.aclose()

    assert stream.closed
    assert not (tmp_path / "stream-runtime").exists()


@pytest.mark.parametrize("failure", ["write", "flush", "close"])
@pytest.mark.asyncio
async def test_destination_failures_are_stable_and_trigger_workspace_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    real_fdopen = stager_module.os.fdopen

    def faulty_fdopen(*args: object, **kwargs: object) -> FaultyDestination:
        return FaultyDestination(real_fdopen(*args, **kwargs), failure)  # type: ignore[arg-type]

    monkeypatch.setattr(stager_module.os, "fdopen", faulty_fdopen)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id=f"destination-{failure}",
        )
    finally:
        await client.aclose()

    assert not (tmp_path / f"destination-{failure}").exists()


@pytest.mark.parametrize("manifest_case", ["invalid_json", "strict_validation"])
@pytest.mark.parametrize("spool_failure", ["flush", "close"])
@pytest.mark.asyncio
async def test_manifest_invalid_precedes_spool_flush_and_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_case: str,
    spool_failure: str,
) -> None:
    if manifest_case == "invalid_json":
        manifest_payload = b"{not-json"
    else:
        valid_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
        manifest_payload = valid_payload[:-1] + b',"unexpected_private_field":true}'

    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
            }
        )
    )
    real_spool = stager_module.tempfile.SpooledTemporaryFile
    destinations: list[FaultyDestination] = []

    def faulty_spool(*args: object, **kwargs: object) -> FaultyDestination:
        destination = FaultyDestination(
            real_spool(*args, **kwargs),  # type: ignore[arg-type]
            spool_failure,
        )
        destinations.append(destination)
        return destination

    monkeypatch.setattr(stager_module.tempfile, "SpooledTemporaryFile", faulty_spool)
    run_id = f"manifest-{manifest_case}-{spool_failure}"
    try:
        error = await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload),),
            stable_code="manifest_invalid",
            run_id=run_id,
        )
    finally:
        await client.aclose()

    assert error.retryable is False
    assert len(destinations) == 1
    assert destinations[0].close_calls == 1
    assert not (tmp_path / run_id).exists()


@pytest.mark.parametrize("spool_failure", ["flush", "close"])
@pytest.mark.asyncio
async def test_manifest_route_invalid_precedes_spool_flush_and_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spool_failure: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    requests: list[httpx.Request] = []
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
            },
            requests,
        )
    )
    real_spool = stager_module.tempfile.SpooledTemporaryFile
    destinations: list[FaultyDestination] = []

    def faulty_spool(*args: object, **kwargs: object) -> FaultyDestination:
        destination = FaultyDestination(
            real_spool(*args, **kwargs),  # type: ignore[arg-type]
            spool_failure,
        )
        destinations.append(destination)
        return destination

    monkeypatch.setattr(stager_module.tempfile, "SpooledTemporaryFile", faulty_spool)
    run_id = f"manifest-route-{spool_failure}"
    try:
        error = await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload),),
            stable_code="manifest_invalid",
            run_id=run_id,
        )
    finally:
        await client.aclose()

    assert error.retryable is False
    assert [request.url.path for request in requests] == ["/private/manifest.json"]
    assert len(destinations) == 1
    assert destinations[0].close_calls == 1
    assert not (tmp_path / run_id).exists()


@pytest.mark.parametrize(
    ("case", "stable_code"),
    [
        ("close_only", "download_failed"),
        ("overflow", "input_limit_exceeded"),
        ("hash", "integrity_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_response_close_failure_never_leaks_or_overrides_primary_error(
    tmp_path: Path,
    case: str,
    stable_code: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    chunks = [manifest_payload]
    if case == "overflow":
        chunks.append(b"overflow")
    stream = FaultingStream(
        chunks,
        close_error=RuntimeError(f"close failed: {PRIVATE_FAILURE_MARKER}: secret-query-marker"),
    )
    client = _client(lambda _: httpx.Response(200, stream=stream))
    manifest = _manifest_input(
        manifest_payload,
        sha256_b64=(_hash(b"different-private-payload") if case == "hash" else None),
    )

    try:
        await _stage_error(
            _stager(client, tmp_path),
            (manifest, _meminfo_input()),
            stable_code=stable_code,
            run_id=f"response-close-{case}",
        )
    finally:
        await client.aclose()

    assert stream.closed


@pytest.mark.asyncio
async def test_cancellation_is_preserved_when_response_close_also_fails(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    cancellation = asyncio.CancelledError(PRIVATE_FAILURE_MARKER)
    stream = FaultingStream(
        [],
        iteration_error=cancellation,
        close_error=RuntimeError(f"close failed: {PRIVATE_FAILURE_MARKER}"),
    )
    client = _client(lambda _: httpx.Response(200, stream=stream))

    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await _stager(client, tmp_path).stage(
                run_id="cancel-close",
                inputs=(_manifest_input(manifest_payload), _meminfo_input()),
            )
    finally:
        await client.aclose()

    assert caught.value is cancellation
    assert stream.closed
    assert not (tmp_path / "cancel-close").exists()


@pytest.mark.parametrize(
    "terminal_error",
    [KeyboardInterrupt(PRIVATE_FAILURE_MARKER), SystemExit(PRIVATE_FAILURE_MARKER)],
    ids=["keyboard-interrupt", "system-exit"],
)
@pytest.mark.asyncio
async def test_process_control_exceptions_are_never_swallowed_or_overridden(
    tmp_path: Path,
    terminal_error: KeyboardInterrupt | SystemExit,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    stream = FaultingStream(
        [],
        iteration_error=terminal_error,
        close_error=RuntimeError(f"close failed: {PRIVATE_FAILURE_MARKER}"),
    )
    client = _client(lambda _: httpx.Response(200, stream=stream))

    try:
        with pytest.raises(type(terminal_error)) as caught:
            await _stager(client, tmp_path).stage(
                run_id="process-control",
                inputs=(_manifest_input(manifest_payload), _meminfo_input()),
            )
    finally:
        await client.aclose()

    assert caught.value is terminal_error
    assert stream.closed
    assert not (tmp_path / "process-control").exists()


@pytest.mark.asyncio
async def test_stream_response_closes_and_partial_workspace_is_cleaned_on_failure(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    evidence_stream = TrackingStream([MEMINFO_BYTES, b"overflow"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/private/manifest.json":
            return httpx.Response(200, content=manifest_payload)
        return httpx.Response(200, stream=evidence_stream)

    client = _client(handler)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="input_limit_exceeded",
            run_id="partial-failure",
        )
    finally:
        await client.aclose()

    assert evidence_stream.closed
    assert not (tmp_path / "partial-failure").exists()


@pytest.mark.parametrize("attack", ["directory", "file"])
@pytest.mark.asyncio
async def test_symlinked_destinations_fail_closed_without_touching_target(
    tmp_path: Path,
    attack: str,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("do-not-delete", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/private/manifest.json":
            input_dir = tmp_path / "symlink-run" / "input"
            if attack == "directory":
                (input_dir / "meminfo").symlink_to(outside, target_is_directory=True)
            else:
                role_dir = input_dir / "meminfo"
                role_dir.mkdir()
                (role_dir / f"meminfo-{MEMINFO_ID}.txt").symlink_to(sentinel)
            return httpx.Response(200, content=manifest_payload)
        return httpx.Response(200, content=MEMINFO_BYTES)

    client = _client(handler)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id="symlink-run",
        )
    finally:
        await client.aclose()

    assert sentinel.read_text(encoding="utf-8") == "do-not-delete"
    assert not (tmp_path / "symlink-run").exists()


@pytest.mark.asyncio
async def test_symlinked_workspace_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside-root"
    outside.mkdir()
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(outside, target_is_directory=True)
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("symlinked root must fail before HTTP")

    client = _client(forbidden)
    try:
        await _stage_error(
            _stager(client, workspace_link),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id="root-symlink",
        )
    finally:
        await client.aclose()

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_partial_workspace_rollback_removes_owned_empty_run_after_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    requests: list[httpx.Request] = []
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            },
            requests,
        )
    )
    run_id = "partial-run-open-failure"
    run_dir = tmp_path / run_id
    real_open = stager_module.os.open
    root_fds: list[int] = []
    failed = False

    def failing_run_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal failed
        if path == run_id and dir_fd is not None and flags & os.O_DIRECTORY and not failed:
            failed = True
            raise OSError(f"open failed: {PRIVATE_FAILURE_MARKER}")
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == tmp_path and dir_fd is None and flags & os.O_DIRECTORY:
            root_fds.append(fd)
        return fd

    monkeypatch.setattr(stager_module.os, "open", failing_run_open)
    try:
        error = await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id=run_id,
        )

        assert error.retryable is True
        assert requests == []
        assert not run_dir.exists()
        assert len(root_fds) == 1
        with pytest.raises(OSError):
            os.fstat(root_fds[0])
        root_stat = os.stat(tmp_path)
        owner_key = (root_stat.st_dev, root_stat.st_ino, run_id)
        with stager_module._ACTIVE_OWNERS_LOCK:
            assert owner_key not in stager_module._ACTIVE_OWNERS

        monkeypatch.setattr(stager_module.os, "open", real_open)
        staged = await _stager(client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        await staged.cleanup()
    finally:
        await client.aclose()

    assert failed
    assert [request.url.path for request in requests] == [
        "/private/manifest.json",
        "/private/attacker-name/original-meminfo-secret.txt",
    ]
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_partial_workspace_rollback_never_deletes_a_replacement_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    run_id = "partial-owner-race"
    run_dir = tmp_path / run_id
    retired_dir = tmp_path / f"{run_id}-retired"
    replacement_marker = run_dir / "replacement-owner.txt"
    real_open = stager_module.os.open
    raced = False

    def racing_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if path == run_id and dir_fd is not None and not raced:
            raced = True
            run_dir.rename(retired_dir)
            run_dir.mkdir()
            replacement_marker.write_text("later-owner", encoding="utf-8")
            raise OSError(f"open failed: {PRIVATE_FAILURE_MARKER}")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(stager_module.os, "open", racing_open)
    try:
        await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(manifest_payload), _meminfo_input()),
            stable_code="download_failed",
            run_id=run_id,
        )

        assert replacement_marker.read_text(encoding="utf-8") == "later-owner"
        assert retired_dir.is_dir()
        replacement_marker.unlink()
        run_dir.rmdir()
        retired_dir.rmdir()

        staged = await _stager(client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        await staged.cleanup()
    finally:
        await client.aclose()

    assert raced
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_cleanup_identity_check_race_never_recurses_into_replacement_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    run_id = "cleanup-owner-race"
    run_dir = tmp_path / run_id
    retired_dir = tmp_path / f"{run_id}-retired"
    replacement_marker = run_dir / "replacement-owner.txt"

    try:
        staged = await _stager(client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        real_lstat = stager_module.os.lstat
        real_stat = stager_module.os.stat
        raced = False

        def replace_owned_binding() -> None:
            nonlocal raced
            if raced:
                return
            raced = True
            run_dir.rename(retired_dir)
            run_dir.mkdir()
            replacement_marker.write_text("later-owner", encoding="utf-8")

        def racing_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            result = real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]
            if Path(path) == run_dir:  # type: ignore[arg-type]
                replace_owned_binding()
            return result

        def racing_stat(
            path: object,
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
            if (
                path == run_id
                and kwargs.get("dir_fd") is not None
                and kwargs.get("follow_symlinks") is False
            ):
                replace_owned_binding()
            return result

        monkeypatch.setattr(stager_module.os, "lstat", racing_lstat)
        monkeypatch.setattr(stager_module.os, "stat", racing_stat)

        await staged.cleanup()
        await staged.cleanup()

        assert raced
        assert replacement_marker.read_text(encoding="utf-8") == "later-owner"
        assert retired_dir.is_dir()
        assert list(retired_dir.iterdir()) == []
        replacement_marker.unlink()
        run_dir.rmdir()
        retired_dir.rmdir()
    finally:
        await client.aclose()


@pytest.mark.parametrize("cleanup_failure", ["clear", "rmdir", "remove"])
@pytest.mark.asyncio
async def test_stage_failure_abandons_resources_when_cleanup_cannot_remove_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    invalid_manifest = b"{not-json"
    run_id = f"abandon-{cleanup_failure}"
    run_dir = tmp_path / run_id
    client = _client(
        _responses(
            {
                "/private/manifest.json": invalid_manifest,
            }
        )
    )
    real_create_workspace = AndroidMemoryStager._create_workspace
    real_clear_directory = stager_module._clear_directory_fd
    real_remove_owned = stager_module._remove_owned_directory
    real_rmdir = stager_module.os.rmdir
    created: list[stager_module._OwnedWorkspace] = []

    def recording_create_workspace(
        self: AndroidMemoryStager,
        requested_run_id: str,
    ) -> stager_module._OwnedWorkspace:
        owned = real_create_workspace(self, requested_run_id)
        created.append(owned)
        return owned

    monkeypatch.setattr(
        AndroidMemoryStager,
        "_create_workspace",
        recording_create_workspace,
    )
    if cleanup_failure == "clear":
        monkeypatch.setattr(stager_module, "_clear_directory_fd", lambda _: False)
    elif cleanup_failure == "rmdir":

        def failing_rmdir(
            path: object,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if path == run_id and dir_fd is not None:
                raise OSError(f"rmdir failed: {PRIVATE_FAILURE_MARKER}")
            real_rmdir(path, dir_fd=dir_fd)  # type: ignore[arg-type]

        monkeypatch.setattr(stager_module.os, "rmdir", failing_rmdir)
    else:

        def failing_remove(_: stager_module._OwnedWorkspace) -> bool:
            raise RuntimeError(f"remove failed: {PRIVATE_FAILURE_MARKER}")

        monkeypatch.setattr(stager_module, "_remove_owned_directory", failing_remove)

    try:
        error = await _stage_error(
            _stager(client, tmp_path),
            (_manifest_input(invalid_manifest),),
            stable_code="manifest_invalid",
            run_id=run_id,
        )
    finally:
        await client.aclose()

    assert error.retryable is False
    assert len(created) == 1
    for fd in (created[0].input_fd, created[0].run_fd, created[0].root_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
    assert run_dir.is_dir()

    monkeypatch.setattr(stager_module, "_clear_directory_fd", real_clear_directory)
    monkeypatch.setattr(stager_module, "_remove_owned_directory", real_remove_owned)
    monkeypatch.setattr(stager_module.os, "rmdir", real_rmdir)
    shutil.rmtree(run_dir)

    valid_manifest = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    retry_client = _client(
        _responses(
            {
                "/private/manifest.json": valid_manifest,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    try:
        staged = await _stager(retry_client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(valid_manifest), _meminfo_input()),
        )
        await staged.cleanup()
    finally:
        await retry_client.aclose()

    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_successful_stage_retains_resources_when_cleanup_needs_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    run_id = "cleanup-retry"
    real_clear_directory = stager_module._clear_directory_fd

    try:
        staged = await _stager(client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        owned = staged._cleanup_state.owned  # type: ignore[attr-defined]
        monkeypatch.setattr(stager_module, "_clear_directory_fd", lambda _: False)

        await staged.cleanup()

        assert staged.input_dir.is_dir()
        for fd in (owned.input_fd, owned.run_fd, owned.root_fd):
            os.fstat(fd)
        with stager_module._ACTIVE_OWNERS_LOCK:
            assert stager_module._ACTIVE_OWNERS.get(owned.owner_key) is owned.owner_token

        monkeypatch.setattr(stager_module, "_clear_directory_fd", real_clear_directory)
        await staged.cleanup()
        for fd in (owned.input_fd, owned.run_fd, owned.root_fd):
            with pytest.raises(OSError):
                os.fstat(fd)
        with stager_module._ACTIVE_OWNERS_LOCK:
            assert owned.owner_key not in stager_module._ACTIVE_OWNERS

        retried = await _stager(client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        await retried.cleanup()
    finally:
        await client.aclose()

    assert not (tmp_path / run_id).exists()


@pytest.mark.asyncio
async def test_abandon_unconditionally_releases_resources_when_removal_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    run_id = "public-abandon"

    try:
        staged = await _stager(client, tmp_path).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        owned = staged._cleanup_state.owned  # type: ignore[attr-defined]

        def failing_remove(_: stager_module._OwnedWorkspace) -> bool:
            raise RuntimeError(f"remove failed: {PRIVATE_FAILURE_MARKER}")

        monkeypatch.setattr(stager_module, "_remove_owned_directory", failing_remove)

        await staged.abandon()
        await staged.abandon()

        assert staged.input_dir.is_dir()
        for fd in (owned.input_fd, owned.run_fd, owned.root_fd):
            with pytest.raises(OSError):
                os.fstat(fd)
        with stager_module._ACTIVE_OWNERS_LOCK:
            assert owned.owner_key not in stager_module._ACTIVE_OWNERS
    finally:
        await client.aclose()
        shutil.rmtree(tmp_path / run_id, ignore_errors=True)


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_and_never_deletes_a_later_same_id_owner(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    stager = _stager(client, tmp_path)

    try:
        first = await stager.stage(
            run_id="reused-run",
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        await first.cleanup()
        await first.cleanup()

        second = await stager.stage(
            run_id="reused-run",
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        await first.cleanup()
        assert second.input_dir.is_dir()
        await second.cleanup()
    finally:
        await client.aclose()

    assert not (tmp_path / "reused-run").exists()


@pytest.mark.parametrize("alias_kind", ["parent_symlink", "dotdot"])
@pytest.mark.asyncio
async def test_same_root_inode_alias_rejects_a_concurrent_run_owner(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    real_parent = tmp_path / "real-parent"
    workspace_root = real_parent / "workspace"
    workspace_root.mkdir(parents=True)
    if alias_kind == "parent_symlink":
        alias_parent = tmp_path / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        alias_root = alias_parent / "workspace"
    else:
        hop = real_parent / "hop"
        hop.mkdir()
        alias_root = hop / ".." / "workspace"

    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))
    owner_client = _client(
        _responses(
            {
                "/private/manifest.json": manifest_payload,
                "/private/attacker-name/original-meminfo-secret.txt": MEMINFO_BYTES,
            }
        )
    )
    run_id = f"root-alias-{alias_kind}"
    run_dir = workspace_root / run_id
    retired_dir = workspace_root / f"{run_id}-retired"

    def attempt_alias_from_another_loop() -> tuple[EngineAdapterError, list[str]]:
        async def attempt() -> tuple[EngineAdapterError, list[str]]:
            alias_requests: list[str] = []

            def forbidden_alias_request(request: httpx.Request) -> httpx.Response:
                alias_requests.append(request.url.path)
                raise AssertionError("duplicate owner must fail before HTTP")

            alias_client = _client(forbidden_alias_request)
            try:
                error = await _stage_error(
                    _stager(alias_client, alias_root),
                    (_manifest_input(manifest_payload), _meminfo_input()),
                    stable_code="download_failed",
                    run_id=run_id,
                )
            finally:
                await alias_client.aclose()
            return error, alias_requests

        return asyncio.run(attempt())

    try:
        owner = await _stager(owner_client, workspace_root).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        run_dir.rename(retired_dir)

        error, alias_requests = await asyncio.to_thread(
            attempt_alias_from_another_loop,
        )
        assert error.retryable is True
        assert alias_requests == []

        await owner.cleanup()
        shutil.rmtree(retired_dir)

        retry = await _stager(owner_client, alias_root).stage(
            run_id=run_id,
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )
        await retry.cleanup()
    finally:
        await owner_client.aclose()

    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_concurrent_same_run_id_has_one_owner_and_loser_never_cleans_winner(
    tmp_path: Path,
) -> None:
    manifest_payload = _manifest_bytes(((MEMINFO_ID, "meminfo"),))

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            content=(
                manifest_payload if request.url.path == "/private/manifest.json" else MEMINFO_BYTES
            ),
        )

    client = _client(handler)
    stager = _stager(client, tmp_path)

    async def attempt() -> StagedMemoryInput:
        return await stager.stage(
            run_id="concurrent-run",
            inputs=(_manifest_input(manifest_payload), _meminfo_input()),
        )

    try:
        results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
        winners = [result for result in results if isinstance(result, StagedMemoryInput)]
        losers = [result for result in results if isinstance(result, EngineAdapterError)]

        assert len(winners) == 1
        assert len(losers) == 1
        assert winners[0].input_dir.is_dir()
        assert losers[0].stable_code == "download_failed"
        assert losers[0].retryable is True
        await winners[0].cleanup()
    finally:
        await client.aclose()

    assert not (tmp_path / "concurrent-run").exists()
