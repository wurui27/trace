from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.engines.android_memory_contracts import (
    MemoryArtifactRef,
    MemoryCaptureManifest,
    MemorySubject,
)
from perfpilot_api.services.engine_executions import EngineExecutionRecord
from perfpilot_api.services.internal_artifacts import manifest_artifact_id
from perfpilot_api.services.memory_executions import (
    LoadedMemoryCapture,
    MemoryExecutionArtifact,
    MemoryExecutionNotFoundError,
    MemoryExecutionService,
    MemoryExecutionUnavailableError,
    canonical_memory_config_hash,
)
from perfpilot_api.services.uploads import DownloadAuthorization


TEAM_ID = UUID("61000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("62000000-0000-4000-8000-000000000001")
OTHER_ANALYSIS_ID = UUID("62000000-0000-4000-8000-000000000002")
CAPTURE_ID = UUID("63000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("64000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("65000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)


def _sha256_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _manifest() -> MemoryCaptureManifest:
    return MemoryCaptureManifest(
        schema_version="1.0",
        analysis_id=ANALYSIS_ID,
        capture_id=CAPTURE_ID,
        phase="single",
        source="manual_upload",
        subject=MemorySubject(package="com.example.app", android_sdk=37),
        artifacts=(MemoryArtifactRef(artifact_id=EVIDENCE_ID, role="meminfo"),),
    )


def _artifact(
    artifact_id: UUID,
    *,
    kind: str,
    payload: bytes,
    analysis_id: UUID = ANALYSIS_ID,
    state: str = "finalized",
    expires_at: datetime = NOW + timedelta(days=1),
    deleted_at: datetime | None = None,
) -> MemoryExecutionArtifact:
    return MemoryExecutionArtifact(
        artifact_id=artifact_id,
        analysis_id=analysis_id,
        artifact_kind=kind,
        mime_type="application/json" if kind == "memory_capture_manifest" else "text/plain",
        size_bytes=len(payload),
        sha256_b64=_sha256_b64(payload),
        version=2,
        state=state,
        expires_at=expires_at,
        deleted_at=deleted_at,
    )


def _capture() -> LoadedMemoryCapture:
    manifest = _manifest()
    payload = manifest.canonical_bytes()
    evidence_payload = b"meminfo"
    return LoadedMemoryCapture(
        analysis_id=ANALYSIS_ID,
        analysis_mode="memory_upload",
        analysis_state="created",
        tombstoned_at=None,
        tenant_resource_version=7,
        question="Where is retained memory?",
        manifest=manifest,
        manifest_bytes=payload,
        manifest_artifact=_artifact(
            manifest_artifact_id(CAPTURE_ID),
            kind="memory_capture_manifest",
            payload=payload,
        ),
        evidence_artifacts=(
            _artifact(EVIDENCE_ID, kind="memory_evidence", payload=evidence_payload),
        ),
    )


def _execution() -> EngineExecutionRecord:
    return EngineExecutionRecord(
        id=EXECUTION_ID,
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        engine_id="android_memory",
        attempt_number=1,
        adapter_version="1.0.0",
        engine_commit_sha="a" * 40,
        engine_image_digest="sha256:" + "b" * 64,
        input_manifest_hash="c" * 64,
        config_hash="d" * 64,
        external_workspace_id=None,
        external_session_id=None,
        external_run_id=None,
        state="pending",
        last_event_cursor=None,
        stable_error_code=None,
        started_at=None,
        completed_at=None,
        raw_result_artifact_id=None,
        normalized_report_version_id=None,
        version=1,
    )


class FakeRepository:
    def __init__(self, capture: LoadedMemoryCapture) -> None:
        self.capture = capture
        self.load_calls: list[dict[str, object]] = []
        self.fence_calls: list[tuple[UUID, int]] = []
        self.error: Exception | None = None
        self.rollover_after: int | None = None

    async def load_capture(self, **kwargs: object) -> LoadedMemoryCapture:
        self.load_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.capture

    async def require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None:
        self.fence_calls.append((team_id, expected_resource_version))
        if self.rollover_after is not None and len(self.fence_calls) > self.rollover_after:
            raise MemoryExecutionUnavailableError


class FakeUploads:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.expires_at = NOW + timedelta(minutes=5)
        self.authorization_overrides: dict[str, object] = {}

    async def download(self, **kwargs: object) -> DownloadAuthorization:
        self.calls.append(kwargs)
        artifact_id = kwargs["artifact_id"]
        assert isinstance(artifact_id, UUID)
        authorization = DownloadAuthorization(
            artifact_id=artifact_id,
            tenant_resource_version=7,
            artifact_version=2,
            artifact_kind=(
                "memory_capture_manifest"
                if artifact_id == manifest_artifact_id(CAPTURE_ID)
                else "memory_evidence"
            ),
            mime=(
                "application/json"
                if artifact_id == manifest_artifact_id(CAPTURE_ID)
                else "text/plain"
            ),
            size=(
                len(_manifest().canonical_bytes())
                if artifact_id == manifest_artifact_id(CAPTURE_ID)
                else len(b"meminfo")
            ),
            sha256_b64=(
                _sha256_b64(_manifest().canonical_bytes())
                if artifact_id == manifest_artifact_id(CAPTURE_ID)
                else _sha256_b64(b"meminfo")
            ),
            url=f"https://claims.invalid/{artifact_id}?secret=private",
            expires_at=self.expires_at,
        )
        return replace(authorization, **self.authorization_overrides)


class FakeExecutions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_attempt(self, **kwargs: object) -> EngineExecutionRecord:
        self.calls.append(kwargs)
        return _execution()


def _service(
    repository: FakeRepository,
    uploads: FakeUploads,
    executions: FakeExecutions,
) -> MemoryExecutionService:
    return MemoryExecutionService(
        repository=repository,
        upload_service=uploads,  # type: ignore[arg-type]
        engine_service=executions,  # type: ignore[arg-type]
        timeout_seconds=900,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_prepare_claims_manifest_first_and_creates_pinned_attempt() -> None:
    capture = _capture()
    repository = FakeRepository(capture)
    uploads = FakeUploads()
    executions = FakeExecutions()

    prepared = await _service(repository, uploads, executions).prepare(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        capture_id=CAPTURE_ID,
    )

    assert prepared.execution.id == EXECUTION_ID
    assert prepared.question == capture.question
    assert tuple(item.artifact_id for item in prepared.inputs) == (
        manifest_artifact_id(CAPTURE_ID),
        EVIDENCE_ID,
    )
    assert all(isinstance(item.download_url, SecretStr) for item in prepared.inputs)
    assert "secret=private" not in repr(prepared)
    assert uploads.calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": manifest_artifact_id(CAPTURE_ID),
            "expected_tenant_resource_version": 7,
            "expected_artifact_version": 2,
        },
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": EVIDENCE_ID,
            "expected_tenant_resource_version": 7,
            "expected_artifact_version": 2,
        },
    ]
    assert executions.calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "engine_id": "android_memory",
            "input_manifest_hash": capture.manifest.sha256_hex(),
            "config_hash": canonical_memory_config_hash(
                capture_id=CAPTURE_ID,
                question=capture.question,
                timeout_seconds=900,
            ),
        }
    ]
    assert repository.fence_calls == [(TEAM_ID, 7), (TEAM_ID, 7), (TEAM_ID, 7)]


