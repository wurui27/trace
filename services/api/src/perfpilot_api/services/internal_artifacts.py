"""Persist server-owned JSON as immutable tenant Artifact versions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.services.uploads import BucketResolver, TenantBucket


_MEMORY_MANIFEST_NAMESPACE = UUID("3fce5d93-30fd-5ac5-9f62-c1a89f78cd83")
_INTERNAL_ARTIFACT_RETENTION = timedelta(days=30)
_JSON_MIME = "application/json"


class InternalArtifactError(RuntimeError):
    """A stable failure that never carries tenant storage coordinates."""


class InternalArtifactConflictError(InternalArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("internal artifact idempotency conflict")


class InternalArtifactUnavailableError(InternalArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("internal artifact service is unavailable")


def manifest_artifact_id(capture_id: UUID) -> UUID:
    return uuid5(_MEMORY_MANIFEST_NAMESPACE, str(capture_id))


class InternalArtifactSink(Protocol):
    async def write_json(
        self,
        *,
        team_id: UUID,
        expected_tenant_resource_version: int,
        analysis_id: UUID,
        artifact_id: UUID,
        artifact_kind: Literal["memory_capture_manifest"],
        payload: bytes,
    ) -> UUID: ...


@dataclass(frozen=True, slots=True)
class InternalArtifactRecord:
    artifact_id: UUID
    analysis_id: UUID
    artifact_kind: str
    mime_type: str
    size_bytes: int
    sha256_b64: str = field(repr=False)
    object_key: str = field(repr=False)
    state: str
    expires_at: datetime
    version: int
    version_id: str | None = field(repr=False)


class InternalArtifactRepository(Protocol):
    async def reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        artifact_kind: Literal["memory_capture_manifest"],
        mime_type: Literal["application/json"],
        size_bytes: int,
        sha256_b64: str,
        request_hash: str,
        object_key: str,
        now: datetime,
        expires_at: datetime,
    ) -> InternalArtifactRecord: ...

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
    ) -> InternalArtifactRecord | None: ...


class SQLAlchemyInternalArtifactRepository:
    """Reserve and finalize internal Artifacts only in the routed tenant database."""

    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    @asynccontextmanager
    async def _session(self, tenant: TenantBucket) -> AsyncIterator[AsyncSession]:
        async with self._tenant_router.session(tenant.team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            if (
                type(tenant.resource_version) is not int
                or type(routed_version) is not int
                or routed_version != tenant.resource_version
            ):
                raise InternalArtifactUnavailableError
            yield session

    @staticmethod
    def _record(row: Artifact) -> InternalArtifactRecord:
        if row.analysis_id is None:
            raise InternalArtifactUnavailableError
        return InternalArtifactRecord(
            artifact_id=row.id,
            analysis_id=row.analysis_id,
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

    async def reserve(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        artifact_kind: Literal["memory_capture_manifest"],
        mime_type: Literal["application/json"],
        size_bytes: int,
        sha256_b64: str,
        request_hash: str,
        object_key: str,
        now: datetime,
        expires_at: datetime,
    ) -> InternalArtifactRecord:
        async with self._session(tenant) as session:
            analysis = await session.scalar(
                select(Analysis.id).where(
                    Analysis.id == analysis_id,
                    Analysis.analysis_mode.in_(("memory_upload", "device")),
                    Analysis.tombstoned_at.is_(None),
                    Analysis.state != "deleted",
                )
            )
            if analysis is None:
                raise InternalArtifactConflictError
            created_id = await session.scalar(
                postgresql_insert(Artifact)
                .values(
                    id=artifact_id,
                    analysis_id=analysis_id,
                    upload_id=artifact_id,
                    idempotency_key=f"internal:{artifact_kind}:{artifact_id}",
                    request_hash=request_hash,
                    artifact_kind=artifact_kind,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    sha256_b64=sha256_b64,
                    object_key=object_key,
                    version_id=None,
                    state="pending",
                    finalized_at=None,
                    expires_at=expires_at,
                    deleted_at=None,
                    version=1,
                )
                .on_conflict_do_nothing(index_elements=(Artifact.id,))
                .returning(Artifact.id)
            )
            row = await session.get(Artifact, created_id or artifact_id)
            if row is None or row.analysis_id != analysis_id:
                raise InternalArtifactConflictError
            return self._record(row)

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
    ) -> InternalArtifactRecord | None:
        async with self._session(tenant) as session:
            row = await session.scalar(
                update(Artifact)
                .where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                    Artifact.state == "pending",
                    Artifact.version == expected_version,
                    Artifact.deleted_at.is_(None),
                )
                .values(
                    state="finalized",
                    version_id=storage_version_id,
                    finalized_at=now,
                    expires_at=expires_at,
                    updated_at=now,
                    version=Artifact.version + 1,
                )
                .returning(Artifact)
            )
            return None if row is None else self._record(row)


def _sha256_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _nonempty_version(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value == "null"
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


class S3InternalArtifactSink:
    """Write canonical manifest bytes and pin the verified immutable S3 version."""

    def __init__(
        self,
        *,
        repository: InternalArtifactRepository,
        bucket_resolver: BucketResolver,
        client: Any,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._bucket_resolver = bucket_resolver
        self._client = client
        self._clock = clock

    @staticmethod
    def _validate_manifest_identity(
        *,
        payload: bytes,
        analysis_id: UUID,
        artifact_id: UUID,
    ) -> None:
        try:
            manifest = MemoryCaptureManifest.model_validate_json(payload)
        except Exception:
            raise InternalArtifactConflictError from None
        if (
            manifest.analysis_id != analysis_id
            or manifest_artifact_id(manifest.capture_id) != artifact_id
            or manifest.canonical_bytes() != payload
        ):
            raise InternalArtifactConflictError

    @staticmethod
    def _matches_record(
        record: InternalArtifactRecord,
        *,
        analysis_id: UUID,
        artifact_id: UUID,
        artifact_kind: str,
        object_key: str,
        payload: bytes,
        checksum: str,
    ) -> bool:
        return (
            record.artifact_id == artifact_id
            and record.analysis_id == analysis_id
            and record.artifact_kind == artifact_kind
            and record.mime_type == _JSON_MIME
            and record.size_bytes == len(payload)
            and hmac.compare_digest(record.sha256_b64, checksum)
            and hmac.compare_digest(record.object_key, object_key)
            and record.state in ("pending", "finalized")
        )

    @staticmethod
    def _verified_metadata(
        response: object,
        *,
        version_id: str,
        checksum: str,
        size_bytes: int,
    ) -> Mapping[str, object] | None:
        metadata = _mapping(response)
        if metadata is None:
            return None
        returned_version = _nonempty_version(metadata.get("VersionId"))
        returned_checksum = metadata.get("ChecksumSHA256")
        content_type = metadata.get("ContentType")
        content_length = metadata.get("ContentLength")
        if (
            returned_version != version_id
            or not isinstance(returned_checksum, str)
            or not hmac.compare_digest(returned_checksum, checksum)
            or content_type != _JSON_MIME
            or type(content_length) is not int
            or content_length != size_bytes
            or metadata.get("DeleteMarker", False) is not False
        ):
            return None
        return metadata

    async def _read_existing(
        self,
        *,
        bucket: str,
        record: InternalArtifactRecord,
        payload: bytes,
    ) -> None:
        if record.version_id is None:
            raise InternalArtifactUnavailableError
        read_failed = False
        stored_payload: object = None
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=bucket,
                Key=record.object_key,
                VersionId=record.version_id,
                ChecksumMode="ENABLED",
            )
            metadata = self._verified_metadata(
                response,
                version_id=record.version_id,
                checksum=record.sha256_b64,
                size_bytes=record.size_bytes,
            )
            if metadata is None:
                raise InternalArtifactUnavailableError
            body = metadata.get("Body")
            if body is None or not callable(getattr(body, "read", None)):
                raise InternalArtifactUnavailableError
            stored_payload = await asyncio.to_thread(body.read)
        except InternalArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            read_failed = True
        if read_failed:
            raise InternalArtifactUnavailableError
        if not isinstance(stored_payload, bytes) or not hmac.compare_digest(
            stored_payload, payload
        ):
            raise InternalArtifactConflictError

    async def write_json(
        self,
        *,
        team_id: UUID,
        expected_tenant_resource_version: int,
        analysis_id: UUID,
        artifact_id: UUID,
        artifact_kind: Literal["memory_capture_manifest"],
        payload: bytes,
    ) -> UUID:
        if type(payload) is not bytes or artifact_kind != "memory_capture_manifest":
            raise InternalArtifactConflictError
        self._validate_manifest_identity(
            payload=payload,
            analysis_id=analysis_id,
            artifact_id=artifact_id,
        )
        checksum = _sha256_b64(payload)
        request_hash = hashlib.sha256(payload).hexdigest()
        object_key = f"raw/analyses/{analysis_id}/internal/{artifact_kind}/{artifact_id}"
        now = self._clock()
        expires_at = now + _INTERNAL_ARTIFACT_RETENTION
        if (
            type(expected_tenant_resource_version) is not int
            or expected_tenant_resource_version < 1
        ):
            raise InternalArtifactUnavailableError
        dependency_failed = False
        tenant: TenantBucket | None = None
        record: InternalArtifactRecord | None = None
        try:
            resolved_tenant = await self._bucket_resolver.active_for_team(team_id)
            if (
                not isinstance(resolved_tenant, TenantBucket)
                or resolved_tenant.team_id != team_id
                or type(resolved_tenant.resource_version) is not int
                or resolved_tenant.resource_version != expected_tenant_resource_version
                or not isinstance(resolved_tenant.bucket, str)
                or not resolved_tenant.bucket
            ):
                raise InternalArtifactUnavailableError
            tenant = resolved_tenant
            record = await self._repository.reserve(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                artifact_kind=artifact_kind,
                mime_type=_JSON_MIME,
                size_bytes=len(payload),
                sha256_b64=checksum,
                request_hash=request_hash,
                object_key=object_key,
                now=now,
                expires_at=expires_at,
            )
        except InternalArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            dependency_failed = True
        if dependency_failed or tenant is None or record is None:
            raise InternalArtifactUnavailableError
        if not self._matches_record(
            record,
            analysis_id=analysis_id,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            object_key=object_key,
            payload=payload,
            checksum=checksum,
        ):
            raise InternalArtifactConflictError

        bucket = tenant.bucket
        if record.state == "finalized":
            await self._read_existing(bucket=bucket, record=record, payload=payload)
            return record.artifact_id

        storage_failed = False
        storage_version_id: str | None = None
        try:
            receipt = await asyncio.to_thread(
                self._client.put_object,
                Bucket=bucket,
                Key=object_key,
                Body=payload,
                ContentType=_JSON_MIME,
                ChecksumSHA256=checksum,
            )
            receipt_mapping = _mapping(receipt)
            storage_version_id = (
                None
                if receipt_mapping is None
                else _nonempty_version(receipt_mapping.get("VersionId"))
            )
            receipt_checksum = (
                None if receipt_mapping is None else receipt_mapping.get("ChecksumSHA256")
            )
            if (
                storage_version_id is None
                or not isinstance(receipt_checksum, str)
                or not hmac.compare_digest(receipt_checksum, checksum)
            ):
                raise InternalArtifactUnavailableError
            head = await asyncio.to_thread(
                self._client.head_object,
                Bucket=bucket,
                Key=object_key,
                VersionId=storage_version_id,
                ChecksumMode="ENABLED",
            )
            if (
                self._verified_metadata(
                    head,
                    version_id=storage_version_id,
                    checksum=checksum,
                    size_bytes=len(payload),
                )
                is None
            ):
                raise InternalArtifactUnavailableError
        except InternalArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            storage_failed = True
        if storage_failed or storage_version_id is None:
            raise InternalArtifactUnavailableError

        finalize_failed = False
        finalized: InternalArtifactRecord | None = None
        try:
            finalized = await self._repository.finalize(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                expected_version=record.version,
                storage_version_id=storage_version_id,
                now=now,
                expires_at=expires_at,
            )
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            finalize_failed = True
        if finalize_failed:
            raise InternalArtifactUnavailableError
        if finalized is None:
            replay_failed = False
            try:
                finalized = await self._repository.reserve(
                    tenant=tenant,
                    analysis_id=analysis_id,
                    artifact_id=artifact_id,
                    artifact_kind=artifact_kind,
                    mime_type=_JSON_MIME,
                    size_bytes=len(payload),
                    sha256_b64=checksum,
                    request_hash=request_hash,
                    object_key=object_key,
                    now=now,
                    expires_at=expires_at,
                )
            except BaseException as error:
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                replay_failed = True
            if replay_failed:
                raise InternalArtifactUnavailableError
        if finalized.state != "finalized" or not self._matches_record(
            finalized,
            analysis_id=analysis_id,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            object_key=object_key,
            payload=payload,
            checksum=checksum,
        ):
            raise InternalArtifactUnavailableError
        if finalized.state == "finalized" and finalized.version_id != storage_version_id:
            await self._read_existing(bucket=bucket, record=finalized, payload=payload)
        return finalized.artifact_id


__all__ = [
    "InternalArtifactConflictError",
    "InternalArtifactError",
    "InternalArtifactRecord",
    "InternalArtifactRepository",
    "InternalArtifactSink",
    "InternalArtifactUnavailableError",
    "S3InternalArtifactSink",
    "SQLAlchemyInternalArtifactRepository",
    "manifest_artifact_id",
]
