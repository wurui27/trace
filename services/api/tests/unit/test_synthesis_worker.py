from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.ai.openai_compatible import AIProviderError, SynthesisCandidate
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.services.synthesis_executions import (
    SynthesisExecutionRecord,
    SynthesisSourceRecord,
)
from perfpilot_api.workers.synthesis_orchestrator import (
    SynthesisAnalysisContext,
    SynthesisPipeline,
    SynthesisWorkClaim,
)


ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
TEAM_ID = UUID("11000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("81000000-0000-4000-8000-000000000001")
SYNTHESIS_ID = UUID("22000000-0000-4000-8000-000000000001")
CANONICAL_ID = UUID("85000000-0000-4000-8000-000000000001")
CHECKSUM = base64.b64encode(b"c" * 32).decode("ascii")
PROMPT_CHECKSUM = base64.b64encode(b"p" * 32).decode("ascii")


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts" / "v1" / "examples" / name).read_text(encoding="utf-8")
    )


def _core() -> NormalizedTraceReport:
    payload = canonical_json_bytes(_load("normalized-trace-report.valid.json"))
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
    )


def _record() -> SynthesisExecutionRecord:
    return SynthesisExecutionRecord(
        id=SYNTHESIS_ID,
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        source_execution_id=SOURCE_ID,
        tenant_resource_version=7,
        generation=1,
        state="pending",
        request_fingerprint="a" * 64,
        normalizer_version="smartperfetto-normalizer-1",
        report_worker_image_digest="sha256:" + "1" * 64,
        projection_sha256_b64=CHECKSUM,
        projection_artifact_id=None,
        provider_protocol="chat-completions-json-schema-v1",
        provider_name="fake",
        provider_model="fake-model",
        prompt_template_version="perfpilot-synthesis-v1",
        prompt_template_sha256_b64=PROMPT_CHECKSUM,
        attempt_count=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        latency_ms=None,
        stable_error_code=None,
        last_invocation_error_code=None,
        candidate_artifact_id=None,
        candidate_sha256_b64=None,
        report_generated_at=None,
        report_version_id=None,
        version=1,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.record = _record()
        self.source = SynthesisSourceRecord(
            id=SOURCE_ID,
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            engine_id="smartperfetto",
            attempt_number=1,
            tenant_resource_version=7,
            adapter_version="1.0.0",
            engine_commit_sha="1" * 40,
            engine_image_digest="sha256:" + "1" * 64,
            state="completed",
            raw_result_artifact_id=CANONICAL_ID,
            normalized_report_version_id=None,
            version=1,
        )

    async def load(self, **_: object) -> SynthesisExecutionRecord:
        return self.record

    async def load_source(self, **_: object) -> SynthesisSourceRecord:
        return self.source

    async def bind_projection(self, *, artifact_id: UUID, sha256_b64: str, **_: object):
        self.record = replace(
            self.record,
            projection_artifact_id=artifact_id,
            projection_sha256_b64=sha256_b64,
            version=self.record.version + 1,
        )
        return self.record

    async def begin_invocation(self, **_: object) -> int:
        attempt = self.record.attempt_count + 1
        self.record = replace(
            self.record,
            state="running",
            attempt_count=attempt,
            version=self.record.version + 1,
        )
        return attempt

    async def finish_invocation_failure(
        self,
        *,
        stable_error_code: str,
        exhausted: bool,
        generated_at: datetime | None,
        latency_ms: int | None,
        **_: object,
    ):
        self.record = replace(
            self.record,
            stable_error_code=stable_error_code if exhausted else None,
            last_invocation_error_code=stable_error_code,
            report_generated_at=generated_at if exhausted else None,
            latency_ms=latency_ms,
            version=self.record.version + 1,
        )
        return self.record

    async def bind_candidate_result(
        self,
        *,
        artifact_id: UUID,
        sha256_b64: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        generated_at: datetime,
        **_: object,
    ):
        self.record = replace(
            self.record,
            candidate_artifact_id=artifact_id,
            candidate_sha256_b64=sha256_b64,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            report_generated_at=generated_at,
            stable_error_code=None,
            version=self.record.version + 1,
        )
        return self.record

    async def bind_report_timestamp(self, *, generated_at: datetime, **_: object):
        self.record = replace(self.record, report_generated_at=generated_at)
        return self.record

    async def bind_source_report(self, *, report_version_id: UUID, **_: object):
        self.source = replace(
            self.source,
            normalized_report_version_id=report_version_id,
            version=self.source.version + 1,
        )
        return self.source

    async def bind_report(
        self, *, report_version_id: UUID, synthesis_succeeded: bool, **_: object
    ):
        self.record = replace(
            self.record,
            report_version_id=report_version_id,
            state="succeeded" if synthesis_succeeded else "failed",
            version=self.record.version + 1,
        )
        return self.record

    async def fail_without_report(self, *, stable_error_code: str, **_: object):
        self.record = replace(
            self.record, state="failed", stable_error_code=stable_error_code
        )
        return self.record


class FakeCanonicalReader:
    async def read(self, _source: object):
        return SimpleNamespace(
            artifact_id=CANONICAL_ID,
            sha256_b64=CHECKSUM,
        )


class FakeArtifactStore:
    def __init__(self) -> None:
        self.values: dict[UUID, bytes] = {}

    async def write(self, request):
        self.values.setdefault(request.artifact_id, request.canonical_bytes)
        return SimpleNamespace(artifact_id=request.artifact_id)

    async def read(self, *, artifact_id: UUID, **_: object):
        return SimpleNamespace(canonical_bytes=self.values[artifact_id])


class FakeProvider:
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.outcomes = outcomes or [_load("synthesis-output.valid.json")]
        self.retry_codes: list[str | None] = []

    async def synthesize(self, _projection, *, retry_code: str | None = None):
        self.retry_codes.append(retry_code)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        payload = outcome if isinstance(outcome, bytes) else canonical_json_bytes(outcome)
        return SynthesisCandidate(
            candidate_json=payload,
            prompt_tokens=100,
            completion_tokens=200,
            latency_ms=25,
        )


class FakeWriter:
    def __init__(self) -> None:
        self.identities: set[UUID] = set()
        self.requests: list[object] = []

    async def publish(self, request):
        from perfpilot_api.reports.writer import report_version_id

        identity = report_version_id(request.synthesis_execution_id)
        self.identities.add(identity)
        self.requests.append(request)
        return SimpleNamespace(id=identity)


class FakeContexts:
    async def load(self, **_: object) -> SynthesisAnalysisContext:
        return SynthesisAnalysisContext("auto", None)


class FakeProjector:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def project(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "completed"


def _claim() -> SynthesisWorkClaim:
    return SynthesisWorkClaim(
        claim_id=UUID("33000000-0000-4000-8000-000000000001"),
        event_id=UUID("34000000-0000-4000-8000-000000000001"),
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        synthesis_execution_id=SYNTHESIS_ID,
        consumer_id="worker-1",
        token=SecretStr("x" * 32),
        expires_at=NOW.replace(year=2027),
    )


def _pipeline(
    repository: FakeRepository,
    provider: FakeProvider,
    artifacts: FakeArtifactStore,
    writer: FakeWriter,
    projector: FakeProjector,
    checkpoint=None,
) -> SynthesisPipeline:
    return SynthesisPipeline(
        repository=repository,  # type: ignore[arg-type]
        canonical_reader=FakeCanonicalReader(),
        artifact_store=artifacts,
        provider=provider,
        report_writer=writer,  # type: ignore[arg-type]
        analysis_contexts=FakeContexts(),
        parent_projector=projector,
        clock=lambda: NOW,
        checkpoint=checkpoint,
        normalizer=lambda _loaded: _core(),
    )


async def _finish(pipeline: SynthesisPipeline, *, limit: int = 10):
    for _ in range(limit):
        result = await pipeline.advance(_claim())
        if result.state in {"succeeded", "failed", "canceled"}:
            return result
    raise AssertionError("pipeline did not terminate")


@pytest.mark.asyncio
async def test_pipeline_converges_on_one_report_and_does_not_repeat_provider_after_binding() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    artifacts = FakeArtifactStore()
    writer = FakeWriter()
    projector = FakeProjector()
    crashed = False

    async def checkpoint(name: str) -> None:
        nonlocal crashed
        if name == "candidate_binding" and not crashed:
            crashed = True
            raise RuntimeError("injected crash")

    pipeline = _pipeline(repository, provider, artifacts, writer, projector, checkpoint)
    assert (await pipeline.advance(_claim())).state == "running"
    with pytest.raises(RuntimeError, match="injected crash"):
        await pipeline.advance(_claim())

    result = await _finish(pipeline)

    assert result.state == "succeeded"
    assert len(provider.retry_codes) == 1
    assert len(writer.identities) == 1
    assert repository.record.report_generated_at == NOW
    assert projector.calls[-1]["terminal"] == "report"


@pytest.mark.asyncio
async def test_invalid_candidate_retries_once_with_only_stable_retry_code() -> None:
    repository = FakeRepository()
    provider = FakeProvider([b"{}", _load("synthesis-output.valid.json")])
    pipeline = _pipeline(
        repository,
        provider,
        FakeArtifactStore(),
        FakeWriter(),
        FakeProjector(),
    )

    result = await _finish(pipeline)

    assert result.state == "succeeded"
    assert provider.retry_codes == [None, "ai_output_invalid"]
    assert repository.record.attempt_count == 2


@pytest.mark.asyncio
async def test_nonretryable_provider_failure_publishes_partial_core_report() -> None:
    repository = FakeRepository()
    provider = FakeProvider(
        [AIProviderError("ai_authentication_failed", retryable=False)]
    )
    writer = FakeWriter()
    pipeline = _pipeline(
        repository,
        provider,
        FakeArtifactStore(),
        writer,
        FakeProjector(),
    )

    result = await _finish(pipeline)

    assert result.state == "failed"
    assert len(provider.retry_codes) == 1
    assert writer.requests[-1].synthesis_document is None
    assert writer.requests[-1].synthesis_failure_code == "ai_authentication_failed"
    assert repository.record.report_version_id is not None