@pytest.mark.parametrize(
    "capture",
    [
        replace(_capture(), analysis_id=OTHER_ANALYSIS_ID),
        replace(_capture(), analysis_mode="trace_upload"),
        replace(_capture(), analysis_state="deleted"),
        replace(_capture(), tombstoned_at=NOW),
        replace(
            _capture(),
            manifest_artifact=replace(_capture().manifest_artifact, state="pending"),
        ),
        replace(
            _capture(),
            manifest_artifact=replace(_capture().manifest_artifact, expires_at=NOW),
        ),
        replace(
            _capture(),
            evidence_artifacts=(replace(_capture().evidence_artifacts[0], deleted_at=NOW),),
        ),
        replace(
            _capture(),
            evidence_artifacts=(
                replace(_capture().evidence_artifacts[0], analysis_id=OTHER_ANALYSIS_ID),
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_owner_state_or_artifact_metadata_is_stable_not_found(
    capture: LoadedMemoryCapture,
) -> None:
    repository = FakeRepository(capture)
    uploads = FakeUploads()
    executions = FakeExecutions()

    with pytest.raises(MemoryExecutionNotFoundError) as caught:
        await _service(repository, uploads, executions).prepare(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )

    assert str(caught.value) == "memory capture was not found"
    assert uploads.calls == []
    assert executions.calls == []


@pytest.mark.asyncio
async def test_manifest_hash_mismatch_and_noncanonical_bytes_are_rejected() -> None:
    for capture in (
        replace(
            _capture(),
            manifest_artifact=replace(_capture().manifest_artifact, sha256_b64="A" * 44),
        ),
        replace(_capture(), manifest_bytes=b" " + _capture().manifest_bytes),
    ):
        repository = FakeRepository(capture)
        with pytest.raises(MemoryExecutionNotFoundError):
            await _service(repository, FakeUploads(), FakeExecutions()).prepare(
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                capture_id=CAPTURE_ID,
            )


@pytest.mark.asyncio
async def test_expired_claim_and_resource_rollover_fail_closed() -> None:
    uploads = FakeUploads()
    uploads.expires_at = NOW
    with pytest.raises(MemoryExecutionUnavailableError) as expired:
        await _service(FakeRepository(_capture()), uploads, FakeExecutions()).prepare(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )
    assert str(expired.value) == "memory execution service is unavailable"

    repository = FakeRepository(_capture())
    repository.rollover_after = 1
    executions = FakeExecutions()
    with pytest.raises(MemoryExecutionUnavailableError):
        await _service(repository, FakeUploads(), executions).prepare(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )
    assert executions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_resource_version": 8},
        {"artifact_version": 3},
        {"artifact_kind": "trace"},
        {"mime": "application/octet-stream"},
        {"size": 999},
        {"sha256_b64": "A" * 44},
    ],
)
async def test_claim_metadata_must_match_the_pinned_artifact(
    overrides: dict[str, object],
) -> None:
    uploads = FakeUploads()
    uploads.authorization_overrides = overrides
    executions = FakeExecutions()

    with pytest.raises(MemoryExecutionUnavailableError):
        await _service(FakeRepository(_capture()), uploads, executions).prepare(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )

    assert executions.calls == []


@pytest.mark.asyncio
async def test_other_tenant_repository_failure_is_redacted() -> None:
    repository = FakeRepository(_capture())
    repository.error = MemoryExecutionNotFoundError("private bucket and object key")

    with pytest.raises(MemoryExecutionNotFoundError) as caught:
        await _service(repository, FakeUploads(), FakeExecutions()).prepare(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
        )

    assert str(caught.value) == "memory capture was not found"
    assert "private" not in repr(caught.value)
