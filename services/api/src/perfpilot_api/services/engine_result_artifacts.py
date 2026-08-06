"""Reserve canonical engine-result artifacts in routed tenant databases."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.engines.canonical_results import (
    CanonicalEngineResult,
    EngineResultValidationError,
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.services.uploads import BucketResolver, TenantBucket


_ARTIFACT_KIND = "engine_result"
_IDEMPOTENCY_PREFIX = "internal:engine_result:"
_JSON_MIME = "application/json"
_RETENTION = timedelta(days=30)
_REQUEST_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ENGINE_ANALYSIS_MODES = {
    "android_memory": frozenset({"memory_upload"}),
    "smartperfetto": frozenset({"trace_upload", "device"}),
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


class EngineResultSink(Protocol):
    async def write(self, request: EngineResultWrite) -> UUID: ...


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
        analysis_modes = _ENGINE_ANALYSIS_MODES.get(engine_id)
        if (
            not isinstance(analysis_id, UUID)
            or not isinstance(execution_id, UUID)
            or not isinstance(artifact_id, UUID)
            or analysis_modes is None
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
                    Analysis.analysis_mode.in_(analysis_modes),
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


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _safe_version_id(value: object) -> str | None:
    return value if _is_storage_version(value) else None


def _valid_bucket(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 255
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


class S3EngineResultSink:
    """Persist one canonical result and pin one verified immutable S3 version."""

    def __init__(
        self,
        *,
        repository: EngineResultArtifactRepository,
        bucket_resolver: BucketResolver,
        client: Any,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._bucket_resolver = bucket_resolver
        self._client = client
        self._clock = clock

    @staticmethod
    async def _dependency(operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except EngineResultArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            failed = True
        if failed:
            raise EngineResultUnavailableError
        raise AssertionError("unreachable")

    @staticmethod
    def _now(clock: Callable[[], datetime]) -> datetime:
        try:
            now = clock()
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            failed = True
        else:
            failed = False
        if failed:
            raise EngineResultUnavailableError
        if not _is_aware(now):
            raise EngineResultUnavailableError
        return now

    @staticmethod
    def _record_matches(
        record: object,
        *,
        request: EngineResultWrite,
        canonical: CanonicalEngineResult,
        object_key: str,
        now: datetime,
    ) -> bool:
        if not isinstance(record, EngineResultArtifactRecord):
            return False
        pending = record.state == "pending" and record.version == 1 and record.version_id is None
        finalized = (
            record.state == "finalized"
            and record.version == 2
            and _safe_version_id(record.version_id) is not None
        )
        return (
            record.artifact_id == request.artifact_id
            and record.analysis_id == request.analysis_id
            and record.upload_id == request.artifact_id
            and record.idempotency_key == f"{_IDEMPOTENCY_PREFIX}{request.execution_id}"
            and record.request_hash == canonical.request_hash_hex
            and record.artifact_kind == _ARTIFACT_KIND
            and record.mime_type == _JSON_MIME
            and type(record.size_bytes) is int
            and record.size_bytes == len(canonical.canonical_bytes)
            and isinstance(record.sha256_b64, str)
            and hmac.compare_digest(
                record.sha256_b64,
                canonical.checksum_sha256_b64,
            )
            and isinstance(record.object_key, str)
            and hmac.compare_digest(record.object_key, object_key)
            and type(record.version) is int
            and _is_aware(record.expires_at)
            and now < record.expires_at <= now + _RETENTION
            and (pending or finalized)
        )

    @staticmethod
    def _verified_metadata(
        response: object,
        *,
        expected_version_id: str,
        checksum: str,
        size_bytes: int,
    ) -> Mapping[str, object]:
        metadata = _mapping(response)
        if metadata is None:
            raise EngineResultUnavailableError
        returned_version = _safe_version_id(metadata.get("VersionId"))
        returned_checksum = metadata.get("ChecksumSHA256")
        if (
            returned_version != expected_version_id
            or not isinstance(returned_checksum, str)
            or not hmac.compare_digest(returned_checksum, checksum)
            or metadata.get("ContentType") != _JSON_MIME
            or type(metadata.get("ContentLength")) is not int
            or metadata.get("ContentLength") != size_bytes
            or metadata.get("DeleteMarker", False) is not False
        ):
            raise EngineResultConflictError
        return metadata

    @staticmethod
    def _read_exact_sync(
        client: Any,
        *,
        bucket: str,
        record: EngineResultArtifactRecord,
        payload: bytes,
    ) -> None:
        version_id = _safe_version_id(record.version_id)
        if version_id is None:
            raise EngineResultConflictError
        response = client.get_object(
            Bucket=bucket,
            Key=record.object_key,
            VersionId=version_id,
            ChecksumMode="ENABLED",
        )
        metadata = _mapping(response)
        body = None if metadata is None else metadata.get("Body")
        if body is None:
            raise EngineResultUnavailableError
        close = getattr(body, "close", None)
        if not callable(close):
            raise EngineResultUnavailableError
        try:
            read = getattr(body, "read", None)
            if not callable(read):
                raise EngineResultUnavailableError
            S3EngineResultSink._verified_metadata(
                response,
                expected_version_id=version_id,
                checksum=record.sha256_b64,
                size_bytes=record.size_bytes,
            )
            stored_payload = read()
        finally:
            close()
        if not isinstance(stored_payload, bytes) or not hmac.compare_digest(
            stored_payload,
            payload,
        ):
            raise EngineResultConflictError

    async def _fence(self, tenant: TenantBucket) -> None:
        await self._dependency(self._repository.require_resource_version(tenant))

    async def _read_exact(
        self,
        *,
        tenant: TenantBucket,
        record: EngineResultArtifactRecord,
        payload: bytes,
    ) -> None:
        await self._dependency(
            asyncio.to_thread(
                self._read_exact_sync,
                self._client,
                bucket=tenant.bucket,
                record=record,
                payload=payload,
            )
        )

    async def write(self, request: EngineResultWrite) -> UUID:
        canonical = canonicalize_engine_result(request)
        now = self._now(self._clock)
        object_key = _object_key(
            analysis_id=request.analysis_id,
            artifact_id=request.artifact_id,
        )

        tenant = await self._dependency(
            self._bucket_resolver.active_for_team(request.team_id)
        )
        if (
            not isinstance(tenant, TenantBucket)
            or tenant.team_id != request.team_id
            or type(tenant.resource_version) is not int
            or tenant.resource_version != request.tenant_resource_version
            or not _valid_bucket(tenant.bucket)
        ):
            raise EngineResultUnavailableError

        record = await self._dependency(
            self._repository.reserve(
                tenant=tenant,
                analysis_id=request.analysis_id,
                execution_id=request.execution_id,
                artifact_id=request.artifact_id,
                engine_id=request.engine_id,
                request_hash=canonical.request_hash_hex,
                size_bytes=len(canonical.canonical_bytes),
                sha256_b64=canonical.checksum_sha256_b64,
                now=now,
            )
        )
        if not self._record_matches(
            record,
            request=request,
            canonical=canonical,
            object_key=object_key,
            now=now,
        ):
            raise EngineResultConflictError

        await self._fence(tenant)
        if record.state == "finalized":
            await self._read_exact(
                tenant=tenant,
                record=record,
                payload=canonical.canonical_bytes,
            )
            await self._fence(tenant)
            return request.artifact_id

        await self._fence(tenant)
        receipt = await self._dependency(
            asyncio.to_thread(
                self._client.put_object,
                Bucket=tenant.bucket,
                Key=object_key,
                Body=canonical.canonical_bytes,
                ContentType=_JSON_MIME,
                ChecksumSHA256=canonical.checksum_sha256_b64,
            )
        )
        receipt_mapping = _mapping(receipt)
        if receipt_mapping is None:
            raise EngineResultUnavailableError
        storage_version_id = _safe_version_id(receipt_mapping.get("VersionId"))
        receipt_checksum = receipt_mapping.get("ChecksumSHA256")
        if storage_version_id is None or not isinstance(receipt_checksum, str):
            raise EngineResultUnavailableError
        if not hmac.compare_digest(receipt_checksum, canonical.checksum_sha256_b64):
            raise EngineResultConflictError

        head = await self._dependency(
            asyncio.to_thread(
                self._client.head_object,
                Bucket=tenant.bucket,
                Key=object_key,
                VersionId=storage_version_id,
                ChecksumMode="ENABLED",
            )
        )
        self._verified_metadata(
            head,
            expected_version_id=storage_version_id,
            checksum=canonical.checksum_sha256_b64,
            size_bytes=len(canonical.canonical_bytes),
        )
        await self._fence(tenant)

        finalized = await self._dependency(
            self._repository.finalize(
                tenant=tenant,
                analysis_id=request.analysis_id,
                artifact_id=request.artifact_id,
                expected_version=record.version,
                storage_version_id=storage_version_id,
                now=now,
                expires_at=record.expires_at,
            )
        )
        if finalized is None:
            finalized = await self._dependency(
                self._repository.reload(
                    tenant=tenant,
                    analysis_id=request.analysis_id,
                    artifact_id=request.artifact_id,
                )
            )
            if not self._record_matches(
                finalized,
                request=request,
                canonical=canonical,
                object_key=object_key,
                now=now,
            ) or finalized.state != "finalized":
                raise EngineResultConflictError
            await self._fence(tenant)
            await self._read_exact(
                tenant=tenant,
                record=finalized,
                payload=canonical.canonical_bytes,
            )
        elif (
            not self._record_matches(
                finalized,
                request=request,
                canonical=canonical,
                object_key=object_key,
                now=now,
            )
            or finalized.state != "finalized"
            or finalized.version_id != storage_version_id
        ):
            raise EngineResultConflictError

        await self._fence(tenant)
        return request.artifact_id


__all__ = [
    "EngineResultArtifactError",
    "EngineResultArtifactRecord",
    "EngineResultArtifactRepository",
    "EngineResultConflictError",
    "EngineResultSink",
    "EngineResultUnavailableError",
    "EngineResultValidationError",
    "S3EngineResultSink",
    "SQLAlchemyEngineResultArtifactRepository",
]
