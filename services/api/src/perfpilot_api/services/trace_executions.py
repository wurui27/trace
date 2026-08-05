"""Prepare and advance tenant-owned SmartPerfetto trace executions."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal, Protocol
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import EngineExecution, GlobalJob
from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.domain.states import ANALYSIS_TERMINAL_STATES, AnalysisState
from perfpilot_api.domain.transitions import InvalidTransition, transition
from perfpilot_api.engines.contracts import (
    AnalysisProfile,
    EngineInput,
    EngineStepOutcome,
)
from perfpilot_api.services.engine_executions import EngineExecutionRecord
from perfpilot_api.services.uploads import (
    DownloadAuthorization,
    UploadError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadService,
)


_TRACE_INPUT_ORDER = (
    "trace",
    "memory_evidence",
    "apk",
    "source_archive",
    "mapping",
    "native_symbols",
    "log",
)
_TRACE_INPUT_KINDS = frozenset(_TRACE_INPUT_ORDER)
_SMARTPERFETTO_INPUT_KINDS = frozenset({"trace"})
_MIME_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024


class TraceExecutionError(RuntimeError):
    """A stable trace orchestration error without private storage details."""


class TraceExecutionNotFoundError(TraceExecutionError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("trace analysis was not found")


class TraceExecutionUnavailableError(TraceExecutionError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("trace execution service is unavailable")


@dataclass(frozen=True, slots=True)
class TraceExecutionArtifact:
    artifact_id: UUID
    analysis_id: UUID
    artifact_kind: str
    mime_type: str
    size_bytes: int
    sha256_b64: str
    version: int
    state: str
    expires_at: datetime
    deleted_at: datetime | None
    source_artifact_kind: str | None = None


@dataclass(frozen=True, slots=True)
class LoadedTraceAnalysis:
    analysis_id: UUID
    analysis_mode: str
    analysis_state: str
    tombstoned_at: datetime | None
    tenant_resource_version: int
    analysis_profile: str
    question: str | None
    input_manifest: tuple[dict[str, object], ...]
    input_artifacts: tuple[TraceExecutionArtifact, ...]
    latest_execution: EngineExecutionRecord | None


@dataclass(frozen=True, slots=True)
class PreparedTraceExecution:
    execution: EngineExecutionRecord
    inputs: tuple[EngineInput, ...]
    analysis_profile: AnalysisProfile
    question: str | None
    timeout_seconds: int


class TraceExecutionRepository(Protocol):
    async def load_analysis(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> LoadedTraceAnalysis: ...

    async def require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None: ...

    async def load_execution(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineExecutionRecord: ...

    async def project_parent(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        target_state: Literal[
            "analyzing",
            "completed",
            "partially_completed",
            "failed",
            "canceled",
        ],
        failure_code: str | None,
        now: datetime,
    ) -> None: ...


class TraceEngineExecutionService(Protocol):
    async def create_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        engine_id: str,
        tenant_resource_version: int,
        input_manifest_hash: str,
        config_hash: str,
    ) -> EngineExecutionRecord: ...

    async def submit_attempt(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        inputs: tuple[EngineInput, ...],
        profile: AnalysisProfile,
        question: str | None,
        timeout_seconds: int,
    ) -> EngineExecutionRecord | EngineStepOutcome: ...

    async def step(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineStepOutcome: ...


def _canonical_checksum(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        return None
    return value


def _is_unexpired(value: object, *, now: datetime) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None or now.tzinfo is None:
        return False
    try:
        return value > now
    except (TypeError, ValueError):
        return False


def _canonical_json_hash(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise ValueError("trace execution input is invalid") from None
    return hashlib.sha256(payload).hexdigest()


def canonical_trace_input_manifest_hash(
    artifacts: tuple[TraceExecutionArtifact, ...],
) -> str:
    if not artifacts:
        raise ValueError("trace execution input is invalid")
    by_kind: dict[str, TraceExecutionArtifact] = {}
    for artifact in artifacts:
        if (
            not isinstance(artifact, TraceExecutionArtifact)
            or artifact.artifact_kind not in _SMARTPERFETTO_INPUT_KINDS
            or artifact.artifact_kind in by_kind
            or not isinstance(artifact.artifact_id, UUID)
            or not isinstance(artifact.analysis_id, UUID)
            or _MIME_TYPE.fullmatch(artifact.mime_type) is None
            or type(artifact.size_bytes) is not int
            or not 1 <= artifact.size_bytes <= _MAX_UPLOAD_BYTES
            or _canonical_checksum(artifact.sha256_b64) is None
            or type(artifact.version) is not int
            or artifact.version < 1
            or artifact.state != "finalized"
            or artifact.deleted_at is not None
        ):
            raise ValueError("trace execution input is invalid")
        by_kind[artifact.artifact_kind] = artifact
    if set(by_kind) != {"trace"}:
        raise ValueError("trace execution input is invalid")
    return _canonical_json_hash(
        {
            "schema_version": "1.0",
            "inputs": [
                {
                    "artifact_id": str(artifact.artifact_id),
                    "artifact_version": artifact.version,
                    "kind": artifact.artifact_kind,
                    "mime": artifact.mime_type,
                    "sha256_b64": artifact.sha256_b64,
                    "size": artifact.size_bytes,
                }
                for artifact in (by_kind["trace"],)
            ],
        }
    )


def canonical_trace_config_hash(
    *,
    analysis_profile: str,
    question: str | None,
    timeout_seconds: int,
) -> str:
    if (
        analysis_profile not in ("auto", "startup", "scroll")
        or question is not None
        and (
            not isinstance(question, str)
            or not question
            or question != question.strip()
            or len(question) > 2_000
        )
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 3_600
    ):
        raise ValueError("trace execution config is invalid")
    return _canonical_json_hash(
        {
            "analysis_profile": analysis_profile,
            "question": question,
            "schema_version": "1.0",
            "timeout_seconds": timeout_seconds,
        }
    )


class TraceExecutionService:
    def __init__(
        self,
        *,
        repository: TraceExecutionRepository,
        upload_service: UploadService,
        engine_service: TraceEngineExecutionService,
        timeout_seconds: int = 1_800,
        schedule_synthesis: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3_600:
            raise ValueError("trace timeout is invalid")
        self._repository = repository
        self._upload_service = upload_service
        self._engine_service = engine_service
        self._timeout_seconds = timeout_seconds
        self._schedule_synthesis = schedule_synthesis
        self._clock = clock

    def _validate_analysis(
        self,
        loaded: LoadedTraceAnalysis,
        *,
        analysis_id: UUID,
    ) -> tuple[AnalysisProfile, tuple[TraceExecutionArtifact, ...]]:
        now = self._clock()
        if (
            not isinstance(loaded, LoadedTraceAnalysis)
            or loaded.analysis_id != analysis_id
            or loaded.analysis_mode not in ("trace_upload", "device")
            or loaded.analysis_state == "deleted"
            or loaded.tombstoned_at is not None
            or type(loaded.tenant_resource_version) is not int
            or loaded.tenant_resource_version < 1
            or loaded.analysis_profile not in ("auto", "startup", "scroll")
            or loaded.question is not None
            and (
                not isinstance(loaded.question, str)
                or not loaded.question
                or loaded.question != loaded.question.strip()
                or len(loaded.question) > 2_000
            )
            or not loaded.input_manifest
        ):
            raise TraceExecutionNotFoundError

        manifest_by_kind: dict[str, dict[str, object]] = {}
        ordered_kinds: list[str] = []
        for item in loaded.input_manifest:
            if not isinstance(item, dict) or set(item) != {
                "kind",
                "mime",
                "size",
                "sha256_b64",
            }:
                raise TraceExecutionNotFoundError
            kind = item.get("kind")
            mime = item.get("mime")
            size = item.get("size")
            checksum = _canonical_checksum(item.get("sha256_b64"))
            if (
                not isinstance(kind, str)
                or kind not in _TRACE_INPUT_KINDS
                or kind in manifest_by_kind
                or not isinstance(mime, str)
                or _MIME_TYPE.fullmatch(mime) is None
                or type(size) is not int
                or not 1 <= size <= _MAX_UPLOAD_BYTES
                or checksum is None
            ):
                raise TraceExecutionNotFoundError
            manifest_by_kind[kind] = {
                "kind": kind,
                "mime": mime,
                "size": size,
                "sha256_b64": checksum,
            }
            ordered_kinds.append(kind)
        if "trace" not in manifest_by_kind or ordered_kinds != [
            kind for kind in _TRACE_INPUT_ORDER if kind in manifest_by_kind
        ]:
            raise TraceExecutionNotFoundError

        artifacts_by_kind: dict[str, TraceExecutionArtifact] = {}
        for artifact in loaded.input_artifacts:
            expected = manifest_by_kind.get(artifact.artifact_kind)
            if (
                not isinstance(artifact, TraceExecutionArtifact)
                or artifact.analysis_id != analysis_id
                or expected is None
                or artifact.artifact_kind in artifacts_by_kind
                or artifact.mime_type != expected["mime"]
                or artifact.size_bytes != expected["size"]
                or not hmac.compare_digest(
                    artifact.sha256_b64,
                    str(expected["sha256_b64"]),
                )
                or type(artifact.version) is not int
                or artifact.version < 1
                or artifact.deleted_at is not None
                or artifact.state not in ("pending", "finalized")
            ):
                raise TraceExecutionNotFoundError
            artifacts_by_kind[artifact.artifact_kind] = artifact
        trace = artifacts_by_kind.get("trace")
        if (
            trace is None
            or trace.state != "finalized"
            or not _is_unexpired(trace.expires_at, now=now)
        ):
            raise TraceExecutionNotFoundError
        return loaded.analysis_profile, (trace,)  # type: ignore[return-value]

    async def _claim_input(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact: TraceExecutionArtifact,
        resource_version: int,
    ) -> tuple[EngineInput, datetime]:
        try:
            authorization = await self._upload_service.download(
                team_id=team_id,
                analysis_id=analysis_id,
                artifact_id=artifact.artifact_id,
                expected_tenant_resource_version=resource_version,
                expected_artifact_version=artifact.version,
            )
        except (UploadNotFoundError, UploadExpiredError):
            raise TraceExecutionNotFoundError from None
        except UploadError:
            raise TraceExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise TraceExecutionUnavailableError from None
        if (
            not isinstance(authorization, DownloadAuthorization)
            or authorization.artifact_id != artifact.artifact_id
            or authorization.tenant_resource_version != resource_version
            or authorization.artifact_version != artifact.version
            or authorization.artifact_kind
            != (artifact.source_artifact_kind or artifact.artifact_kind)
            or authorization.mime != artifact.mime_type
            or authorization.size != artifact.size_bytes
            or not hmac.compare_digest(authorization.sha256_b64, artifact.sha256_b64)
            or not isinstance(authorization.url, str)
            or not authorization.url
            or not _is_unexpired(authorization.expires_at, now=self._clock())
        ):
            raise TraceExecutionUnavailableError
        await self._require_resource_version(
            team_id=team_id,
            expected_resource_version=resource_version,
        )
        return (
            EngineInput(
                artifact_id=artifact.artifact_id,
                kind=artifact.artifact_kind,
                mime=artifact.mime_type,
                size_bytes=artifact.size_bytes,
                sha256_b64=artifact.sha256_b64,
                download_url=SecretStr(authorization.url),
            ),
            authorization.expires_at,
        )

    async def _require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None:
        try:
            await self._repository.require_resource_version(
                team_id=team_id,
                expected_resource_version=expected_resource_version,
            )
        except TraceExecutionError:
            raise TraceExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise TraceExecutionUnavailableError from None

    async def prepare(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> PreparedTraceExecution:
        try:
            loaded = await self._repository.load_analysis(
                team_id=team_id,
                analysis_id=analysis_id,
            )
        except TraceExecutionNotFoundError:
            raise TraceExecutionNotFoundError from None
        except TraceExecutionError:
            raise TraceExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise TraceExecutionUnavailableError from None
        profile, artifacts = self._validate_analysis(loaded, analysis_id=analysis_id)
        input_manifest_hash = canonical_trace_input_manifest_hash(artifacts)
        config_hash = canonical_trace_config_hash(
            analysis_profile=profile,
            question=loaded.question,
            timeout_seconds=self._timeout_seconds,
        )
        await self._require_resource_version(
            team_id=team_id,
            expected_resource_version=loaded.tenant_resource_version,
        )

        execution = loaded.latest_execution
        if execution is None:
            try:
                execution = await self._engine_service.create_attempt(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    engine_id="smartperfetto",
                    tenant_resource_version=loaded.tenant_resource_version,
                    input_manifest_hash=input_manifest_hash,
                    config_hash=config_hash,
                )
            except BaseException as error:
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                raise TraceExecutionUnavailableError from None
        if (
            not isinstance(execution, EngineExecutionRecord)
            or execution.team_id != team_id
            or execution.analysis_id != analysis_id
            or execution.engine_id != "smartperfetto"
            or execution.tenant_resource_version != loaded.tenant_resource_version
            or not hmac.compare_digest(execution.input_manifest_hash, input_manifest_hash)
            or not hmac.compare_digest(execution.config_hash, config_hash)
        ):
            raise TraceExecutionUnavailableError

        inputs: tuple[EngineInput, ...] = ()
        if execution.state == "pending":
            claimed = tuple(
                [
                    await self._claim_input(
                        team_id=team_id,
                        analysis_id=analysis_id,
                        artifact=artifact,
                        resource_version=loaded.tenant_resource_version,
                    )
                    for artifact in artifacts
                ]
            )
            if any(not _is_unexpired(expires_at, now=self._clock()) for _, expires_at in claimed):
                raise TraceExecutionUnavailableError
            inputs = tuple(engine_input for engine_input, _expires_at in claimed)
            await self._require_resource_version(
                team_id=team_id,
                expected_resource_version=loaded.tenant_resource_version,
            )
        return PreparedTraceExecution(
            execution=execution,
            inputs=inputs,
            analysis_profile=profile,
            question=loaded.question,
            timeout_seconds=self._timeout_seconds,
        )

    async def advance(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome:
        prepared = await self.prepare(team_id=team_id, analysis_id=analysis_id)
        execution = prepared.execution
        if execution.state == "pending":
            result = await self._engine_service.submit_attempt(
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution.id,
                inputs=prepared.inputs,
                profile=prepared.analysis_profile,
                question=prepared.question,
                timeout_seconds=prepared.timeout_seconds,
            )
            outcome = (
                result
                if isinstance(result, EngineStepOutcome)
                else EngineStepOutcome(result.id, result.state, None)
            )
        elif execution.state in ("running", "awaiting_user"):
            outcome = await self._engine_service.step(
                team_id=team_id,
                analysis_id=analysis_id,
                execution_id=execution.id,
            )
        else:
            outcome = EngineStepOutcome(execution.id, execution.state, None)

        observed = await self._repository.load_execution(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=outcome.execution_id,
        )
        target_state: Literal[
            "analyzing",
            "completed",
            "partially_completed",
            "failed",
            "canceled",
        ]
        failure_code: str | None = None
        if outcome.retry is not None or observed.state in ("pending", "running", "awaiting_user"):
            target_state = "analyzing"
        elif observed.state in {"completed", "insufficient_data"} and self._schedule_synthesis:
            # The report stage now owns the terminal parent projection.
            target_state = "analyzing"
        elif observed.state == "completed":
            target_state = "completed"
        elif observed.state == "insufficient_data":
            target_state = "partially_completed"
        elif observed.state == "canceled":
            target_state = "canceled"
        else:
            target_state = "failed"
            failure_code = observed.stable_error_code or "engine_failed"
        await self._repository.project_parent(
            team_id=team_id,
            analysis_id=analysis_id,
            target_state=target_state,
            failure_code=failure_code,
            now=self._clock(),
        )
        return outcome


class SQLAlchemyTraceExecutionRepository:
    def __init__(
        self,
        *,
        control_session_factory: async_sessionmaker[AsyncSession],
        tenant_router: TenantRouter,
    ) -> None:
        self._control_session_factory = control_session_factory
        self._tenant_router = tenant_router

    @staticmethod
    def _execution(row: EngineExecution) -> EngineExecutionRecord:
        return EngineExecutionRecord(
            id=row.id,
            analysis_id=row.analysis_id,
            team_id=row.team_id,
            engine_id=row.engine_id,
            attempt_number=row.attempt_number,
            tenant_resource_version=row.tenant_resource_version,
            adapter_version=row.adapter_version,
            engine_commit_sha=row.engine_commit_sha,
            engine_image_digest=row.engine_image_digest,
            input_manifest_hash=row.input_manifest_hash,
            config_hash=row.config_hash,
            external_workspace_id=row.external_workspace_id,
            external_session_id=row.external_session_id,
            external_run_id=row.external_run_id,
            state=row.state,  # type: ignore[arg-type]
            last_event_cursor=row.last_event_cursor,
            stable_error_code=row.stable_error_code,
            started_at=row.started_at,
            completed_at=row.completed_at,
            raw_result_artifact_id=row.raw_result_artifact_id,
            normalized_report_version_id=row.normalized_report_version_id,
            version=row.version,
        )

    @staticmethod
    def _artifact(
        row: Artifact,
        *,
        artifact_kind: str | None = None,
        source_artifact_kind: str | None = None,
    ) -> TraceExecutionArtifact:
        if row.analysis_id is None:
            raise TraceExecutionNotFoundError
        return TraceExecutionArtifact(
            artifact_id=row.id,
            analysis_id=row.analysis_id,
            artifact_kind=artifact_kind or row.artifact_kind,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            sha256_b64=row.sha256_b64,
            version=row.version,
            state=row.state,
            expires_at=row.expires_at,
            deleted_at=row.deleted_at,
            source_artifact_kind=source_artifact_kind,
        )

    async def load_analysis(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> LoadedTraceAnalysis:
        async with self._control_session_factory() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                    GlobalJob.analysis_mode.in_(("trace_upload", "device")),
                )
            )
            latest = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.team_id == team_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
                .order_by(EngineExecution.attempt_number.desc())
                .limit(1)
            )
        if job is None:
            raise TraceExecutionNotFoundError

        async with self._tenant_router.session(team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            analysis = await session.get(Analysis, analysis_id)
            if analysis is None:
                artifact_rows: tuple[Artifact, ...] = ()
            elif analysis.analysis_mode == "trace_upload":
                artifact_rows = tuple(
                    (
                        await session.scalars(
                            select(Artifact).where(
                                Artifact.analysis_id == analysis_id,
                                Artifact.idempotency_key.like("input-%"),
                                Artifact.deleted_at.is_(None),
                            )
                        )
                    ).all()
                )
            else:
                artifact_rows = tuple(
                    (
                        await session.scalars(
                            select(Artifact).where(
                                Artifact.analysis_id == analysis_id,
                                Artifact.artifact_kind.in_(("startup_trace", "scroll_trace")),
                                Artifact.state == "finalized",
                                Artifact.deleted_at.is_(None),
                            )
                        )
                    ).all()
                )
        if analysis is None or type(routed_version) is not int or routed_version < 1:
            raise TraceExecutionNotFoundError
        if analysis.analysis_mode == "trace_upload":
            if not isinstance(analysis.input_manifest, list) or not all(
                isinstance(item, dict) for item in analysis.input_manifest
            ):
                raise TraceExecutionNotFoundError
            manifest = tuple(dict(item) for item in analysis.input_manifest)
            artifacts = tuple(self._artifact(row) for row in artifact_rows)
            analysis_profile = analysis.analysis_profile or ""
            question = analysis.question
        elif analysis.analysis_mode == "device":
            by_kind = {row.artifact_kind: row for row in artifact_rows}
            selected = by_kind.get("startup_trace") or by_kind.get("scroll_trace")
            if selected is None:
                raise TraceExecutionNotFoundError
            profile = "startup" if selected.artifact_kind == "startup_trace" else "scroll"
            manifest = (
                {
                    "kind": "trace",
                    "mime": selected.mime_type,
                    "size": selected.size_bytes,
                    "sha256_b64": selected.sha256_b64,
                },
            )
            artifacts = (
                self._artifact(
                    selected,
                    artifact_kind="trace",
                    source_artifact_kind=selected.artifact_kind,
                ),
            )
            analysis_profile = profile
            question = None
        else:
            raise TraceExecutionNotFoundError
        return LoadedTraceAnalysis(
            analysis_id=analysis.id,
            analysis_mode=analysis.analysis_mode,
            analysis_state=analysis.state,
            tombstoned_at=analysis.tombstoned_at,
            tenant_resource_version=routed_version,
            analysis_profile=analysis_profile,
            question=question,
            input_manifest=manifest,
            input_artifacts=artifacts,
            latest_execution=self._execution(latest) if latest is not None else None,
        )

    async def require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None:
        if type(expected_resource_version) is not int or expected_resource_version < 1:
            raise TraceExecutionUnavailableError
        async with self._tenant_router.session(team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
        if routed_version != expected_resource_version:
            raise TraceExecutionUnavailableError

    async def load_execution(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
    ) -> EngineExecutionRecord:
        async with self._control_session_factory() as session:
            row = await session.scalar(
                select(EngineExecution).where(
                    EngineExecution.id == execution_id,
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.team_id == team_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
            )
        if row is None:
            raise TraceExecutionNotFoundError
        return self._execution(row)

    @staticmethod
    def _parent_values(
        *,
        current_started_at: datetime | None,
        target_state: str,
        failure_code: str | None,
        now: datetime,
    ) -> dict[str, object]:
        terminal = target_state in {
            "completed",
            "partially_completed",
            "failed",
            "canceled",
        }
        return {
            "state": target_state,
            "started_at": current_started_at or now,
            "completed_at": now if terminal else None,
            "failure_code": failure_code if target_state == "failed" else None,
            "updated_at": now,
        }

    @staticmethod
    def _validate_projection(
        *,
        current_state: str,
        target_state: str,
        failure_code: str | None,
    ) -> bool:
        if target_state == "failed":
            if failure_code is None or _STABLE_CODE.fullmatch(failure_code) is None:
                raise TraceExecutionUnavailableError
        elif failure_code is not None:
            raise TraceExecutionUnavailableError
        if current_state == target_state:
            return False
        try:
            current = AnalysisState(current_state)
            if current in ANALYSIS_TERMINAL_STATES:
                raise TraceExecutionUnavailableError
            transition(current_state, target_state)
        except (InvalidTransition, ValueError):
            raise TraceExecutionUnavailableError from None
        return True

    async def project_parent(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        target_state: Literal[
            "analyzing",
            "completed",
            "partially_completed",
            "failed",
            "canceled",
        ],
        failure_code: str | None,
        now: datetime,
    ) -> None:
        async with self._tenant_router.session(team_id) as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None or analysis.analysis_mode not in ("trace_upload", "device"):
                raise TraceExecutionNotFoundError
            if self._validate_projection(
                current_state=analysis.state,
                target_state=target_state,
                failure_code=failure_code,
            ):
                changed = await session.scalar(
                    update(Analysis)
                    .where(
                        Analysis.id == analysis_id,
                        Analysis.version == analysis.version,
                        Analysis.state == analysis.state,
                    )
                    .values(
                        **self._parent_values(
                            current_started_at=analysis.started_at,
                            target_state=target_state,
                            failure_code=failure_code,
                            now=now,
                        ),
                        version=Analysis.version + 1,
                    )
                    .returning(Analysis.id)
                )
                if changed is None:
                    raise TraceExecutionUnavailableError

        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                        GlobalJob.analysis_mode.in_(("trace_upload", "device")),
                    )
                    .with_for_update()
                )
                if job is None:
                    raise TraceExecutionNotFoundError
                if self._validate_projection(
                    current_state=job.state,
                    target_state=target_state,
                    failure_code=failure_code,
                ):
                    changed = await session.scalar(
                        update(GlobalJob)
                        .where(
                            GlobalJob.id == analysis_id,
                            GlobalJob.team_id == team_id,
                            GlobalJob.version == job.version,
                            GlobalJob.state == job.state,
                        )
                        .values(
                            **self._parent_values(
                                current_started_at=job.started_at,
                                target_state=target_state,
                                failure_code=failure_code,
                                now=now,
                            ),
                            version=GlobalJob.version + 1,
                        )
                        .returning(GlobalJob.id)
                    )
                    if changed is None:
                        raise TraceExecutionUnavailableError


__all__ = [
    "LoadedTraceAnalysis",
    "PreparedTraceExecution",
    "SQLAlchemyTraceExecutionRepository",
    "TraceExecutionArtifact",
    "TraceExecutionError",
    "TraceExecutionNotFoundError",
    "TraceExecutionRepository",
    "TraceExecutionService",
    "TraceExecutionUnavailableError",
    "canonical_trace_config_hash",
    "canonical_trace_input_manifest_hash",
]
