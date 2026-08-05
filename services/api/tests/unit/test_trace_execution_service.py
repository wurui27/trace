from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.engines.contracts import EngineStepOutcome
from perfpilot_api.services.engine_executions import EngineExecutionRecord
from perfpilot_api.services.trace_executions import (
    LoadedTraceAnalysis,
    TraceExecutionArtifact,
    TraceExecutionService,
    canonical_trace_config_hash,
    canonical_trace_input_manifest_hash,
)
from perfpilot_api.services.uploads import DownloadAuthorization


TEAM_ID = UUID("81000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
TRACE_ID = UUID("83000000-0000-4000-8000-000000000001")
MAPPING_ID = UUID("83000000-0000-4000-8000-000000000002")
EXECUTION_ID = UUID("84000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
TRACE_CHECKSUM = base64.b64encode(hashlib.sha256(b"trace").digest()).decode("ascii")
MAPPING_CHECKSUM = base64.b64encode(hashlib.sha256(b"mapping").digest()).decode("ascii")


def _artifact(
    artifact_id: UUID,
    *,
    kind: str,
    mime: str,
    size: int,
    checksum: str,
    state: str,
) -> TraceExecutionArtifact:
    return TraceExecutionArtifact(
        artifact_id=artifact_id,
        analysis_id=ANALYSIS_ID,
        artifact_kind=kind,
        mime_type=mime,
        size_bytes=size,
        sha256_b64=checksum,
        version=2 if state == "finalized" else 1,
        state=state,
        expires_at=NOW + timedelta(days=1),
        deleted_at=None,
    )


def _loaded(*, latest: EngineExecutionRecord | None = None) -> LoadedTraceAnalysis:
    return LoadedTraceAnalysis(
        analysis_id=ANALYSIS_ID,
        analysis_mode="trace_upload",
        analysis_state="uploading",
        tombstoned_at=None,
        tenant_resource_version=7,
        analysis_profile="scroll",
        question="为什么掉帧？",
        input_manifest=(
            {
                "kind": "trace",
                "mime": "application/octet-stream",
                "size": 5,
                "sha256_b64": TRACE_CHECKSUM,
            },
            {
                "kind": "mapping",
                "mime": "text/plain",
                "size": 7,
                "sha256_b64": MAPPING_CHECKSUM,
            },
        ),
        input_artifacts=(
            _artifact(
                TRACE_ID,
                kind="trace",
                mime="application/octet-stream",
                size=5,
                checksum=TRACE_CHECKSUM,
                state="finalized",
            ),
            _artifact(
                MAPPING_ID,
                kind="mapping",
                mime="text/plain",
                size=7,
                checksum=MAPPING_CHECKSUM,
                state="pending",
            ),
        ),
        latest_execution=latest,
    )


def _execution(
    *,
    state: str = "pending",
    input_manifest_hash: str | None = None,
    config_hash: str | None = None,
    stable_error_code: str | None = None,
) -> EngineExecutionRecord:
    trace = _loaded().input_artifacts[0]
    return EngineExecutionRecord(
        id=EXECUTION_ID,
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        engine_id="smartperfetto",
        attempt_number=1,
        tenant_resource_version=7,
        adapter_version="1.0.0",
        engine_commit_sha="a" * 40,
        engine_image_digest="sha256:" + "b" * 64,
        input_manifest_hash=input_manifest_hash
        or canonical_trace_input_manifest_hash((trace,)),
        config_hash=config_hash
        or canonical_trace_config_hash(
            analysis_profile="scroll",
            question="为什么掉帧？",
            timeout_seconds=1_800,
        ),
        external_workspace_id="workspace-1" if state != "pending" else None,
        external_session_id="session-1" if state != "pending" else None,
        external_run_id="run-1" if state != "pending" else None,
        state=state,  # type: ignore[arg-type]
        last_event_cursor=None,
        stable_error_code=stable_error_code,
        started_at=NOW if state != "pending" else None,
        completed_at=NOW if state in {"completed", "insufficient_data", "failed"} else None,
        raw_result_artifact_id=None,
        normalized_report_version_id=None,
        version=1,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.latest: EngineExecutionRecord | None = None
        self.fence_calls: list[tuple[UUID, int]] = []
        self.projections: list[dict[str, object]] = []

    async def load_analysis(self, **_: object) -> LoadedTraceAnalysis:
        return _loaded(latest=self.latest)

    async def require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None:
        self.fence_calls.append((team_id, expected_resource_version))

    async def load_execution(self, **_: object) -> EngineExecutionRecord:
        assert self.latest is not None
        return self.latest

    async def project_parent(self, **kwargs: object) -> None:
        self.projections.append(kwargs)


class FakeUploads:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def download(self, **kwargs: object) -> DownloadAuthorization:
        self.calls.append(kwargs)
        return DownloadAuthorization(
            artifact_id=TRACE_ID,
            tenant_resource_version=7,
            artifact_version=2,
            artifact_kind="trace",
            mime="application/octet-stream",
            size=5,
            sha256_b64=TRACE_CHECKSUM,
            url="https://claims.invalid/trace?secret=private",
            expires_at=NOW + timedelta(minutes=5),
        )


class FakeEngineExecutions:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.create_calls: list[dict[str, object]] = []
        self.submit_calls: list[dict[str, object]] = []
        self.step_calls: list[dict[str, object]] = []
        self.step_state = "completed"

    async def create_attempt(self, **kwargs: object) -> EngineExecutionRecord:
        self.create_calls.append(kwargs)
        self.repository.latest = _execution(
            input_manifest_hash=str(kwargs["input_manifest_hash"]),
            config_hash=str(kwargs["config_hash"]),
        )
        return self.repository.latest

    async def submit_attempt(self, **kwargs: object) -> EngineExecutionRecord:
        self.submit_calls.append(kwargs)
        self.repository.latest = replace(
            _execution(state="running"),
            version=2,
        )
        return self.repository.latest

    async def step(self, **kwargs: object) -> EngineStepOutcome:
        self.step_calls.append(kwargs)
        self.repository.latest = _execution(state=self.step_state)
        return EngineStepOutcome(EXECUTION_ID, self.step_state, None)  # type: ignore[arg-type]


def _service(
    repository: FakeRepository,
    uploads: FakeUploads,
    executions: FakeEngineExecutions,
) -> TraceExecutionService:
    return TraceExecutionService(
        repository=repository,
        upload_service=uploads,  # type: ignore[arg-type]
        engine_service=executions,  # type: ignore[arg-type]
        timeout_seconds=1_800,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_prepare_claims_only_supported_finalized_trace_and_reuses_one_attempt() -> None:
    repository = FakeRepository()
    uploads = FakeUploads()
    executions = FakeEngineExecutions(repository)
    service = _service(repository, uploads, executions)

    first = await service.prepare(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)
    second = await service.prepare(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert first.execution == second.execution == _execution()
    assert len(first.inputs) == len(second.inputs) == 1
    assert first.inputs[0].kind == "trace"
    assert isinstance(first.inputs[0].download_url, SecretStr)
    assert "secret=private" not in repr(first)
    assert executions.create_calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "engine_id": "smartperfetto",
            "tenant_resource_version": 7,
            "input_manifest_hash": canonical_trace_input_manifest_hash(
                (_loaded().input_artifacts[0],)
            ),
            "config_hash": canonical_trace_config_hash(
                analysis_profile="scroll",
                question="为什么掉帧？",
                timeout_seconds=1_800,
            ),
        }
    ]
    assert len(uploads.calls) == 2
    assert all(call["artifact_id"] == TRACE_ID for call in uploads.calls)
    assert repository.fence_calls == [(TEAM_ID, 7)] * 6


@pytest.mark.asyncio
async def test_device_capture_aliases_finalized_startup_trace_for_smartperfetto() -> None:
    source = _artifact(
        TRACE_ID,
        kind="trace",
        mime="application/x-perfetto-trace",
        size=5,
        checksum=TRACE_CHECKSUM,
        state="finalized",
    )
    source = replace(source, source_artifact_kind="startup_trace")

    class DeviceRepository(FakeRepository):
        async def load_analysis(self, **_: object) -> LoadedTraceAnalysis:
            return LoadedTraceAnalysis(
                analysis_id=ANALYSIS_ID,
                analysis_mode="device",
                analysis_state="analyzing",
                tombstoned_at=None,
                tenant_resource_version=7,
                analysis_profile="startup",
                question=None,
                input_manifest=(
                    {
                        "kind": "trace",
                        "mime": "application/x-perfetto-trace",
                        "size": 5,
                        "sha256_b64": TRACE_CHECKSUM,
                    },
                ),
                input_artifacts=(source,),
                latest_execution=self.latest,
            )

    class DeviceUploads(FakeUploads):
        async def download(self, **kwargs: object) -> DownloadAuthorization:
            self.calls.append(kwargs)
            return DownloadAuthorization(
                artifact_id=TRACE_ID,
                tenant_resource_version=7,
                artifact_version=2,
                artifact_kind="startup_trace",
                mime="application/x-perfetto-trace",
                size=5,
                sha256_b64=TRACE_CHECKSUM,
                url="https://claims.invalid/device-trace",
                expires_at=NOW + timedelta(minutes=5),
            )

    repository = DeviceRepository()
    uploads = DeviceUploads()
    executions = FakeEngineExecutions(repository)
    service = _service(repository, uploads, executions)

    prepared = await service.prepare(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert prepared.analysis_profile == "startup"
    assert len(prepared.inputs) == 1
    assert prepared.inputs[0].kind == "trace"
    assert uploads.calls[0]["artifact_id"] == TRACE_ID


@pytest.mark.asyncio
async def test_advance_submits_pending_then_resumes_running_without_another_attempt() -> None:
    repository = FakeRepository()
    uploads = FakeUploads()
    executions = FakeEngineExecutions(repository)
    service = _service(repository, uploads, executions)

    submitted = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)
    assert submitted.state == "running"
    assert len(executions.create_calls) == 1
    assert len(executions.submit_calls) == 1
    assert repository.projections[-1]["target_state"] == "analyzing"

    completed = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)
    assert completed.state == "completed"
    assert len(executions.create_calls) == 1
    assert len(executions.submit_calls) == 1
    assert len(executions.step_calls) == 1
    assert repository.projections[-1] == {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "target_state": "completed",
        "failure_code": None,
        "now": NOW,
    }


@pytest.mark.parametrize(
    ("engine_state", "target_state", "failure_code"),
    [
        ("insufficient_data", "partially_completed", None),
        ("failed", "failed", "engine_failed"),
        ("canceled", "canceled", None),
    ],
)
@pytest.mark.asyncio
async def test_terminal_engine_states_project_stable_parent_states(
    engine_state: str,
    target_state: str,
    failure_code: str | None,
) -> None:
    repository = FakeRepository()
    repository.latest = _execution(
        state=engine_state,
        stable_error_code="engine_failed" if engine_state == "failed" else None,
    )
    service = _service(repository, FakeUploads(), FakeEngineExecutions(repository))

    outcome = await service.advance(team_id=TEAM_ID, analysis_id=ANALYSIS_ID)

    assert outcome.state == engine_state
    assert repository.projections[-1]["target_state"] == target_state
    assert repository.projections[-1]["failure_code"] == failure_code
