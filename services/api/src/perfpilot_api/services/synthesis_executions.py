"""Durable, non-secret control-plane records for AI synthesis generations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import (
    AIInvocation,
    EngineExecution,
    IdempotencyKey,
    SynthesisExecution,
    TenantResource,
)
from perfpilot_api.reports.projection import normalize_authoritative_question


_NORMALIZER_VERSION = "smartperfetto-normalizer-1"
_PROJECTION_CONTRACT_VERSION = "1.0"
_REPORT_CONTRACT_VERSION = "1.1"
_PROVIDER_PROTOCOL = "chat-completions-json-schema-v1"


class SynthesisExecutionError(RuntimeError):
    """Stable synthesis control-plane error."""


class SynthesisExecutionNotFoundError(SynthesisExecutionError):
    pass


class SynthesisIdempotencyConflictError(SynthesisExecutionError):
    pass


class SynthesisLeaseLostError(SynthesisExecutionError):
    pass


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
    projection_sha256_b64: str
    projection_artifact_id: UUID | None
    attempt_count: int
    candidate_artifact_id: UUID | None
    candidate_sha256_b64: str | None
    report_generated_at: datetime | None
    report_version_id: UUID | None
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
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _record(row: SynthesisExecution) -> SynthesisExecutionRecord:
        return SynthesisExecutionRecord(
            id=row.id, team_id=row.team_id, analysis_id=row.analysis_id,
            source_execution_id=row.source_execution_id, tenant_resource_version=row.tenant_resource_version,
            generation=row.generation, state=row.state, request_fingerprint=row.request_fingerprint,
            normalizer_version=row.normalizer_version, projection_sha256_b64=row.projection_sha256_b64,
            projection_artifact_id=row.projection_artifact_id, attempt_count=row.attempt_count,
            candidate_artifact_id=row.candidate_artifact_id, candidate_sha256_b64=row.candidate_sha256_b64,
            report_generated_at=row.report_generated_at, report_version_id=row.report_version_id, version=row.version,
        )

    async def allocate(
        self, *, team_id: UUID, analysis_id: UUID, source_execution_id: UUID,
        request: SynthesisRequest, now: datetime, idempotency_key: str | None = None,
    ) -> SynthesisExecutionRecord:
        fingerprint = synthesis_request_fingerprint(
            canonical_sha256_b64=request.canonical_sha256_b64, tenant_resource_version=request.tenant_resource_version,
            question=request.question, normalizer_version=request.normalizer_version,
            prompt_template_version=request.prompt_template_version, prompt_template_sha256_b64=request.prompt_template_sha256_b64,
            report_worker_image_digest=request.report_worker_image_digest, provider_protocol=request.provider_protocol,
            provider_name=request.provider_name, model=request.model, inference_config_hash=request.inference_config_hash,
            generation=request.generation,
        )
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
            existing = await session.scalar(select(SynthesisExecution).where(
                SynthesisExecution.analysis_id == analysis_id, SynthesisExecution.source_execution_id == source_execution_id,
                SynthesisExecution.generation == request.generation).with_for_update())
            if existing is not None:
                if not hmac.compare_digest(existing.request_fingerprint, fingerprint):
                    raise SynthesisIdempotencyConflictError("synthesis request changed")
                return self._record(existing)
            if idempotency_key is not None:
                key = await session.scalar(select(IdempotencyKey).where(
                    IdempotencyKey.operation == "create_synthesis_run", IdempotencyKey.scope_type == "team",
                    IdempotencyKey.scope_id == team_id, IdempotencyKey.key == idempotency_key).with_for_update())
                if key is not None:
                    if not hmac.compare_digest(key.request_hash, fingerprint) or key.response_resource_id is None:
                        raise SynthesisIdempotencyConflictError("synthesis idempotency key changed")
                    replay = await session.get(SynthesisExecution, key.response_resource_id)
                    if replay is None or replay.team_id != team_id or replay.analysis_id != analysis_id:
                        raise SynthesisExecutionNotFoundError("synthesis replay is unavailable")
                    return self._record(replay)
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
            if idempotency_key is not None:
                session.add(IdempotencyKey(id=uuid4(), team_id=team_id, key=idempotency_key,
                    operation="create_synthesis_run", scope_type="team", scope_id=team_id,
                    request_hash=fingerprint, state="completed", response_resource_id=row.id,
                    expires_at=now + timedelta(days=30), version=1))
            await session.flush()
            return self._record(row)

    async def bind_projection(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, artifact_id: UUID, now: datetime) -> SynthesisExecutionRecord:
        return await self._bind_uuid("projection_artifact_id", team_id, analysis_id, execution_id, artifact_id, now)

    async def bind_candidate(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, artifact_id: UUID, sha256_b64: str, now: datetime) -> SynthesisExecutionRecord:
        _checksum(sha256_b64)
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if row.candidate_artifact_id not in (None, artifact_id) or row.candidate_sha256_b64 not in (None, sha256_b64):
                raise SynthesisIdempotencyConflictError("candidate authority changed")
            row.candidate_artifact_id, row.candidate_sha256_b64, row.version, row.updated_at = artifact_id, sha256_b64, row.version + 1, now
            return self._record(row)

    async def _bind_uuid(self, field: Literal["projection_artifact_id"], team_id: UUID, analysis_id: UUID, execution_id: UUID, value: UUID, now: datetime) -> SynthesisExecutionRecord:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if getattr(row, field) not in (None, value):
                raise SynthesisIdempotencyConflictError("artifact authority changed")
            setattr(row, field, value)
            row.version += 1
            row.updated_at = now
            return self._record(row)

    async def _row(self, session: AsyncSession, team_id: UUID, analysis_id: UUID, execution_id: UUID) -> SynthesisExecution:
        row = await session.scalar(select(SynthesisExecution).where(SynthesisExecution.id == execution_id, SynthesisExecution.team_id == team_id, SynthesisExecution.analysis_id == analysis_id).with_for_update())
        if row is None:
            raise SynthesisExecutionNotFoundError("synthesis execution was not found")
        return row

    async def begin_invocation(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, now: datetime) -> int:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if row.state == "canceled":
                raise SynthesisLeaseLostError("synthesis is canceled")
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

    async def cancel(self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, now: datetime) -> SynthesisExecutionRecord:
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if row.state not in {"succeeded", "failed", "canceled"}:
                row.state, row.started_at, row.completed_at, row.version, row.updated_at = "canceled", row.started_at or now, now, row.version + 1, now
            return self._record(row)

    async def bind_report_timestamp(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, generated_at: datetime
    ) -> SynthesisExecutionRecord:
        """Persist the one report timestamp before any report writer is invoked."""
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("report timestamp is invalid")
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if row.report_generated_at not in (None, generated_at):
                raise SynthesisIdempotencyConflictError("report timestamp changed")
            if row.report_generated_at is None:
                row.report_generated_at = generated_at
                row.version += 1
                row.updated_at = generated_at
            return self._record(row)

    async def bind_report(
        self, *, team_id: UUID, analysis_id: UUID, execution_id: UUID, report_version_id: UUID,
        now: datetime,
    ) -> SynthesisExecutionRecord:
        """Record an immutable report version; report bytes remain tenant-owned."""
        async with self._session_factory.begin() as session:
            row = await self._row(session, team_id, analysis_id, execution_id)
            if row.report_generated_at is None:
                raise SynthesisIdempotencyConflictError("report timestamp is required")
            if row.report_version_id not in (None, report_version_id):
                raise SynthesisIdempotencyConflictError("report authority changed")
            if row.state == "canceled":
                raise SynthesisLeaseLostError("synthesis is canceled")
            row.report_version_id = report_version_id
            row.state = "succeeded"
            row.started_at = row.started_at or now
            row.completed_at = now
            row.version += 1
            row.updated_at = now
            return self._record(row)


__all__ = ["SQLAlchemySynthesisExecutionRepository", "SynthesisExecutionError", "SynthesisExecutionNotFoundError", "SynthesisExecutionRecord", "SynthesisIdempotencyConflictError", "SynthesisLeaseLostError", "SynthesisRequest", "synthesis_request_fingerprint"]
