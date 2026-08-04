"""Durable, non-secret control-plane records for AI synthesis generations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import (
    AIInvocation,
    EngineExecution,
    IdempotencyKey,
    OutboxEvent,
    SynthesisExecution,
    TenantResource,
    WorkerClaim,
)
from perfpilot_api.reports.projection import normalize_authoritative_question


_NORMALIZER_VERSION = "smartperfetto-normalizer-1"
_PROJECTION_CONTRACT_VERSION = "1.0"
_REPORT_CONTRACT_VERSION = "1.1"
_PROVIDER_PROTOCOL = "chat-completions-json-schema-v1"
_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")


class SynthesisExecutionError(RuntimeError):
    """Stable synthesis control-plane error."""


class SynthesisExecutionNotFoundError(SynthesisExecutionError):
    pass


class SynthesisIdempotencyConflictError(SynthesisExecutionError):
    pass


class SynthesisLeaseLostError(SynthesisExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesisMutationFence:
    claim_id: UUID
    event_id: UUID
    consumer_id: str
    token: SecretStr


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    canonical_sha256_b64: str
    tenant_resource_version: int
    question: str | None
    normalizer_version: str
    prompt_template_version: str
    prompt_template_sha256_b64: str
    report_worker_image_digest: str
    provider_name: str
    model: str
    inference_config_hash: str
    projection_sha256_b64: str
    generation: int
    provider_protocol: str = _PROVIDER_PROTOCOL


@dataclass(frozen=True, slots=True)
class SynthesisExecutionRecord:
    id: UUID
    team_id: UUID
    analysis_id: UUID
    source_execution_id: UUID
    tenant_resource_version: int
    generation: int
    state: str
    request_fingerprint: str
    normalizer_version: str
    report_worker_image_digest: str
    projection_sha256_b64: str
    projection_artifact_id: UUID | None
    provider_protocol: str
    provider_name: str
    provider_model: str
    prompt_template_version: str
    prompt_template_sha256_b64: str
    attempt_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None
    stable_error_code: str | None
    last_invocation_error_code: str | None
    candidate_artifact_id: UUID | None
    candidate_sha256_b64: str | None
    report_generated_at: datetime | None
    report_version_id: UUID | None
    version: int


@dataclass(frozen=True, slots=True)
class SynthesisSourceRecord:
    id: UUID
    team_id: UUID
    analysis_id: UUID
    engine_id: str
    attempt_number: int
    tenant_resource_version: int
    adapter_version: str
    engine_commit_sha: str
    engine_image_digest: str
    state: str
    raw_result_artifact_id: UUID | None
    normalized_report_version_id: UUID | None
    version: int


def _checksum(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError("canonical checksum is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("canonical checksum is invalid")
    return value


def _canonical_hash(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError):
        raise ValueError("synthesis request is invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def synthesis_request_fingerprint(
    *, canonical_sha256_b64: str, tenant_resource_version: int, question: str | None,
    normalizer_version: str, prompt_template_version: str, prompt_template_sha256_b64: str,
    report_worker_image_digest: str, provider_protocol: str, provider_name: str,
    model: str, inference_config_hash: str, generation: int,
) -> str:
    """Hash the reviewed allow-list; secrets and request bodies never enter control DB."""
    normalized_question = normalize_authoritative_question(question)
    if type(tenant_resource_version) is not int or tenant_resource_version < 1 or type(generation) is not int or generation < 1:
        raise ValueError("synthesis request is invalid")
    if not all(isinstance(item, str) and item for item in (normalizer_version, prompt_template_version, report_worker_image_digest, provider_protocol, provider_name, model, inference_config_hash)):
        raise ValueError("synthesis request is invalid")
    return _canonical_hash({
        "canonical_sha256_b64": _checksum(canonical_sha256_b64),
        "tenant_resource_version": tenant_resource_version,
        "question_sha256": hashlib.sha256((normalized_question or "").encode("utf-8")).hexdigest(),
        "normalizer_version": normalizer_version,
        "projection_contract_version": _PROJECTION_CONTRACT_VERSION,
        "report_contract_version": _REPORT_CONTRACT_VERSION,
        "prompt_template_version": prompt_template_version,
        "prompt_template_sha256_b64": _checksum(prompt_template_sha256_b64),
        "report_worker_image_digest": report_worker_image_digest,
        "provider_protocol": provider_protocol,
        "provider_name": provider_name,
        "model": model,
        "inference_config_hash": inference_config_hash,
        "generation": generation,
    })


class SQLAlchemySynthesisExecutionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    @staticmethod
    def _record(
        row: SynthesisExecution,
        *,
        last_invocation_error_code: str | None = None,
    ) -> SynthesisExecutionRecord:
        return SynthesisExecutionRecord(
            id=row.id, team_id=row.team_id, analysis_id=row.analysis_id,
            source_execution_id=row.source_execution_id, tenant_resource_version=row.tenant_resource_version,
            generation=row.generation, state=row.state, request_fingerprint=row.request_fingerprint,
            normalizer_version=row.normalizer_version,
            report_worker_image_digest=row.report_worker_image_digest,
            projection_sha256_b64=row.projection_sha256_b64,
            projection_artifact_id=row.projection_artifact_id, attempt_count=row.attempt_count,
            provider_protocol=row.provider_protocol, provider_name=row.provider_name,
            provider_model=row.provider_model, prompt_template_version=row.prompt_template_version,
            prompt_template_sha256_b64=row.prompt_template_sha256_b64,
            prompt_tokens=row.prompt_tokens, completion_tokens=row.completion_tokens,
            total_tokens=row.total_tokens, latency_ms=row.latency_ms,
            stable_error_code=row.stable_error_code,
            last_invocation_error_code=last_invocation_error_code,
            candidate_artifact_id=row.candidate_artifact_id, candidate_sha256_b64=row.candidate_sha256_b64,
            report_generated_at=row.report_generated_at, report_version_id=row.report_version_id, version=row.version,
        )

    @staticmethod
    def _source(row: EngineExecution) -> SynthesisSourceRecord:
        return SynthesisSourceRecord(
            id=row.id,
            team_id=row.team_id,
            analysis_id=row.analysis_id,
            engine_id=row.engine_id,
            attempt_number=row.attempt_number,
            tenant_resource_version=row.tenant_resource_version,
            adapter_version=row.adapter_version,
            engine_commit_sha=row.engine_commit_sha,
            engine_image_digest=row.engine_image_digest,
            state=row.state,
            raw_result_artifact_id=row.raw_result_artifact_id,
            normalized_report_version_id=row.normalized_report_version_id,
            version=row.version,
        )

    async def load(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID
    ) -> SynthesisExecutionRecord:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SynthesisExecution).where(
                    SynthesisExecution.id == execution_id,
                    SynthesisExecution.team_id == team_id,
                    SynthesisExecution.analysis_id == analysis_id,
                )
            )
            if row is None:
                raise SynthesisExecutionNotFoundError("synthesis execution was not found")
            invocation = await session.scalar(
                select(AIInvocation)
                .where(AIInvocation.synthesis_execution_id == execution_id)
                .order_by(AIInvocation.attempt_number.desc())
                .limit(1)
            )
            return self._record(
                row,
                last_invocation_error_code=(
                    invocation.stable_error_code if invocation is not None else None
                ),
            )

    async def load_generation(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        source_execution_id: UUID,
        generation: int,
    ) -> SynthesisExecutionRecord | None:
        if type(generation) is not int or generation < 1:
            raise ValueError("synthesis generation is invalid")
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SynthesisExecution).where(
                    SynthesisExecution.team_id == team_id,
                    SynthesisExecution.analysis_id == analysis_id,
                    SynthesisExecution.source_execution_id == source_execution_id,
                    SynthesisExecution.generation == generation,
                )
            )
            if row is None:
                return None
            invocation = await session.scalar(
                select(AIInvocation)
                .where(AIInvocation.synthesis_execution_id == row.id)
                .order_by(AIInvocation.attempt_number.desc())
                .limit(1)
            )
            return self._record(
                row,
                last_invocation_error_code=(
                    invocation.stable_error_code if invocation is not None else None
                ),
            )

    async def load_source(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID
    ) -> SynthesisSourceRecord:
        async with self._session_factory() as session:
            synthesis = await session.scalar(
                select(SynthesisExecution).where(
                    SynthesisExecution.id == execution_id,
                    SynthesisExecution.team_id == team_id,
                    SynthesisExecution.analysis_id == analysis_id,
                )
            )
            if synthesis is None:
                raise SynthesisExecutionNotFoundError("synthesis execution was not found")
            row = await session.scalar(
                select(EngineExecution).where(
                    EngineExecution.id == synthesis.source_execution_id,
                    EngineExecution.team_id == team_id,
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
            )
            if row is None or row.state not in {"completed", "insufficient_data"}:
                raise SynthesisExecutionNotFoundError("source execution is not authoritative")
            return self._source(row)

    async def allocate(
        self, *, team_id: UUID, analysis_id: UUID, source_execution_id: UUID,
        request: SynthesisRequest, now: datetime,
        mode: Literal["auto", "manual"] = "auto",
        idempotency_key: str | None = None,
    ) -> SynthesisExecutionRecord:
        fingerprint = synthesis_request_fingerprint(
            canonical_sha256_b64=request.canonical_sha256_b64, tenant_resource_version=request.tenant_resource_version,
            question=request.question, normalizer_version=request.normalizer_version,
            prompt_template_version=request.prompt_template_version, prompt_template_sha256_b64=request.prompt_template_sha256_b64,
            report_worker_image_digest=request.report_worker_image_digest, provider_protocol=request.provider_protocol,
            provider_name=request.provider_name, model=request.model, inference_config_hash=request.inference_config_hash,
            generation=request.generation,
        )
        if mode not in {"auto", "manual"}:
            raise ValueError("synthesis allocation mode is invalid")
        if mode == "auto" and (request.generation != 1 or idempotency_key is not None):
            raise SynthesisIdempotencyConflictError("automatic synthesis must be generation one")
        if mode == "manual" and not idempotency_key:
            raise ValueError("manual synthesis idempotency key is required")
        async with self._session_factory.begin() as session:
            source = await session.scalar(select(EngineExecution).where(
                EngineExecution.id == source_execution_id, EngineExecution.team_id == team_id,
                EngineExecution.analysis_id == analysis_id).with_for_update())
            latest = await session.scalar(select(EngineExecution).where(
                EngineExecution.team_id == team_id, EngineExecution.analysis_id == analysis_id,
                EngineExecution.engine_id == "smartperfetto").order_by(EngineExecution.attempt_number.desc()).limit(1).with_for_update())
            tenant = await session.scalar(select(TenantResource).where(
                TenantResource.team_id == team_id, TenantResource.state.in_(("active", "migrating"))).with_for_update())
            if (source is None or latest is None or latest.id != source.id or source.engine_id != "smartperfetto"
                    or source.state not in {"completed", "insufficient_data"} or source.raw_result_artifact_id is None
                    or tenant is None or tenant.resource_version != source.tenant_resource_version
                    or request.tenant_resource_version != source.tenant_resource_version):
                raise SynthesisExecutionNotFoundError("source execution is not authoritative")
            if mode == "manual":
                key = await session.scalar(select(IdempotencyKey).where(
                    IdempotencyKey.operation == "create_synthesis_run",
                    IdempotencyKey.scope_type == "team",
                    IdempotencyKey.scope_id == team_id,
                    IdempotencyKey.key == idempotency_key,
                ).with_for_update())
                if key is not None:
                    if not hmac.compare_digest(key.request_hash, fingerprint) or key.response_resource_id is None:
                        raise SynthesisIdempotencyConflictError("synthesis idempotency key changed")
                    replay = await session.get(SynthesisExecution, key.response_resource_id)
                    if replay is None or replay.team_id != team_id or replay.analysis_id != analysis_id:
                        raise SynthesisExecutionNotFoundError("synthesis replay is unavailable")
                    return self._record(replay)
            existing = await session.scalar(select(SynthesisExecution).where(
                SynthesisExecution.analysis_id == analysis_id, SynthesisExecution.source_execution_id == source_execution_id,
                SynthesisExecution.generation == request.generation).with_for_update())
            if existing is not None:
                if mode == "manual":
                    raise SynthesisIdempotencyConflictError("manual synthesis generation is occupied")
                if not hmac.compare_digest(existing.request_fingerprint, fingerprint):
                    raise SynthesisIdempotencyConflictError("synthesis request changed")
                return self._record(existing)
            if mode == "manual":
                maximum = await session.scalar(
                    select(func.max(SynthesisExecution.generation)).where(
                        SynthesisExecution.analysis_id == analysis_id,
                        SynthesisExecution.source_execution_id == source_execution_id,
                    )
                )
                if request.generation != (maximum or 0) + 1:
                    raise SynthesisIdempotencyConflictError("manual synthesis generation is not next")
            row = SynthesisExecution(
                id=uuid4(), team_id=team_id, analysis_id=analysis_id, source_execution_id=source_execution_id,
                tenant_resource_version=request.tenant_resource_version, generation=request.generation, state="pending",
                request_fingerprint=fingerprint, normalizer_version=request.normalizer_version,
                report_worker_image_digest=request.report_worker_image_digest, projection_sha256_b64=_checksum(request.projection_sha256_b64),
                projection_artifact_id=None, provider_protocol=request.provider_protocol, provider_name=request.provider_name,
                provider_model=request.model, prompt_template_version=request.prompt_template_version,
                prompt_template_sha256_b64=_checksum(request.prompt_template_sha256_b64), attempt_count=0,
                prompt_tokens=None, completion_tokens=None, total_tokens=None, latency_ms=None, stable_error_code=None,
                candidate_artifact_id=None, candidate_sha256_b64=None, report_generated_at=None, report_version_id=None,
                started_at=None, completed_at=None, version=1,
            )
            session.add(row)
            if mode == "manual":
                session.add(IdempotencyKey(id=uuid4(), team_id=team_id, key=idempotency_key,
                    operation="create_synthesis_run", scope_type="team", scope_id=team_id,
                    request_hash=fingerprint, state="completed", response_resource_id=row.id,
                    expires_at=now + timedelta(days=30), version=1))
            await session.flush()
            return self._record(row)

    async def bind_projection(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        now: datetime,
        fence: SynthesisMutationFence,
        sha256_b64: str | None = None,
    ) -> SynthesisExecutionRecord:
        if sha256_b64 is not None:
            _checksum(sha256_b64)
        return await self._bind_uuid(
            "projection_artifact_id",
            team_id,
            analysis_id,
            execution_id,
            artifact_id,
            now,
            fence,
            sha256_b64=sha256_b64,
        )

    async def bind_candidate(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, artifact_id: UUID, sha256_b64: str, now: datetime, fence: SynthesisMutationFence) -> SynthesisExecutionRecord:
        _checksum(sha256_b64)
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if row.candidate_artifact_id not in (None, artifact_id) or row.candidate_sha256_b64 not in (None, sha256_b64):
                raise SynthesisIdempotencyConflictError("candidate authority changed")
            row.candidate_artifact_id, row.candidate_sha256_b64, row.version, row.updated_at = artifact_id, sha256_b64, row.version + 1, now
            return self._record(row)

    async def _bind_uuid(self, field: Literal["projection_artifact_id"], team_id: UUID, analysis_id: UUID, execution_id: UUID, value: UUID, now: datetime, fence: SynthesisMutationFence, *, sha256_b64: str | None = None) -> SynthesisExecutionRecord:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if getattr(row, field) not in (None, value):
                raise SynthesisIdempotencyConflictError("artifact authority changed")
            if row.projection_artifact_id is not None and sha256_b64 is not None and row.projection_sha256_b64 != sha256_b64:
                raise SynthesisIdempotencyConflictError("projection checksum changed")
            setattr(row, field, value)
            if sha256_b64 is not None:
                row.projection_sha256_b64 = sha256_b64
            row.version += 1
            row.updated_at = now
            return self._record(row)

    async def _row(self, session: AsyncSession, team_id: UUID, analysis_id: UUID, execution_id: UUID) -> SynthesisExecution:
        row = await session.scalar(select(SynthesisExecution).where(SynthesisExecution.id == execution_id, SynthesisExecution.team_id == team_id, SynthesisExecution.analysis_id == analysis_id).with_for_update())
        if row is None:
            raise SynthesisExecutionNotFoundError("synthesis execution was not found")
        return row

    @staticmethod
    async def _require_fence(
        session: AsyncSession,
        row: SynthesisExecution,
        fence: SynthesisMutationFence,
        now: datetime,
    ) -> None:
        digest = hashlib.sha256(fence.token.get_secret_value().encode()).hexdigest()
        claim = await session.scalar(select(WorkerClaim).where(
            WorkerClaim.id == fence.claim_id,
            WorkerClaim.event_id == fence.event_id,
            WorkerClaim.global_job_id == row.analysis_id,
            WorkerClaim.scenario_job_id.is_(None),
            WorkerClaim.consumer_id == fence.consumer_id,
        ).with_for_update())
        event = await session.scalar(select(OutboxEvent).where(
            OutboxEvent.id == fence.event_id,
            OutboxEvent.team_id == row.team_id,
            OutboxEvent.global_job_id == row.analysis_id,
            OutboxEvent.subject_id == row.id,
            OutboxEvent.event_type == "analysis_synthesis_requested",
            OutboxEvent.subject_type == "synthesis_execution",
        ).with_for_update())
        if (
            claim is None
            or event is None
            or claim.state != "active"
            or claim.expires_at <= now
            or not hmac.compare_digest(claim.token_digest, digest)
        ):
            raise SynthesisLeaseLostError("synthesis execution fence was lost")

    async def begin_invocation(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, now: datetime, fence: SynthesisMutationFence) -> int:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if row.state not in {"pending", "running"}:
                raise SynthesisLeaseLostError("synthesis is terminal")
            attempt = row.attempt_count + 1
            if attempt > 2:
                raise SynthesisIdempotencyConflictError("invocation retry limit reached")
            row.state, row.attempt_count, row.started_at, row.version, row.updated_at = "running", attempt, row.started_at or now, row.version + 1, now
            session.add(AIInvocation(id=uuid4(), synthesis_execution_id=row.id, team_id=team_id, analysis_id=analysis_id,
                attempt_number=attempt, request_fingerprint=row.request_fingerprint, provider_protocol=row.provider_protocol,
                provider_name=row.provider_name, provider_model=row.provider_model, prompt_template_version=row.prompt_template_version,
                state="running", prompt_tokens=None, completion_tokens=None, total_tokens=None, latency_ms=None,
                stable_error_code=None, started_at=now, completed_at=None))
            return attempt

    async def finish_invocation(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, attempt_number: int,
        succeeded: bool, prompt_tokens: int | None, completion_tokens: int | None,
        total_tokens: int | None, latency_ms: int | None, stable_error_code: str | None,
        now: datetime, fence: SynthesisMutationFence,
    ) -> SynthesisExecutionRecord:
        if attempt_number not in {1, 2} or (succeeded and stable_error_code is not None):
            raise ValueError("AI invocation completion is invalid")
        if any(value is not None and (type(value) is not int or value < 0) for value in (prompt_tokens, completion_tokens, total_tokens, latency_ms)):
            raise ValueError("AI invocation completion is invalid")
        if None not in (prompt_tokens, completion_tokens, total_tokens) and total_tokens != prompt_tokens + completion_tokens:
            raise ValueError("AI invocation completion is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            invocation = await session.scalar(select(AIInvocation).where(
                AIInvocation.synthesis_execution_id == execution_id,
                AIInvocation.attempt_number == attempt_number,
                AIInvocation.team_id == team_id, AIInvocation.analysis_id == analysis_id,
            ).with_for_update())
            if invocation is None or invocation.state != "running" or row.state not in {"pending", "running"}:
                raise SynthesisLeaseLostError("AI invocation authority was lost")
            invocation.state = "succeeded" if succeeded else "failed"
            invocation.prompt_tokens = prompt_tokens
            invocation.completion_tokens = completion_tokens
            invocation.total_tokens = total_tokens
            invocation.latency_ms = latency_ms
            invocation.stable_error_code = stable_error_code
            invocation.completed_at = now
            row.prompt_tokens = prompt_tokens
            row.completion_tokens = completion_tokens
            row.total_tokens = total_tokens
            row.latency_ms = latency_ms
            row.stable_error_code = stable_error_code
            if not succeeded and attempt_number == 2:
                row.state = "failed"
                row.completed_at = now
            row.version += 1
            row.updated_at = now
            return self._record(row)

    async def bind_candidate_result(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        attempt_number: int,
        artifact_id: UUID,
        sha256_b64: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        generated_at: datetime,
        now: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisExecutionRecord:
        _checksum(sha256_b64)
        if (
            attempt_number not in {1, 2}
            or any(
                type(value) is not int or value < 0
                for value in (prompt_tokens, completion_tokens, total_tokens, latency_ms)
            )
            or total_tokens != prompt_tokens + completion_tokens
            or generated_at.tzinfo is None
            or generated_at.utcoffset() is None
        ):
            raise ValueError("AI invocation completion is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            invocation = await session.scalar(
                select(AIInvocation)
                .where(
                    AIInvocation.synthesis_execution_id == execution_id,
                    AIInvocation.attempt_number == attempt_number,
                    AIInvocation.team_id == team_id,
                    AIInvocation.analysis_id == analysis_id,
                )
                .with_for_update()
            )
            if invocation is None or row.state not in {"pending", "running"}:
                raise SynthesisLeaseLostError("AI invocation authority was lost")
            if invocation.state == "succeeded":
                if (
                    row.candidate_artifact_id == artifact_id
                    and row.candidate_sha256_b64 == sha256_b64
                    and row.report_generated_at == generated_at
                ):
                    return self._record(row)
                raise SynthesisIdempotencyConflictError("candidate authority changed")
            if invocation.state != "running":
                raise SynthesisLeaseLostError("AI invocation authority was lost")
            if row.candidate_artifact_id not in (None, artifact_id) or row.candidate_sha256_b64 not in (None, sha256_b64):
                raise SynthesisIdempotencyConflictError("candidate authority changed")
            if row.report_generated_at not in (None, generated_at):
                raise SynthesisIdempotencyConflictError("report timestamp changed")
            invocation.state = "succeeded"
            invocation.prompt_tokens = prompt_tokens
            invocation.completion_tokens = completion_tokens
            invocation.total_tokens = total_tokens
            invocation.latency_ms = latency_ms
            invocation.stable_error_code = None
            invocation.completed_at = now
            row.candidate_artifact_id = artifact_id
            row.candidate_sha256_b64 = sha256_b64
            row.prompt_tokens = prompt_tokens
            row.completion_tokens = completion_tokens
            row.total_tokens = total_tokens
            row.latency_ms = latency_ms
            row.stable_error_code = None
            row.report_generated_at = generated_at
            row.version += 1
            row.updated_at = now
            return self._record(row)

    async def finish_invocation_failure(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        attempt_number: int,
        stable_error_code: str,
        latency_ms: int | None,
        exhausted: bool,
        generated_at: datetime | None,
        now: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisExecutionRecord:
        if (
            attempt_number not in {1, 2}
            or _STABLE_CODE.fullmatch(stable_error_code) is None
            or latency_ms is not None
            and (type(latency_ms) is not int or latency_ms < 0)
            or exhausted != (generated_at is not None)
            or generated_at is not None
            and (generated_at.tzinfo is None or generated_at.utcoffset() is None)
        ):
            raise ValueError("AI invocation completion is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            invocation = await session.scalar(
                select(AIInvocation)
                .where(
                    AIInvocation.synthesis_execution_id == execution_id,
                    AIInvocation.attempt_number == attempt_number,
                    AIInvocation.team_id == team_id,
                    AIInvocation.analysis_id == analysis_id,
                )
                .with_for_update()
            )
            if invocation is None or row.state not in {"pending", "running"}:
                raise SynthesisLeaseLostError("AI invocation authority was lost")
            if invocation.state == "failed":
                if invocation.stable_error_code == stable_error_code:
                    return self._record(
                        row, last_invocation_error_code=stable_error_code
                    )
                raise SynthesisIdempotencyConflictError("AI invocation result changed")
            if invocation.state != "running":
                raise SynthesisLeaseLostError("AI invocation authority was lost")
            invocation.state = "failed"
            invocation.prompt_tokens = None
            invocation.completion_tokens = None
            invocation.total_tokens = None
            invocation.latency_ms = latency_ms
            invocation.stable_error_code = stable_error_code
            invocation.completed_at = now
            row.latency_ms = latency_ms
            if exhausted:
                if row.report_generated_at not in (None, generated_at):
                    raise SynthesisIdempotencyConflictError("report timestamp changed")
                row.stable_error_code = stable_error_code
                row.report_generated_at = generated_at
            else:
                row.stable_error_code = None
            row.version += 1
            row.updated_at = now
            return self._record(row, last_invocation_error_code=stable_error_code)

    async def cancel(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, now: datetime) -> SynthesisExecutionRecord:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if row.state not in {"succeeded", "failed", "canceled"}:
                row.state, row.started_at, row.completed_at, row.version, row.updated_at = "canceled", row.started_at or now, now, row.version + 1, now
            return self._record(row)

    async def bind_report_timestamp(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, generated_at: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisExecutionRecord:
        """Persist the one report timestamp before any report writer is invoked."""
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("report timestamp is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if row.report_generated_at not in (None, generated_at):
                raise SynthesisIdempotencyConflictError("report timestamp changed")
            if row.report_generated_at is None:
                row.report_generated_at = generated_at
                row.version += 1
                row.updated_at = generated_at
            return self._record(row)

    async def bind_preflight_failure(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        stable_error_code: str,
        generated_at: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisExecutionRecord:
        """Persist a deterministic pre-provider failure so core reporting can resume."""
        if (
            _STABLE_CODE.fullmatch(stable_error_code) is None
            or generated_at.tzinfo is None
            or generated_at.utcoffset() is None
        ):
            raise ValueError("synthesis preflight failure is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if (
                row.stable_error_code == stable_error_code
                and row.report_generated_at == generated_at
            ):
                return self._record(row)
            if (
                row.state not in {"pending", "running"}
                or row.report_version_id is not None
                or row.candidate_artifact_id is not None
                or row.stable_error_code is not None
                or row.report_generated_at is not None
            ):
                raise SynthesisLeaseLostError("synthesis preflight authority was lost")
            row.state = "running"
            row.started_at = row.started_at or generated_at
            row.stable_error_code = stable_error_code
            row.report_generated_at = generated_at
            row.version += 1
            row.updated_at = generated_at
            return self._record(row)

    async def bind_report(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, report_version_id: UUID,
        now: datetime, fence: SynthesisMutationFence, synthesis_succeeded: bool | None = None,
    ) -> SynthesisExecutionRecord:
        """Record an immutable report version; report bytes remain tenant-owned."""
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if row.report_generated_at is None:
                raise SynthesisIdempotencyConflictError("report timestamp is required")
            if row.report_version_id not in (None, report_version_id):
                raise SynthesisIdempotencyConflictError("report authority changed")
            target_state = "failed" if synthesis_succeeded is False else "succeeded"
            if row.state == target_state and row.report_version_id == report_version_id:
                return self._record(row)
            if row.state not in {"pending", "running"}:
                raise SynthesisLeaseLostError("synthesis is terminal")
            if synthesis_succeeded is not None and synthesis_succeeded != (row.candidate_artifact_id is not None):
                raise SynthesisIdempotencyConflictError("synthesis result is inconsistent")
            if synthesis_succeeded is False and row.stable_error_code is None:
                raise SynthesisIdempotencyConflictError("synthesis failure is missing")
            row.report_version_id = report_version_id
            row.state = target_state
            row.started_at = row.started_at or now
            row.completed_at = now
            row.version += 1
            row.updated_at = now
            return self._record(row)

    async def bind_source_report(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        report_version_id: UUID,
        now: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisSourceRecord:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            source = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.id == row.source_execution_id,
                    EngineExecution.team_id == team_id,
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
                .with_for_update()
            )
            if source is None:
                raise SynthesisExecutionNotFoundError("source execution is not authoritative")
            if source.normalized_report_version_id == report_version_id:
                return self._source(source)
            expected: UUID | None = None
            if row.generation > 1:
                previous = await session.scalar(
                    select(SynthesisExecution).where(
                        SynthesisExecution.analysis_id == analysis_id,
                        SynthesisExecution.source_execution_id == row.source_execution_id,
                        SynthesisExecution.generation == row.generation - 1,
                    )
                )
                if previous is None or previous.report_version_id is None:
                    raise SynthesisIdempotencyConflictError("previous report is unavailable")
                expected = previous.report_version_id
            if source.normalized_report_version_id != expected:
                raise SynthesisIdempotencyConflictError("source report authority changed")
            source.normalized_report_version_id = report_version_id
            source.version += 1
            source.updated_at = now
            return self._source(source)

    async def fail_without_report(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        stable_error_code: str,
        now: datetime,
        fence: SynthesisMutationFence,
    ) -> SynthesisExecutionRecord:
        if _STABLE_CODE.fullmatch(stable_error_code) is None:
            raise ValueError("synthesis failure code is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            await self._require_fence(session, row, fence, self._clock())
            if row.state == "failed" and row.report_version_id is None:
                if row.stable_error_code != stable_error_code:
                    raise SynthesisIdempotencyConflictError("synthesis failure changed")
                return self._record(row)
            if row.state not in {"pending", "running"} or row.report_version_id is not None:
                raise SynthesisLeaseLostError("synthesis is terminal")
            row.state = "failed"
            row.stable_error_code = stable_error_code
            row.started_at = row.started_at or now
            row.completed_at = now
            row.version += 1
            row.updated_at = now
            return self._record(row)


__all__ = ["SQLAlchemySynthesisExecutionRepository", "SynthesisExecutionError", "SynthesisExecutionNotFoundError", "SynthesisExecutionRecord", "SynthesisIdempotencyConflictError", "SynthesisLeaseLostError", "SynthesisMutationFence", "SynthesisRequest", "SynthesisSourceRecord", "synthesis_request_fingerprint"]
