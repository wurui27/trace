"""Reserve canonical engine-result artifacts in routed tenant databases."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.engines.canonical_results import (
    EngineResultValidationError,
    result_artifact_id,
)
from perfpilot_api.services.uploads import TenantBucket


_ARTIFACT_KIND = "engine_result"
_IDEMPOTENCY_PREFIX = "internal:engine_result:"
_JSON_MIME = "application/json"
_RETENTION = timedelta(days=30)
_REQUEST_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ENGINE_ANALYSIS_MODES = {
    "android_memory": "memory_upload",
    "smartperfetto": "trace_upload",
}
_T = TypeVar("_T")


class EngineResultArtifactError(RuntimeError):
    """A stable artifact failure that carries no tenant storage coordinates."""


class EngineResultConflictError(EngineResultArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("engine result integrity conflict")


class EngineResultUnavailableError(EngineResultArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("engine result service is unavailable")


@dataclass(frozen=True, slots=True)
class EngineResultArtifactRecord:
    artifact_id: UUID
    analysis_id: UUID
    upload_id: UUID
    idempotency_key: str
    request_hash: str
    artifact_kind: str
    mime_type: str
    size_bytes: int
    sha256_b64: str = field(repr=False)
    object_key: str = field(repr=False)
    state: str
    expires_at: datetime
    version: int
    version_id: str | None = field(repr=False)


class EngineResultArtifactRepository(Protocol):
    async def reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        engine_id: Literal["smartperfetto", "android_memory"],
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> EngineResultArtifactRecord: ...

    async def require_resource_version(self, tenant: TenantBucket) -> None: ...

    async def finalize(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_version: int,
        storage_version_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> EngineResultArtifactRecord | None: ...

    async def reload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> EngineResultArtifactRecord: ...


def _is_aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _is_checksum(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 44:
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 32 and base64.b64encode(decoded).decode("ascii") == value


def _is_storage_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value != "null"
        and len(value) <= 1024
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _object_key(*, analysis_id: UUID, artifact_id: UUID) -> str:
    return f"raw/analyses/{analysis_id}/internal/engine-results/{artifact_id}.json"


class SQLAlchemyEngineResultArtifactRepository:
    """Persist canonical result reservations only in the selected tenant database."""

    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    @staticmethod
    async def _guard(operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except EngineResultArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, IntegrityError):
                mapped_error: EngineResultArtifactError = EngineResultConflictError()
            else:
                mapped_error = EngineResultUnavailableError()
        raise mapped_error

    @asynccontextmanager
    async def _session(self, tenant: TenantBucket) -> AsyncIterator[AsyncSession]:
        if (
            not isinstance(tenant, TenantBucket)
            or not isinstance(tenant.team_id, UUID)
            or type(tenant.resource_version) is not int
            or tenant.resource_version < 1
        ):
            raise EngineResultUnavailableError
        async with self._tenant_router.session(tenant.team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            if (
                type(routed_version) is not int
                or routed_version < 1
                or routed_version != tenant.resource_version
            ):
                raise EngineResultUnavailableError
            yield session

    @staticmethod
    def _row_has_engine_shape(
        row: Artifact,
        *,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> bool:
        idempotency_key = row.idempotency_key
        if not isinstance(idempotency_key, str) or not idempotency_key.startswith(
            _IDEMPOTENCY_PREFIX
        ):
            return False
        execution_text = idempotency_key.removeprefix(_IDEMPOTENCY_PREFIX)
        try:
            execution_id = UUID(execution_text)
        except (ValueError, AttributeError):
            return False
        if str(execution_id) != execution_text:
            return False

        pending = (
            row.state == "pending"
            and row.version == 1
            and row.version_id is None
            and row.finalized_at is None
        )
        finalized = (
            row.state == "finalized"
            and row.version == 2
            and _is_storage_version(row.version_id)
            and _is_aware(row.finalized_at)
        )
        return (
            row.id == artifact_id
            and row.analysis_id == analysis_id
            and row.application_version_id is None
            and row.scenario_result_id is None
            and row.sample_attempt_id is None
            and row.upload_id == artifact_id
            and result_artifact_id(execution_id) == artifact_id
            and isinstance(row.request_hash, str)
            and _REQUEST_HASH.fullmatch(row.request_hash) is not None
            and row.artifact_kind == _ARTIFACT_KIND
            and row.mime_type == _JSON_MIME
            and type(row.size_bytes) is int
            and row.size_bytes > 0
            and _is_checksum(row.sha256_b64)
            and row.object_key == _object_key(analysis_id=analysis_id, artifact_id=artifact_id)
            and row.deleted_at is None
            and _is_aware(row.expires_at)
            and (pending or finalized)
        )

    @classmethod
    def _record(
        cls,
        row: Artifact,
        *,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> EngineResultArtifactRecord:
        if (
            not cls._row_has_engine_shape(
                row,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )
            or row.analysis_id is None
            or row.idempotency_key is None
            or row.request_hash is None
        ):
            raise EngineResultConflictError
        return EngineResultArtifactRecord(
            artifact_id=row.id,
            analysis_id=row.analysis_id,
            upload_id=row.upload_id,
            idempotency_key=row.idempotency_key,
            request_hash=row.request_hash,
            artifact_kind=row.artifact_kind,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            sha256_b64=row.sha256_b64,
            object_key=row.object_key,
            state=row.state,
            expires_at=row.expires_at,
            version=row.version,
            version_id=row.version_id,
        )

    @classmethod
    def _reservation_matches(
        cls,
        row: Artifact,
        *,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> bool:
        return (
            cls._row_has_engine_shape(
                row,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )
            and row.idempotency_key == f"{_IDEMPOTENCY_PREFIX}{execution_id}"
            and row.request_hash == request_hash
            and row.size_bytes == size_bytes
            and row.sha256_b64 == sha256_b64
            and row.expires_at > now
        )

    async def reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        engine_id: Literal["smartperfetto", "android_memory"],
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> EngineResultArtifactRecord:
        return await self._guard(
            self._reserve(
                tenant=tenant,
                analysis_id=analysis_id,
                execution_id=execution_id,
                artifact_id=artifact_id,
                engine_id=engine_id,
                request_hash=request_hash,
                size_bytes=size_bytes,
                sha256_b64=sha256_b64,
                now=now,
            )
        )

    async def _reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        engine_id: str,
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> EngineResultArtifactRecord:
        analysis_mode = _ENGINE_ANALYSIS_MODES.get(engine_id)
        if (
            not isinstance(analysis_id, UUID)
            or not isinstance(execution_id, UUID)
            or not isinstance(artifact_id, UUID)
            or analysis_mode is None
            or artifact_id != result_artifact_id(execution_id)
            or not isinstance(request_hash, str)
            or _REQUEST_HASH.fullmatch(request_hash) is None
            or type(size_bytes) is not int
            or size_bytes < 1
            or not _is_checksum(sha256_b64)
            or not _is_aware(now)
        ):
            raise EngineResultConflictError

        idempotency_key = f"{_IDEMPOTENCY_PREFIX}{execution_id}"
        object_key = _object_key(analysis_id=analysis_id, artifact_id=artifact_id)
        async with self._session(tenant) as session:
            analysis = await session.scalar(
                select(Analysis.id)
                .where(
                    Analysis.id == analysis_id,
                    Analysis.analysis_mode == analysis_mode,
                    Analysis.tombstoned_at.is_(None),
                    Analysis.state != "deleted",
                )
                .with_for_update(read=True)
            )
            if analysis is None:
                raise EngineResultConflictError

            await session.scalar(
                postgresql_insert(Artifact)
                .values(
                    id=artifact_id,
                    analysis_id=analysis_id,
                    upload_id=artifact_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    artifact_kind=_ARTIFACT_KIND,
                    mime_type=_JSON_MIME,
                    size_bytes=size_bytes,
                    sha256_b64=sha256_b64,
                    object_key=object_key,
                    version_id=None,
                    state="pending",
                    finalized_at=None,
                    expires_at=now + _RETENTION,
                    deleted_at=None,
                    version=1,
                )
                .on_conflict_do_nothing()
                .returning(Artifact.id)
            )
            row = await session.get(Artifact, artifact_id)
            if row is None or not self._reservation_matches(
                row,
                analysis_id=analysis_id,
                execution_id=execution_id,
                artifact_id=artifact_id,
                request_hash=request_hash,
                size_bytes=size_bytes,
                sha256_b64=sha256_b64,
                now=now,
            ):
                raise EngineResultConflictError
            return self._record(row, analysis_id=analysis_id, artifact_id=artifact_id)

    async def require_resource_version(self, tenant: TenantBucket) -> None:
        await self._guard(self._require_resource_version(tenant))

    async def _require_resource_version(self, tenant: TenantBucket) -> None:
        async with self._session(tenant):
            return

    async def finalize(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_version: int,
        storage_version_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> EngineResultArtifactRecord | None:
        return await self._guard(
            self._finalize(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                expected_version=expected_version,
                storage_version_id=storage_version_id,
                now=now,
                expires_at=expires_at,
            )
        )

    async def _finalize(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_version: int,
        storage_version_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> EngineResultArtifactRecord | None:
        if (
            not isinstance(analysis_id, UUID)
            or not isinstance(artifact_id, UUID)
            or type(expected_version) is not int
            or expected_version < 1
            or not _is_storage_version(storage_version_id)
            or not _is_aware(now)
            or not _is_aware(expires_at)
            or expires_at <= now
        ):
            raise EngineResultConflictError
        async with self._session(tenant) as session:
            row = await session.scalar(
                update(Artifact)
                .where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                    Artifact.state == "pending",
                    Artifact.version == expected_version,
                    Artifact.expires_at == expires_at,
                    Artifact.deleted_at.is_(None),
                )
                .values(
                    state="finalized",
                    version_id=storage_version_id,
                    finalized_at=now,
                    updated_at=now,
                    version=Artifact.version + 1,
                )
                .returning(Artifact)
            )
            if row is None:
                return None
            return self._record(row, analysis_id=analysis_id, artifact_id=artifact_id)

    async def reload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> EngineResultArtifactRecord:
        return await self._guard(
            self._reload(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )
        )

    async def _reload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> EngineResultArtifactRecord:
        if not isinstance(analysis_id, UUID) or not isinstance(artifact_id, UUID):
            raise EngineResultConflictError
        async with self._session(tenant) as session:
            row = await session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                )
            )
            if row is None:
                raise EngineResultConflictError
            return self._record(row, analysis_id=analysis_id, artifact_id=artifact_id)


__all__ = [
    "EngineResultArtifactError",
    "EngineResultArtifactRecord",
    "EngineResultArtifactRepository",
    "EngineResultConflictError",
    "EngineResultUnavailableError",
    "EngineResultValidationError",
    "SQLAlchemyEngineResultArtifactRepository",
]
