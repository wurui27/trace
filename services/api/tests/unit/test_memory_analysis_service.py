from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.engines.android_memory_contracts import MemoryArtifactRef, MemorySubject
from perfpilot_api.services.internal_artifacts import (
    InternalArtifactConflictError,
    manifest_artifact_id,
)
from perfpilot_api.services.memory_analyses import (
    MemoryAnalysisContext,
    MemoryCaptureConflictError,
    MemoryCaptureInvalidRequestError,
    MemoryCaptureNotFoundError,
    MemoryCaptureService,
    ReferencedArtifact,
)


TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
OTHER_ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000002")
CAPTURE_ID = UUID("40000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("50000000-0000-4000-8000-000000000001")
SECOND_ARTIFACT_ID = UUID("50000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _artifact(
    artifact_id: UUID = ARTIFACT_ID,
    *,
    analysis_id: UUID = ANALYSIS_ID,
    artifact_kind: str = "memory_evidence",
    state: str = "finalized",
    expires_at: datetime = NOW + timedelta(days=1),
    deleted_at: datetime | None = None,
) -> ReferencedArtifact:
    return ReferencedArtifact(
        artifact_id=artifact_id,
        analysis_id=analysis_id,
        artifact_kind=artifact_kind,
        state=state,
        expires_at=expires_at,
        deleted_at=deleted_at,
    )


def _context(
    *artifacts: ReferencedArtifact,
    tenant_resource_version: int = 1,
) -> MemoryAnalysisContext:
    return MemoryAnalysisContext(
        analysis_id=ANALYSIS_ID,
        analysis_mode="memory_upload",
        state="created",
        tombstoned_at=None,
        tenant_resource_version=tenant_resource_version,
        package_name="com.example.app",
        artifacts=artifacts or (_artifact(),),
    )


class FakeRepository:
    def __init__(self, context: MemoryAnalysisContext) -> None:
        self.context = context
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def load_context(self, **kwargs: object) -> MemoryAnalysisContext:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.context


class FakeSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def write_json(self, **kwargs: object) -> UUID:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return kwargs["artifact_id"]  # type: ignore[return-value]


async def _create(
    service: MemoryCaptureService,
    *,
    subject: MemorySubject | None = None,
    artifacts: tuple[MemoryArtifactRef, ...] | None = None,
) -> object:
    return await service.create_capture(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        phase="single",
        source="manual_upload",
        captured_at=None,
        subject=subject or MemorySubject(package="com.example.app", android_sdk=37),
        artifacts=artifacts or (MemoryArtifactRef(artifact_id=ARTIFACT_ID, role="meminfo"),),
    )


def _service(repository: FakeRepository, sink: FakeSink) -> MemoryCaptureService:
    return MemoryCaptureService(
        repository=repository,
        manifest_sink=sink,
        clock=lambda: NOW,
        uuid_source=lambda: CAPTURE_ID,
    )


@pytest.mark.asyncio
async def test_create_capture_rebinds_artifacts_and_writes_canonical_manifest() -> None:
    repository = FakeRepository(_context())
    sink = FakeSink()

    created = await _create(_service(repository, sink))

    assert created.manifest.capture_id == CAPTURE_ID
    assert created.artifact_id == manifest_artifact_id(CAPTURE_ID)
    assert created.manifest_sha256 == created.manifest.sha256_hex()
    assert sink.calls == [
        {
            "team_id": TEAM_ID,
            "expected_tenant_resource_version": 1,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": manifest_artifact_id(CAPTURE_ID),
            "artifact_kind": "memory_capture_manifest",
            "payload": created.manifest.canonical_bytes(),
        }
    ]
    payload = sink.calls[0]["payload"]
    assert isinstance(payload, bytes)
    assert b"bucket" not in payload
    assert b"object_key" not in payload
    assert b"sha256" not in payload


@pytest.mark.asyncio
async def test_validated_context_resource_version_is_forwarded_to_manifest_sink() -> None:
    repository = FakeRepository(_context(tenant_resource_version=7))
    sink = FakeSink()

    await _create(_service(repository, sink))

    assert sink.calls[0]["expected_tenant_resource_version"] == 7


@pytest.mark.parametrize(
    "context",
    [
        _context(_artifact(analysis_id=OTHER_ANALYSIS_ID)),
        _context(_artifact(state="pending")),
        _context(_artifact(state="expired")),
        _context(_artifact(expires_at=NOW)),
        _context(_artifact(deleted_at=NOW - timedelta(seconds=1))),
        replace(_context(), analysis_mode="device"),
        replace(_context(), state="deleted"),
        replace(_context(), tombstoned_at=NOW),
    ],
)
@pytest.mark.asyncio
async def test_wrong_owner_or_unavailable_state_is_one_stable_not_found(
    context: MemoryAnalysisContext,
) -> None:
    repository = FakeRepository(context)
    sink = FakeSink()

    with pytest.raises(MemoryCaptureNotFoundError) as caught:
        await _create(_service(repository, sink))

    assert str(caught.value) == "memory capture inputs were not found"
    assert sink.calls == []


@pytest.mark.asyncio
async def test_other_tenant_and_absent_rows_are_indistinguishable() -> None:
    marker = "private-tenant-route-marker"
    repository = FakeRepository(_context())
    repository.error = MemoryCaptureNotFoundError("memory capture inputs were not found")
    sink = FakeSink()

    with pytest.raises(MemoryCaptureNotFoundError) as caught:
        await _create(_service(repository, sink))

    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert sink.calls == []


@pytest.mark.parametrize(
    "artifact_kind",
    ["apk", "mapping", "native_symbols", "source_archive", "memory_capture_manifest"],
)
@pytest.mark.asyncio
async def test_disallowed_artifact_kinds_are_not_manifest_inputs(artifact_kind: str) -> None:
    repository = FakeRepository(_context(_artifact(artifact_kind=artifact_kind)))
    sink = FakeSink()

    with pytest.raises(MemoryCaptureNotFoundError):
        await _create(_service(repository, sink))

    assert sink.calls == []


@pytest.mark.parametrize(
    "artifact_kind",
    ["memory_evidence", "screenshot", "log", "trace", "capture_manifest"],
)
@pytest.mark.asyncio
async def test_supported_memory_input_kinds_are_preserved(artifact_kind: str) -> None:
    repository = FakeRepository(_context(_artifact(artifact_kind=artifact_kind)))
    sink = FakeSink()

    created = await _create(_service(repository, sink))

    assert created.manifest.artifacts[0].artifact_id == ARTIFACT_ID


@pytest.mark.asyncio
async def test_duplicate_artifact_ids_are_rejected_before_repository_access() -> None:
    repository = FakeRepository(_context())
    sink = FakeSink()
    refs = (
        MemoryArtifactRef(artifact_id=ARTIFACT_ID, role="meminfo"),
        MemoryArtifactRef(artifact_id=ARTIFACT_ID, role="android_log"),
    )

    with pytest.raises(MemoryCaptureInvalidRequestError):
        await _create(_service(repository, sink), artifacts=refs)

    assert repository.calls == []
    assert sink.calls == []


@pytest.mark.asyncio
async def test_missing_one_of_multiple_artifacts_is_not_found() -> None:
    repository = FakeRepository(_context(_artifact()))
    sink = FakeSink()
    refs = (
        MemoryArtifactRef(artifact_id=ARTIFACT_ID, role="meminfo"),
        MemoryArtifactRef(artifact_id=SECOND_ARTIFACT_ID, role="android_log"),
    )

    with pytest.raises(MemoryCaptureNotFoundError):
        await _create(_service(repository, sink), artifacts=refs)

    assert sink.calls == []


@pytest.mark.asyncio
async def test_package_mismatch_is_recorded_in_manifest_instead_of_rejected() -> None:
    repository = FakeRepository(_context())
    sink = FakeSink()
    supplied = MemorySubject(package="com.other.app", android_sdk=37)

    created = await _create(_service(repository, sink), subject=supplied)

    assert repository.context.package_name == "com.example.app"
    assert created.manifest.subject.package == "com.other.app"
    assert b'"package":"com.other.app"' in created.manifest.canonical_bytes()


@pytest.mark.asyncio
async def test_exact_replay_returns_the_same_manifest_artifact() -> None:
    repository = FakeRepository(_context())
    sink = FakeSink()
    service = _service(repository, sink)

    first = await _create(service)
    replay = await _create(service)

    assert replay == first
    assert len(sink.calls) == 2
    assert sink.calls[0]["payload"] == sink.calls[1]["payload"]


@pytest.mark.asyncio
async def test_sink_idempotency_conflict_is_redacted_at_service_boundary() -> None:
    marker = "private-object-checksum-marker"
    repository = FakeRepository(_context())
    sink = FakeSink()
    sink.error = InternalArtifactConflictError(marker)

    with pytest.raises(MemoryCaptureConflictError) as caught:
        await _create(_service(repository, sink))

    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
