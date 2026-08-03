from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.engines.android_memory_contracts import (
    MemoryArtifactRef,
    MemoryCaptureManifest,
    MemorySubject,
)
from perfpilot_api.services.internal_artifacts import manifest_artifact_id
from perfpilot_api.services.memory_executions import (
    MemoryExecutionNotFoundError,
    MemoryExecutionUnavailableError,
    SQLAlchemyMemoryExecutionRepository,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("71000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("72000000-0000-4000-8000-000000000001")
CAPTURE_ID = UUID("73000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("74000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _manifest() -> MemoryCaptureManifest:
    return MemoryCaptureManifest(
        schema_version="1.0",
        analysis_id=ANALYSIS_ID,
        capture_id=CAPTURE_ID,
        phase="single",
        source="manual_upload",
        subject=MemorySubject(package="com.example.app"),
        artifacts=(MemoryArtifactRef(artifact_id=EVIDENCE_ID, role="meminfo"),),
    )


def _artifact(
    artifact_id: UUID,
    *,
    kind: str,
    mime: str,
    size: int,
    checksum: str,
    object_key: str,
    deleted_at: datetime | None = None,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        analysis_id=ANALYSIS_ID,
        upload_id=artifact_id,
        artifact_kind=kind,
        mime_type=mime,
        size_bytes=size,
        sha256_b64=checksum,
        object_key=object_key,
        version_id="fixed-version",
        state="finalized",
        finalized_at=NOW,
        expires_at=NOW + timedelta(days=1),
        deleted_at=deleted_at,
        version=2,
    )


class _ScalarRows:
    def __init__(self, rows: tuple[Artifact, ...]) -> None:
        self._rows = rows

    def all(self) -> tuple[Artifact, ...]:
        return self._rows


class _Session:
    def __init__(
        self,
        *,
        scalars: list[object] | None = None,
        rows: tuple[Artifact, ...] = (),
        resource_version: int = 7,
    ) -> None:
        self.info = {"tenant_resource_version": resource_version}
        self._scalars = list(scalars or ())
        self._rows = rows

    async def scalar(self, _statement: object) -> object:
        return self._scalars.pop(0)

    async def scalars(self, _statement: object) -> _ScalarRows:
        return _ScalarRows(self._rows)


class _Router:
    def __init__(self, sessions: list[_Session]) -> None:
        self._sessions = sessions
        self.team_ids: list[UUID] = []

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[_Session]:
        self.team_ids.append(team_id)
        yield self._sessions.pop(0)


class _Resolver:
    def __init__(self, versions: tuple[int, ...] = (7, 7)) -> None:
        self._versions = list(versions)

    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        return TenantBucket(
            team_id=team_id,
            bucket="private-team-bucket",
            resource_version=self._versions.pop(0),
        )


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


class _S3:
    def __init__(self, payload: bytes, **metadata_changes: object) -> None:
        self.payload = payload
        self.metadata_changes = metadata_changes
        self.calls: list[dict[str, object]] = []
        self.body = _Body(payload)

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        metadata: dict[str, object] = {
            "VersionId": "fixed-version",
            "ChecksumSHA256": _checksum(self.payload),
            "ContentType": "application/json",
            "ContentLength": len(self.payload),
            "DeleteMarker": False,
            "Body": self.body,
        }
        metadata.update(self.metadata_changes)
        return metadata


def _repository(
    *,
    s3: _S3,
    evidence: Artifact | None = None,
    resolver: _Resolver | None = None,
) -> tuple[SQLAlchemyMemoryExecutionRepository, _Router]:
    manifest = _manifest()
    payload = manifest.canonical_bytes()
    manifest_id = manifest_artifact_id(CAPTURE_ID)
    manifest_row = _artifact(
        manifest_id,
        kind="memory_capture_manifest",
        mime="application/json",
        size=len(payload),
        checksum=_checksum(payload),
        object_key=(f"raw/analyses/{ANALYSIS_ID}/internal/memory_capture_manifest/{manifest_id}"),
    )
    evidence_row = evidence or _artifact(
        EVIDENCE_ID,
        kind="memory_evidence",
        mime="text/plain",
        size=7,
        checksum=_checksum(b"meminfo"),
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/memory_evidence/{EVIDENCE_ID}",
    )
    analysis = Analysis(
        id=ANALYSIS_ID,
        analysis_mode="memory_upload",
        question="question",
        state="created",
        tombstoned_at=None,
        version=1,
    )
    router = _Router(
        [
            _Session(scalars=[analysis, manifest_row]),
            _Session(rows=(evidence_row,)),
            _Session(),
        ]
    )
    return (
        SQLAlchemyMemoryExecutionRepository(
            tenant_router=router,  # type: ignore[arg-type]
            bucket_resolver=resolver or _Resolver(),
            client=s3,
            clock=lambda: NOW,
        ),
        router,
    )


@pytest.mark.asyncio
async def test_repository_reads_exact_manifest_version_and_evidence_rows() -> None:
    payload = _manifest().canonical_bytes()
    s3 = _S3(payload)
    repository, router = _repository(s3=s3)

    loaded = await repository.load_capture(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        capture_id=CAPTURE_ID,
    )

    manifest_id = manifest_artifact_id(CAPTURE_ID)
    assert loaded.manifest_bytes == payload
    assert loaded.manifest_artifact.artifact_id == manifest_id
    assert loaded.manifest_artifact.version == 2
    assert tuple(item.artifact_id for item in loaded.evidence_artifacts) == (EVIDENCE_ID,)
    assert s3.calls == [
        {
            "Bucket": "private-team-bucket",
            "Key": (f"raw/analyses/{ANALYSIS_ID}/internal/memory_capture_manifest/{manifest_id}"),
            "VersionId": "fixed-version",
            "ChecksumMode": "ENABLED",
        }
    ]
    assert s3.body.closed
    assert router.team_ids == [TEAM_ID, TEAM_ID, TEAM_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata_changes",
    [
        {"VersionId": "other-version"},
        {"ChecksumSHA256": "A" * 44},
        {"ContentType": "text/plain"},
        {"ContentLength": 1},
        {"DeleteMarker": True},
    ],
)
async def test_repository_rejects_changed_immutable_object_metadata(
    metadata_changes: dict[str, object],
) -> None:
    s3 = _S3(_manifest().canonical_bytes(), **metadata_changes)
    repository, _router = _repository(s3=s3)

    with pytest.raises(MemoryExecutionNotFoundError, match="memory capture"):
        await repository.load_capture(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )


@pytest.mark.asyncio
async def test_repository_rejects_deleted_evidence_and_resource_rollover() -> None:
    payload = _manifest().canonical_bytes()
    deleted = _artifact(
        EVIDENCE_ID,
        kind="memory_evidence",
        mime="text/plain",
        size=7,
        checksum=_checksum(b"meminfo"),
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/memory_evidence/{EVIDENCE_ID}",
        deleted_at=NOW,
    )
    deleted_repository, _router = _repository(s3=_S3(payload), evidence=deleted)
    with pytest.raises(MemoryExecutionNotFoundError):
        await deleted_repository.load_capture(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )

    rollover_repository, _router = _repository(
        s3=_S3(payload),
        resolver=_Resolver((7, 8)),
    )
    with pytest.raises(MemoryExecutionUnavailableError, match="unavailable"):
        await rollover_repository.load_capture(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )
