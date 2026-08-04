"""Persist private AI projection and synthesis JSON as immutable tenant artifacts."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.services.uploads import BucketResolver, TenantBucket


ArtifactKind = Literal["ai_projection", "ai_synthesis_result"]

_PROJECTION_NAMESPACE = UUID("98914389-ae6a-5f96-8cfa-ef7d7b80da12")
_SYNTHESIS_NAMESPACE = UUID("784674f2-829a-585a-a302-874d31d09ef1")
_JSON_MIME = "application/json"
_RETENTION = timedelta(days=30)
_REQUEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


class SynthesisArtifactError(RuntimeError):
    """Stable, storage-coordinate-free artifact failure."""


class SynthesisArtifactConflictError(SynthesisArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("synthesis artifact integrity conflict")


class SynthesisArtifactUnavailableError(SynthesisArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("synthesis artifact service is unavailable")


@dataclass(frozen=True, slots=True)
class SynthesisArtifactWrite:
    team_id: UUID
    analysis_id: UUID
    tenant_resource_version: int
    artifact_id: UUID
    kind: ArtifactKind
    canonical_bytes: bytes = field(repr=False)
    sha256_b64: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SynthesisArtifactRecord:
    artifact_id: UUID
    analysis_id: UUID
    artifact_kind: str
    mime_type: str
    size_bytes: int
    sha256_b64: str = field(repr=False)
    object_key: str = field(repr=False)
    idempotency_key: str
    state: str
    expires_at: datetime
    version: int
    version_id: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredSynthesisArtifact:
    artifact_id: UUID
    analysis_id: UUID
    kind: ArtifactKind
    size_bytes: int
    sha256_b64: str = field(repr=False)


class SynthesisArtifactRepository(Protocol):
    async def reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        kind: ArtifactKind,
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> SynthesisArtifactRecord: ...

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
    ) -> SynthesisArtifactRecord | None: ...

    async def reload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> SynthesisArtifactRecord: ...


def projection_artifact_id(canonical_artifact_id: UUID, normalizer_version: str) -> UUID:
    if (
        not isinstance(canonical_artifact_id, UUID)
        or not isinstance(normalizer_version, str)
        or not normalizer_version
        or len(normalizer_version) > 128
    ):
        raise SynthesisArtifactConflictError
    return uuid5(_PROJECTION_NAMESPACE, f"{canonical_artifact_id}:{normalizer_version}")


def synthesis_artifact_id(synthesis_execution_id: UUID, checksum: str) -> UUID:
    if not isinstance(synthesis_execution_id, UUID) or not _is_checksum(checksum):
        raise SynthesisArtifactConflictError
    return uuid5(_SYNTHESIS_NAMESPACE, f"{synthesis_execution_id}:{checksum}")


def artifact_key(analysis_id: UUID, artifact_id: UUID, kind: ArtifactKind) -> str:
    if (
        not isinstance(analysis_id, UUID)
        or not isinstance(artifact_id, UUID)
        or kind not in {"ai_projection", "ai_synthesis_result"}
    ):
        raise SynthesisArtifactConflictError
    directory = kind.replace("_", "-")
    return f"raw/analyses/{analysis_id}/internal/{directory}/{artifact_id}.json"


def _is_checksum(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 44:
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 32 and base64.b64encode(decoded).decode("ascii") == value


def _is_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _safe_version_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value == "null"
        or len(value) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def _valid_bucket(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 255
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _request_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_write(request: object) -> SynthesisArtifactWrite:
    if not isinstance(request, SynthesisArtifactWrite):
        raise SynthesisArtifactConflictError
    if (
        not isinstance(request.team_id, UUID)
        or not isinstance(request.analysis_id, UUID)
        or not isinstance(request.artifact_id, UUID)
        or type(request.tenant_resource_version) is not int
        or request.tenant_resource_version < 1
        or request.kind not in {"ai_projection", "ai_synthesis_result"}
        or not isinstance(request.canonical_bytes, bytes)
        or not request.canonical_bytes
        or not _is_checksum(request.sha256_b64)
    ):
        raise SynthesisArtifactConflictError
    checksum = base64.b64encode(hashlib.sha256(request.canonical_bytes).digest()).decode(
        "ascii"
    )
    try:
        document = json.loads(request.canonical_bytes)
        canonical = canonical_json_bytes(document)
    except Exception:
        raise SynthesisArtifactConflictError from None
    if (
        not hmac.compare_digest(checksum, request.sha256_b64)
        or not hmac.compare_digest(canonical, request.canonical_bytes)
    ):
        raise SynthesisArtifactConflictError
    return request


class SQLAlchemySynthesisArtifactRepository:
    """Reserve AI artifacts only inside the selected tenant database."""

    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    @staticmethod
    async def _guard(operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except SynthesisArtifactError:
            raise
        except IntegrityError:
            raise SynthesisArtifactConflictError from None
        except Exception:
            raise SynthesisArtifactUnavailableError from None

    @asynccontextmanager
    async def _session(self, tenant: TenantBucket) -> AsyncIterator[AsyncSession]:
        if (
            not isinstance(tenant, TenantBucket)
            or not isinstance(tenant.team_id, UUID)
            or type(tenant.resource_version) is not int
            or tenant.resource_version < 1
        ):
            raise SynthesisArtifactUnavailableError
        async with self._tenant_router.session(tenant.team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            if (
                type(routed_version) is not int
                or routed_version != tenant.resource_version
            ):
                raise SynthesisArtifactUnavailableError
            yield session

    @staticmethod
    def _row_matches_shape(
        row: Artifact,
        *,
        analysis_id: UUID,
        artifact_id: UUID,
        kind: ArtifactKind | None = None,
    ) -> bool:
        if row.artifact_kind not in {"ai_projection", "ai_synthesis_result"}:
            return False
        expected_kind = row.artifact_kind if kind is None else kind
        expected_key = artifact_key(analysis_id, artifact_id, expected_kind)  # type: ignore[arg-type]
        pending = (
            row.state == "pending"
            and row.version == 1
            and row.version_id is None
            and row.finalized_at is None
        )
        finalized = (
            row.state == "finalized"
            and row.version == 2
            and _safe_version_id(row.version_id) is not None
            and _is_aware(row.finalized_at)
        )
        return (
            row.id == artifact_id
            and row.analysis_id == analysis_id
            and row.application_version_id is None
            and row.scenario_result_id is None
            and row.sample_attempt_id is None
            and row.upload_id == artifact_id
            and row.artifact_kind == expected_kind
            and row.idempotency_key == f"internal:{expected_kind}:{artifact_id}"
            and isinstance(row.request_hash, str)
            and _REQUEST_HASH.fullmatch(row.request_hash) is not None
            and row.mime_type == _JSON_MIME
            and type(row.size_bytes) is int
            and row.size_bytes > 0
            and _is_checksum(row.sha256_b64)
            and row.object_key == expected_key
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
        kind: ArtifactKind | None = None,
    ) -> SynthesisArtifactRecord:
        if (
            not cls._row_matches_shape(
                row,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                kind=kind,
            )
            or row.analysis_id is None
            or row.idempotency_key is None
        ):
            raise SynthesisArtifactConflictError
        return SynthesisArtifactRecord(
            artifact_id=row.id,
            analysis_id=row.analysis_id,
            artifact_kind=row.artifact_kind,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            sha256_b64=row.sha256_b64,
            object_key=row.object_key,
            idempotency_key=row.idempotency_key,
            state=row.state,
            expires_at=row.expires_at,
            version=row.version,
            version_id=row.version_id,
        )

    async def reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        kind: ArtifactKind,
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> SynthesisArtifactRecord:
        return await self._guard(
            self._reserve(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                kind=kind,
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
        artifact_id: UUID,
        kind: ArtifactKind,
        request_hash: str,
        size_bytes: int,
        sha256_b64: str,
        now: datetime,
    ) -> SynthesisArtifactRecord:
        if (
            not isinstance(analysis_id, UUID)
            or not isinstance(artifact_id, UUID)
            or kind not in {"ai_projection", "ai_synthesis_result"}
            or not isinstance(request_hash, str)
            or _REQUEST_HASH.fullmatch(request_hash) is None
            or type(size_bytes) is not int
            or size_bytes < 1
            or not _is_checksum(sha256_b64)
            or not _is_aware(now)
        ):
            raise SynthesisArtifactConflictError
        key = artifact_key(analysis_id, artifact_id, kind)
        idempotency_key = f"internal:{kind}:{artifact_id}"
        async with self._session(tenant) as session:
            analysis = await session.scalar(
                select(Analysis.id)
                .where(
                    Analysis.id == analysis_id,
                    Analysis.analysis_mode == "trace_upload",
                    Analysis.tombstoned_at.is_(None),
                    Analysis.state != "deleted",
                )
                .with_for_update(read=True)
            )
            if analysis is None:
                raise SynthesisArtifactConflictError
            await session.scalar(
                postgresql_insert(Artifact)
                .values(
                    id=artifact_id,
                    analysis_id=analysis_id,
                    upload_id=artifact_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    artifact_kind=kind,
                    mime_type=_JSON_MIME,
                    size_bytes=size_bytes,
                    sha256_b64=sha256_b64,
                    object_key=key,
                    state="pending",
                    version_id=None,
                    finalized_at=None,
                    expires_at=now + _RETENTION,
                    deleted_at=None,
                    version=1,
                )
                .on_conflict_do_nothing()
                .returning(Artifact.id)
            )
            row = await session.get(Artifact, artifact_id)
            if (
                row is None
                or not self._row_matches_shape(
                    row,
                    analysis_id=analysis_id,
                    artifact_id=artifact_id,
                    kind=kind,
                )
                or row.request_hash != request_hash
                or row.size_bytes != size_bytes
                or not hmac.compare_digest(row.sha256_b64, sha256_b64)
                or row.expires_at <= now
            ):
                raise SynthesisArtifactConflictError
            return self._record(
                row,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                kind=kind,
            )

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
    ) -> SynthesisArtifactRecord | None:
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
    ) -> SynthesisArtifactRecord | None:
        if (
            not isinstance(analysis_id, UUID)
            or not isinstance(artifact_id, UUID)
            or type(expected_version) is not int
            or expected_version < 1
            or _safe_version_id(storage_version_id) is None
            or not _is_aware(now)
            or not _is_aware(expires_at)
            or expires_at <= now
        ):
            raise SynthesisArtifactConflictError
        async with self._session(tenant) as session:
            row = await session.scalar(
                update(Artifact)
                .where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                    Artifact.artifact_kind.in_(
                        ("ai_projection", "ai_synthesis_result")
                    ),
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
            return self._record(
                row,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )

    async def reload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> SynthesisArtifactRecord:
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
    ) -> SynthesisArtifactRecord:
        if not isinstance(analysis_id, UUID) or not isinstance(artifact_id, UUID):
            raise SynthesisArtifactConflictError
        async with self._session(tenant) as session:
            row = await session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                )
            )
            if row is None:
                raise SynthesisArtifactConflictError
            return self._record(
                row,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )


class S3SynthesisArtifactStore:
    """Write one private JSON artifact and pin one verified S3 object version."""

    def __init__(
        self,
        *,
        repository: SynthesisArtifactRepository,
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
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except SynthesisArtifactError:
            raise
        except Exception:
            raise SynthesisArtifactUnavailableError from None

    def _now(self) -> datetime:
        try:
            now = self._clock()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise SynthesisArtifactUnavailableError from None
        if not _is_aware(now):
            raise SynthesisArtifactUnavailableError
        return now

    @staticmethod
    def _matches(
        record: object,
        request: SynthesisArtifactWrite,
        *,
        key: str,
        now: datetime,
    ) -> bool:
        if not isinstance(record, SynthesisArtifactRecord):
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
            and record.artifact_kind == request.kind
            and record.mime_type == _JSON_MIME
            and record.size_bytes == len(request.canonical_bytes)
            and hmac.compare_digest(record.sha256_b64, request.sha256_b64)
            and hmac.compare_digest(record.object_key, key)
            and record.idempotency_key == f"internal:{request.kind}:{request.artifact_id}"
            and _is_aware(record.expires_at)
            and now < record.expires_at <= now + _RETENTION
            and (pending or finalized)
        )

    @staticmethod
    def _verify_metadata(
        value: object,
        *,
        version_id: str,
        request: SynthesisArtifactWrite,
    ) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise SynthesisArtifactUnavailableError
        checksum = value.get("ChecksumSHA256")
        if (
            value.get("VersionId") != version_id
            or not isinstance(checksum, str)
            or not hmac.compare_digest(checksum, request.sha256_b64)
            or value.get("ContentType") != _JSON_MIME
            or type(value.get("ContentLength")) is not int
            or value.get("ContentLength") != len(request.canonical_bytes)
            or value.get("DeleteMarker", False) is not False
        ):
            raise SynthesisArtifactConflictError
        return value

    async def _fence(self, tenant: TenantBucket) -> None:
        await self._dependency(self._repository.require_resource_version(tenant))

    async def _read_exact(
        self,
        tenant: TenantBucket,
        record: SynthesisArtifactRecord,
        request: SynthesisArtifactWrite,
    ) -> None:
        version_id = _safe_version_id(record.version_id)
        if version_id is None:
            raise SynthesisArtifactConflictError

        def read_sync() -> None:
            try:
                response = self._client.get_object(
                    Bucket=tenant.bucket,
                    Key=record.object_key,
                    VersionId=version_id,
                    ChecksumMode="ENABLED",
                )
            except Exception:
                raise SynthesisArtifactUnavailableError from None
            metadata = self._verify_metadata(
                response,
                version_id=version_id,
                request=request,
            )
            body = metadata.get("Body")
            read = getattr(body, "read", None)
            close = getattr(body, "close", None)
            if not callable(read) or not callable(close):
                raise SynthesisArtifactUnavailableError
            try:
                stored = read(record.size_bytes + 1)
            finally:
                close()
            if not isinstance(stored, bytes) or not hmac.compare_digest(
                stored, request.canonical_bytes
            ):
                raise SynthesisArtifactConflictError

        await self._dependency(asyncio.to_thread(read_sync))

    async def write(self, request: SynthesisArtifactWrite) -> StoredSynthesisArtifact:
        request = _validate_write(request)
        now = self._now()
        key = artifact_key(request.analysis_id, request.artifact_id, request.kind)
        tenant = await self._dependency(
            self._bucket_resolver.active_for_team(request.team_id)
        )
        if (
            not isinstance(tenant, TenantBucket)
            or tenant.team_id != request.team_id
            or tenant.resource_version != request.tenant_resource_version
            or not _valid_bucket(tenant.bucket)
        ):
            raise SynthesisArtifactUnavailableError

        record = await self._dependency(
            self._repository.reserve(
                tenant=tenant,
                analysis_id=request.analysis_id,
                artifact_id=request.artifact_id,
                kind=request.kind,
                request_hash=_request_hash(request.canonical_bytes),
                size_bytes=len(request.canonical_bytes),
                sha256_b64=request.sha256_b64,
                now=now,
            )
        )
        if not self._matches(record, request, key=key, now=now):
            raise SynthesisArtifactConflictError

        await self._fence(tenant)
        if record.state == "finalized":
            await self._read_exact(tenant, record, request)
            await self._fence(tenant)
            return self._public(record, request)

        await self._fence(tenant)
        try:
            receipt = await asyncio.to_thread(
                self._client.put_object,
                Bucket=tenant.bucket,
                Key=key,
                Body=request.canonical_bytes,
                ContentType=_JSON_MIME,
                ChecksumSHA256=request.sha256_b64,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise SynthesisArtifactUnavailableError from None
        if not isinstance(receipt, Mapping):
            raise SynthesisArtifactUnavailableError
        storage_version_id = _safe_version_id(receipt.get("VersionId"))
        receipt_checksum = receipt.get("ChecksumSHA256")
        if storage_version_id is None or not isinstance(receipt_checksum, str):
            raise SynthesisArtifactUnavailableError
        if not hmac.compare_digest(receipt_checksum, request.sha256_b64):
            raise SynthesisArtifactConflictError
        try:
            head = await asyncio.to_thread(
                self._client.head_object,
                Bucket=tenant.bucket,
                Key=key,
                VersionId=storage_version_id,
                ChecksumMode="ENABLED",
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise SynthesisArtifactUnavailableError from None
        self._verify_metadata(head, version_id=storage_version_id, request=request)
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
        if (
            not self._matches(finalized, request, key=key, now=now)
            or finalized.state != "finalized"
        ):
            raise SynthesisArtifactConflictError
        # A concurrent identical writer may have won with another immutable VersionId.
        # Always verify the exact version selected by the tenant record.
        await self._read_exact(tenant, finalized, request)
        await self._fence(tenant)
        return self._public(finalized, request)

    @staticmethod
    def _public(
        record: SynthesisArtifactRecord,
        request: SynthesisArtifactWrite,
    ) -> StoredSynthesisArtifact:
        return StoredSynthesisArtifact(
            artifact_id=record.artifact_id,
            analysis_id=record.analysis_id,
            kind=request.kind,
            size_bytes=record.size_bytes,
            sha256_b64=record.sha256_b64,
        )


__all__ = [
    "ArtifactKind",
    "S3SynthesisArtifactStore",
    "SQLAlchemySynthesisArtifactRepository",
    "StoredSynthesisArtifact",
    "SynthesisArtifactConflictError",
    "SynthesisArtifactError",
    "SynthesisArtifactRecord",
    "SynthesisArtifactRepository",
    "SynthesisArtifactUnavailableError",
    "SynthesisArtifactWrite",
    "artifact_key",
    "projection_artifact_id",
    "synthesis_artifact_id",
]
