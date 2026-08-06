"""Create tenant-scoped Android memory capture manifests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import select

from perfpilot_api.db.tenant.models import Analysis, ApplicationVersion, Artifact, ScenarioResult
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.engines.android_memory_contracts import (
    MemoryArtifactRef,
    MemoryCaptureManifest,
    MemorySubject,
)
from perfpilot_api.services.internal_artifacts import (
    InternalArtifactConflictError,
    InternalArtifactSink,
    InternalArtifactUnavailableError,
    manifest_artifact_id,
)


_ALLOWED_ARTIFACT_KINDS = frozenset(
    {"capture_manifest", "log", "memory_evidence", "screenshot", "trace"}
)
_DEVICE_CAPTURE_NAMESPACE = UUID("91a34792-7cb1-5ce6-9279-7db821e60654")


class MemoryCaptureError(RuntimeError):
    """Stable memory capture failure without tenant-private metadata."""


class MemoryCaptureInvalidRequestError(MemoryCaptureError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("memory capture request is invalid")


class MemoryCaptureNotFoundError(MemoryCaptureError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("memory capture inputs were not found")


class MemoryCaptureConflictError(MemoryCaptureError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("memory capture idempotency conflict")


class MemoryCaptureUnavailableError(MemoryCaptureError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("memory capture service is unavailable")


@dataclass(frozen=True, slots=True)
class ReferencedArtifact:
    artifact_id: UUID
    analysis_id: UUID
    artifact_kind: str
    state: str
    expires_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryAnalysisContext:
    analysis_id: UUID
    analysis_mode: str
    state: str
    tombstoned_at: datetime | None
    tenant_resource_version: int
    package_name: str
    artifacts: tuple[ReferencedArtifact, ...]
    memory_scenario_state: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedMemoryCapture:
    artifact_id: UUID
    manifest: MemoryCaptureManifest
    manifest_sha256: str


class MemoryCaptureRepository(Protocol):
    async def load_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> MemoryAnalysisContext: ...

    async def load_device_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> MemoryAnalysisContext: ...


def device_memory_capture_id(analysis_id: UUID, artifact_id: UUID) -> UUID:
    if not isinstance(analysis_id, UUID) or not isinstance(artifact_id, UUID):
        raise ValueError("device memory capture identity is invalid")
    return uuid5(_DEVICE_CAPTURE_NAMESPACE, f"{analysis_id}:{artifact_id}")


class SQLAlchemyMemoryCaptureRepository:
    """Load Analysis and Artifact ownership only through the authenticated tenant route."""

    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    async def load_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> MemoryAnalysisContext:
        async with self._tenant_router.session(team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            if type(routed_version) is not int or routed_version < 1:
                raise MemoryCaptureUnavailableError
            result = await session.execute(
                select(Analysis, ApplicationVersion.package_name)
                .join(
                    ApplicationVersion,
                    ApplicationVersion.id == Analysis.application_version_id,
                )
                .where(Analysis.id == analysis_id)
            )
            analysis_row = result.one_or_none()
            if analysis_row is None:
                raise MemoryCaptureNotFoundError
            analysis, package_name = analysis_row
            rows = tuple(
                (
                    await session.scalars(
                        select(Artifact).where(
                            Artifact.id.in_(artifact_ids),
                            Artifact.analysis_id == analysis_id,
                        )
                    )
                ).all()
            )
        return MemoryAnalysisContext(
            analysis_id=analysis.id,
            analysis_mode=analysis.analysis_mode,
            state=analysis.state,
            tombstoned_at=analysis.tombstoned_at,
            tenant_resource_version=routed_version,
            package_name=package_name,
            artifacts=tuple(
                ReferencedArtifact(
                    artifact_id=row.id,
                    analysis_id=analysis_id if row.analysis_id is None else row.analysis_id,
                    artifact_kind=row.artifact_kind,
                    state=row.state,
                    expires_at=row.expires_at,
                    deleted_at=row.deleted_at,
                )
                for row in rows
            ),
        )

    async def load_device_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> MemoryAnalysisContext:
        async with self._tenant_router.session(team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            if type(routed_version) is not int or routed_version < 1:
                raise MemoryCaptureUnavailableError
            result = await session.execute(
                select(Analysis, ApplicationVersion.package_name, ScenarioResult.state)
                .join(
                    ApplicationVersion,
                    ApplicationVersion.id == Analysis.application_version_id,
                )
                .join(
                    ScenarioResult,
                    ScenarioResult.analysis_id == Analysis.id,
                )
                .where(
                    Analysis.id == analysis_id,
                    ScenarioResult.scenario_type == "memory_cycle",
                )
            )
            analysis_row = result.one_or_none()
            if analysis_row is None:
                raise MemoryCaptureNotFoundError
            analysis, package_name, scenario_state = analysis_row
            rows = tuple(
                (
                    await session.scalars(
                        select(Artifact).where(
                            Artifact.analysis_id == analysis_id,
                            Artifact.artifact_kind == "memory_evidence",
                        )
                    )
                ).all()
            )
        return MemoryAnalysisContext(
            analysis_id=analysis.id,
            analysis_mode=analysis.analysis_mode,
            state=analysis.state,
            tombstoned_at=analysis.tombstoned_at,
            tenant_resource_version=routed_version,
            package_name=package_name,
            artifacts=tuple(
                ReferencedArtifact(
                    artifact_id=row.id,
                    analysis_id=analysis_id if row.analysis_id is None else row.analysis_id,
                    artifact_kind=row.artifact_kind,
                    state=row.state,
                    expires_at=row.expires_at,
                    deleted_at=row.deleted_at,
                )
                for row in rows
            ),
            memory_scenario_state=scenario_state,
        )


class MemoryCaptureService:
    def __init__(
        self,
        *,
        repository: MemoryCaptureRepository,
        manifest_sink: InternalArtifactSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._manifest_sink = manifest_sink
        self._clock = clock
        self._uuid_source = uuid_source

    async def create_capture(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        phase: Literal["single", "before", "after", "cooldown"],
        source: Literal["manual_upload"],
        captured_at: datetime | None,
        subject: MemorySubject,
        artifacts: tuple[MemoryArtifactRef, ...],
    ) -> CreatedMemoryCapture:
        if source != "manual_upload" or not 1 <= len(artifacts) <= 2048:
            raise MemoryCaptureInvalidRequestError
        artifact_ids = tuple(reference.artifact_id for reference in artifacts)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise MemoryCaptureInvalidRequestError
        context = await self._load_context(
            team_id=team_id,
            analysis_id=analysis_id,
            artifact_ids=artifact_ids,
        )
        self._validate_context(
            context,
            analysis_id=analysis_id,
            analysis_mode="memory_upload",
            artifact_ids=artifact_ids,
        )
        capture_id = self._uuid_source()
        if not isinstance(capture_id, UUID):
            raise MemoryCaptureUnavailableError
        return await self._publish(
            team_id=team_id,
            analysis_id=analysis_id,
            context=context,
            capture_id=capture_id,
            phase=phase,
            source=source,
            captured_at=captured_at,
            subject=subject,
            artifacts=artifacts,
        )

    async def create_device_capture(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> CreatedMemoryCapture:
        try:
            context = await self._repository.load_device_context(
                team_id=team_id,
                analysis_id=analysis_id,
            )
        except MemoryCaptureNotFoundError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryCaptureUnavailableError from None

        if len(context.artifacts) != 1:
            raise MemoryCaptureNotFoundError
        evidence = context.artifacts[0]
        artifact_ids = (evidence.artifact_id,)
        self._validate_context(
            context,
            analysis_id=analysis_id,
            analysis_mode="device",
            artifact_ids=artifact_ids,
        )
        if (
            context.memory_scenario_state not in {"analyzing", "completed"}
            or evidence.artifact_kind != "memory_evidence"
        ):
            raise MemoryCaptureNotFoundError
        try:
            subject = MemorySubject(package=context.package_name)
            artifacts = (
                MemoryArtifactRef(
                    artifact_id=evidence.artifact_id,
                    role="handoff_archive",
                ),
            )
        except ValidationError:
            raise MemoryCaptureUnavailableError from None
        return await self._publish(
            team_id=team_id,
            analysis_id=analysis_id,
            context=context,
            capture_id=device_memory_capture_id(analysis_id, evidence.artifact_id),
            phase="single",
            source="adb_agent",
            captured_at=None,
            subject=subject,
            artifacts=artifacts,
        )

    async def _load_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> MemoryAnalysisContext:
        try:
            return await self._repository.load_context(
                team_id=team_id,
                analysis_id=analysis_id,
                artifact_ids=artifact_ids,
            )
        except MemoryCaptureNotFoundError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryCaptureUnavailableError from None

    def _validate_context(
        self,
        context: MemoryAnalysisContext,
        *,
        analysis_id: UUID,
        analysis_mode: Literal["memory_upload", "device"],
        artifact_ids: tuple[UUID, ...],
    ) -> None:
        routed_version = getattr(context, "tenant_resource_version", None)
        if type(routed_version) is not int or routed_version < 1:
            raise MemoryCaptureUnavailableError
        now = self._clock()
        if (
            context.analysis_id != analysis_id
            or context.analysis_mode != analysis_mode
            or context.state == "deleted"
            or context.tombstoned_at is not None
            or len(context.artifacts) != len(artifact_ids)
        ):
            raise MemoryCaptureNotFoundError
        by_id = {artifact.artifact_id: artifact for artifact in context.artifacts}
        if len(by_id) != len(context.artifacts):
            raise MemoryCaptureNotFoundError
        for artifact_id in artifact_ids:
            artifact = by_id.get(artifact_id)
            if (
                artifact is None
                or artifact.analysis_id != analysis_id
                or artifact.artifact_kind not in _ALLOWED_ARTIFACT_KINDS
                or artifact.state != "finalized"
                or artifact.deleted_at is not None
                or artifact.expires_at <= now
            ):
                raise MemoryCaptureNotFoundError

    async def _publish(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        context: MemoryAnalysisContext,
        capture_id: UUID,
        phase: Literal["single", "before", "after", "cooldown"],
        source: Literal["manual_upload", "adb_agent"],
        captured_at: datetime | None,
        subject: MemorySubject,
        artifacts: tuple[MemoryArtifactRef, ...],
    ) -> CreatedMemoryCapture:
        artifact_id = manifest_artifact_id(capture_id)
        try:
            manifest = MemoryCaptureManifest(
                schema_version="1.0",
                analysis_id=analysis_id,
                capture_id=capture_id,
                phase=phase,
                source=source,
                captured_at=captured_at,
                subject=subject,
                artifacts=artifacts,
            )
        except ValidationError:
            raise MemoryCaptureInvalidRequestError from None
        payload = manifest.canonical_bytes()
        sink_conflict = False
        sink_unavailable = False
        stored_id: UUID | None = None
        try:
            stored_id = await self._manifest_sink.write_json(
                team_id=team_id,
                expected_tenant_resource_version=context.tenant_resource_version,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                artifact_kind="memory_capture_manifest",
                payload=payload,
            )
        except InternalArtifactConflictError:
            sink_conflict = True
        except InternalArtifactUnavailableError:
            sink_unavailable = True
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            sink_unavailable = True
        if sink_conflict:
            raise MemoryCaptureConflictError
        if sink_unavailable:
            raise MemoryCaptureUnavailableError
        if stored_id != artifact_id:
            raise MemoryCaptureUnavailableError
        return CreatedMemoryCapture(
            artifact_id=artifact_id,
            manifest=manifest,
            manifest_sha256=manifest.sha256_hex(),
        )


__all__ = [
    "CreatedMemoryCapture",
    "MemoryAnalysisContext",
    "MemoryCaptureConflictError",
    "MemoryCaptureError",
    "MemoryCaptureInvalidRequestError",
    "MemoryCaptureNotFoundError",
    "MemoryCaptureRepository",
    "MemoryCaptureService",
    "MemoryCaptureUnavailableError",
    "ReferencedArtifact",
    "SQLAlchemyMemoryCaptureRepository",
    "device_memory_capture_id",
]
