from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import TenantResource
from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.storage.base import (
    ArtifactMetadataError,
    ArtifactNotFoundError,
    ArtifactStore,
    ObjectLocation,
    StoredObjectMetadata,
)

_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024
_UPLOADABLE_KINDS = frozenset(
    {
        "apk",
        "capture_manifest",
        "log",
        "mapping",
        "memory_evidence",
        "native_symbols",
        "screenshot",
        "source_archive",
        "trace",
    }
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
_SLOT_LIFETIME = timedelta(minutes=15)
_RETENTION_LIFETIME = timedelta(days=30)

UploadState = Literal["pending", "finalized", "expired", "deleted"]
_T = TypeVar("_T")


class UploadError(RuntimeError):
    """A stable upload failure that carries no storage identifiers or signed URLs."""


class UploadInvalidRequestError(UploadError):
    pass


class UploadNotFoundError(UploadError):
    pass


class UploadIdempotencyConflictError(UploadError):
    pass


class UploadMismatchError(UploadError):
    pass


class UploadExpiredError(UploadError):
    pass


class UploadUnavailableError(UploadError):
    pass


@dataclass(frozen=True, slots=True)
class TenantBucket:
    team_id: UUID
    bucket: str = field(repr=False)
    resource_version: int


@dataclass(frozen=True, slots=True)
class UploadDescriptor:
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str


@dataclass(frozen=True, slots=True)
class StoredUpload:
    artifact_id: UUID
    analysis_id: UUID
    upload_id: UUID
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str
    object_key: str = field(repr=False)
    state: UploadState
    expires_at: datetime
    version: int
    version_id: str | None = field(repr=False)
    finalized_at: datetime | None


@dataclass(frozen=True, slots=True)
class UploadSlot:
    artifact_id: UUID
    upload_id: UUID
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str
    state: Literal["pending", "finalized"]
    expires_at: datetime
    finalized_at: datetime | None
    required_headers: dict[str, str]
    put_url: str | None = field(repr=False)
    object_key: str = field(repr=False)
    version_id: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class DownloadAuthorization:
    artifact_id: UUID
    tenant_resource_version: int
    artifact_version: int
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str
    url: str = field(repr=False)
    expires_at: datetime


class BucketResolver(Protocol):
    async def active_for_team(self, team_id: UUID) -> TenantBucket: ...


class UploadRepository(Protocol):
    async def reserve_slot(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        idempotency_key: str,
        request_hash: str,
        descriptor: UploadDescriptor,
        artifact_id: UUID,
        upload_id: UUID,
        object_key: str,
        now: datetime,
        expires_at: datetime,
    ) -> StoredUpload: ...

    async def load_upload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        upload_id: UUID,
    ) -> StoredUpload: ...

    async def finalize_upload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        upload_id: UUID,
        expected_version: int,
        storage_version_id: str,
        finalized_at: datetime,
        expires_at: datetime,
    ) -> StoredUpload | None: ...

    async def load_download(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> StoredUpload: ...


class SQLAlchemyTenantBucketResolver:
    """Resolve one team's bucket only from the authoritative control mapping."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        failed = False
        try:
            async with self._session_factory() as session:
                resource = await session.scalar(
                    select(TenantResource).where(
                        TenantResource.team_id == team_id,
                        TenantResource.state.in_(("active", "migrating")),
                    )
                )
        except Exception:
            failed = True
        if failed:
            raise UploadUnavailableError("upload service is unavailable")
        if (
            resource is None
            or resource.write_paused
            or resource.bucket_name is None
            or not resource.bucket_name
        ):
            raise UploadUnavailableError("upload service is unavailable")
        return TenantBucket(
            team_id=resource.team_id,
            bucket=resource.bucket_name,
            resource_version=resource.resource_version,
        )


class SQLAlchemyUploadRepository:
    """Persist analysis-owned upload slots inside the routed tenant database."""

    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    @asynccontextmanager
    async def _session(self, tenant: TenantBucket) -> AsyncIterator[AsyncSession]:
        async with self._tenant_router.session(tenant.team_id) as session:
            routed_version = session.info.get("tenant_resource_version")
            if type(routed_version) is not int or routed_version != tenant.resource_version:
                raise UploadUnavailableError("upload service is unavailable")
            yield session

    @staticmethod
    def _stored(row: Artifact) -> StoredUpload:
        if row.analysis_id is None:
            raise UploadUnavailableError("upload service is unavailable")
        return StoredUpload(
            artifact_id=row.id,
            analysis_id=row.analysis_id,
            upload_id=row.upload_id,
            artifact_kind=row.artifact_kind,
            mime=row.mime_type,
            size=row.size_bytes,
            sha256_b64=row.sha256_b64,
            object_key=row.object_key,
            state=row.state,  # type: ignore[arg-type]
            expires_at=row.expires_at,
            version=row.version,
            version_id=row.version_id,
            finalized_at=row.finalized_at,
        )

    @staticmethod
    async def _require_analysis(
        session: AsyncSession,
        analysis_id: UUID,
        *,
        uploadable: bool,
        lock: bool = False,
    ) -> Analysis:
        statement = select(Analysis).where(
            Analysis.id == analysis_id,
            Analysis.tombstoned_at.is_(None),
            Analysis.state != "deleted",
        )
        if uploadable:
            statement = statement.where(Analysis.state.in_(("creating", "created", "uploading")))
        if lock:
            statement = statement.with_for_update()
        analysis = await session.scalar(statement)
        if analysis is None:
            raise UploadNotFoundError("artifact was not found")
        return analysis

    @staticmethod
    def _authorize_trace_input(
        analysis: Analysis,
        *,
        idempotency_key: str,
        descriptor: UploadDescriptor,
        now: datetime,
    ) -> None:
        manifest = analysis.input_manifest
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= len(_TRACE_INPUT_ORDER):
            raise UploadUnavailableError("upload service is unavailable")
        by_kind: dict[str, dict[str, object]] = {}
        ordered_kinds: list[str] = []
        for item in manifest:
            if not isinstance(item, dict) or set(item) != {
                "kind",
                "mime",
                "size",
                "sha256_b64",
            }:
                raise UploadUnavailableError("upload service is unavailable")
            kind = item.get("kind")
            mime = item.get("mime")
            size = item.get("size")
            checksum = item.get("sha256_b64")
            if (
                not isinstance(kind, str)
                or kind not in _TRACE_INPUT_KINDS
                or kind in by_kind
                or not isinstance(mime, str)
                or _MIME_TYPE.fullmatch(mime) is None
                or type(size) is not int
                or not 1 <= size <= _MAX_UPLOAD_BYTES
                or not isinstance(checksum, str)
            ):
                raise UploadUnavailableError("upload service is unavailable")
            try:
                canonical_checksum = _canonical_checksum(checksum)
            except UploadInvalidRequestError:
                raise UploadUnavailableError("upload service is unavailable") from None
            by_kind[kind] = {
                "kind": kind,
                "mime": mime,
                "size": size,
                "sha256_b64": canonical_checksum,
            }
            ordered_kinds.append(kind)
        expected_order = [kind for kind in _TRACE_INPUT_ORDER if kind in by_kind]
        if "trace" not in by_kind or ordered_kinds != expected_order:
            raise UploadUnavailableError("upload service is unavailable")

        expected = by_kind.get(descriptor.artifact_kind)
        actual = {
            "kind": descriptor.artifact_kind,
            "mime": descriptor.mime,
            "size": descriptor.size,
            "sha256_b64": descriptor.sha256_b64,
        }
        if idempotency_key != f"input-{descriptor.artifact_kind}" or expected != actual:
            raise UploadNotFoundError("artifact was not found")
        if analysis.state == "created":
            analysis.state = "uploading"
            analysis.version += 1
            analysis.updated_at = now
        elif analysis.state != "uploading":
            raise UploadNotFoundError("artifact was not found")

    async def reserve_slot(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        idempotency_key: str,
        request_hash: str,
        descriptor: UploadDescriptor,
        artifact_id: UUID,
        upload_id: UUID,
        object_key: str,
        now: datetime,
        expires_at: datetime,
    ) -> StoredUpload:
        async with self._session(tenant) as session:
            analysis = await self._require_analysis(
                session,
                analysis_id,
                uploadable=True,
                lock=True,
            )
            if analysis.analysis_mode == "trace_upload":
                self._authorize_trace_input(
                    analysis,
                    idempotency_key=idempotency_key,
                    descriptor=descriptor,
                    now=now,
                )
            statement = (
                postgresql_insert(Artifact)
                .values(
                    id=artifact_id,
                    analysis_id=analysis_id,
                    upload_id=upload_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    artifact_kind=descriptor.artifact_kind,
                    mime_type=descriptor.mime,
                    size_bytes=descriptor.size,
                    sha256_b64=descriptor.sha256_b64,
                    object_key=object_key,
                    version_id=None,
                    state="pending",
                    finalized_at=None,
                    expires_at=expires_at,
                    deleted_at=None,
                    version=1,
                )
                .on_conflict_do_nothing(
                    index_elements=(Artifact.analysis_id, Artifact.idempotency_key),
                    index_where=(
                        Artifact.analysis_id.is_not(None) & Artifact.idempotency_key.is_not(None)
                    ),
                )
                .returning(Artifact.id)
            )
            created_id = await session.scalar(statement)
            if created_id is not None:
                created = await session.get(Artifact, created_id)
                if created is None:
                    raise UploadUnavailableError("upload service is unavailable")
                return self._stored(created)

            existing = await session.scalar(
                select(Artifact)
                .where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if existing is None:
                raise UploadUnavailableError("upload service is unavailable")
            if existing.request_hash is None or not hmac.compare_digest(
                existing.request_hash,
                request_hash,
            ):
                raise UploadIdempotencyConflictError(
                    "idempotency key was reused with another upload request"
                )
            if existing.state == "finalized":
                return self._stored(existing)
            if existing.state != "pending":
                raise UploadExpiredError("upload authorization has expired")
            if existing.expires_at > now:
                return self._stored(existing)

            existing.upload_id = upload_id
            existing.object_key = object_key
            existing.expires_at = expires_at
            existing.version_id = None
            existing.finalized_at = None
            existing.deleted_at = None
            existing.version += 1
            existing.updated_at = now
            await session.flush()
            return self._stored(existing)

    async def load_upload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        upload_id: UUID,
    ) -> StoredUpload:
        async with self._session(tenant) as session:
            await self._require_analysis(session, analysis_id, uploadable=False)
            row = await session.scalar(
                select(Artifact).where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.upload_id == upload_id,
                    Artifact.deleted_at.is_(None),
                )
            )
            if row is None:
                raise UploadNotFoundError("artifact was not found")
            return self._stored(row)

    async def finalize_upload(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        upload_id: UUID,
        expected_version: int,
        storage_version_id: str,
        finalized_at: datetime,
        expires_at: datetime,
    ) -> StoredUpload | None:
        async with self._session(tenant) as session:
            statement = (
                update(Artifact)
                .where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.upload_id == upload_id,
                    Artifact.state == "pending",
                    Artifact.version == expected_version,
                    Artifact.expires_at > finalized_at,
                    Artifact.deleted_at.is_(None),
                )
                .values(
                    state="finalized",
                    version_id=storage_version_id,
                    finalized_at=finalized_at,
                    expires_at=expires_at,
                    updated_at=finalized_at,
                    version=Artifact.version + 1,
                )
                .returning(Artifact)
            )
            row = await session.scalar(statement)
            return None if row is None else self._stored(row)

    async def load_download(
        self,
        *,
        tenant: TenantBucket,
        analysis_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> StoredUpload:
        async with self._session(tenant) as session:
            row = await session.scalar(
                select(Artifact)
                .join(Analysis, Analysis.id == Artifact.analysis_id)
                .where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                    Analysis.tombstoned_at.is_(None),
                    Analysis.state != "deleted",
                    Artifact.deleted_at.is_(None),
                )
            )
            if row is None:
                raise UploadNotFoundError("artifact was not found")
            if row.state == "expired" or row.expires_at <= now:
                raise UploadExpiredError("artifact has expired")
            if row.state != "finalized" or row.version_id is None:
                raise UploadNotFoundError("artifact was not found")
            return self._stored(row)


def _canonical_checksum(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise UploadInvalidRequestError("upload request is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise UploadInvalidRequestError("upload request is invalid")
    return value


def _validated_descriptor(
    *,
    artifact_kind: str,
    mime: str,
    size: int,
    sha256_b64: str,
) -> UploadDescriptor:
    if (
        artifact_kind not in _UPLOADABLE_KINDS
        or _MIME_TYPE.fullmatch(mime) is None
        or type(size) is not int
        or not 1 <= size <= _MAX_UPLOAD_BYTES
    ):
        raise UploadInvalidRequestError("upload request is invalid")
    return UploadDescriptor(
        artifact_kind=artifact_kind,
        mime=mime,
        size=size,
        sha256_b64=_canonical_checksum(sha256_b64),
    )


def canonical_upload_request_hash(
    *,
    analysis_id: UUID,
    descriptor: UploadDescriptor,
) -> str:
    payload = json.dumps(
        {
            "analysis_id": str(analysis_id),
            "artifact_kind": descriptor.artifact_kind,
            "mime": descriptor.mime,
            "sha256_b64": descriptor.sha256_b64,
            "size": descriptor.size,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


async def _call_available(operation: Callable[[], Awaitable[_T]]) -> _T:
    failed = False
    try:
        result = await operation()
    except UploadError:
        raise
    except Exception:
        failed = True
    if failed:
        raise UploadUnavailableError("upload service is unavailable")
    return result


async def _head_for_finalize(
    artifact_store: ArtifactStore,
    location: ObjectLocation,
) -> StoredObjectMetadata:
    mismatch = False
    unavailable = False
    try:
        metadata = await artifact_store.head(location=location)
    except (ArtifactMetadataError, ArtifactNotFoundError):
        mismatch = True
    except Exception:
        unavailable = True
    if mismatch:
        raise UploadMismatchError("uploaded object metadata does not match")
    if unavailable:
        raise UploadUnavailableError("upload service is unavailable")
    return metadata


class UploadService:
    def __init__(
        self,
        *,
        repository: UploadRepository,
        artifact_store: ArtifactStore,
        bucket_resolver: BucketResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._bucket_resolver = bucket_resolver
        self._clock = clock
        self._uuid_source = uuid_source

    async def create_slot(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        idempotency_key: str,
        artifact_kind: str,
        mime: str,
        size: int,
        sha256_b64: str,
    ) -> UploadSlot:
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise UploadInvalidRequestError("upload request is invalid")
        descriptor = _validated_descriptor(
            artifact_kind=artifact_kind,
            mime=mime,
            size=size,
            sha256_b64=sha256_b64,
        )
        now = self._clock()
        tenant = await _call_available(lambda: self._bucket_resolver.active_for_team(team_id))
        artifact_id = self._uuid_source()
        upload_id = self._uuid_source()
        object_key = f"raw/analyses/{analysis_id}/inputs/{descriptor.artifact_kind}/{upload_id}"
        stored = await _call_available(
            lambda: self._repository.reserve_slot(
                tenant=tenant,
                analysis_id=analysis_id,
                idempotency_key=idempotency_key,
                request_hash=canonical_upload_request_hash(
                    analysis_id=analysis_id,
                    descriptor=descriptor,
                ),
                descriptor=descriptor,
                artifact_id=artifact_id,
                upload_id=upload_id,
                object_key=object_key,
                now=now,
                expires_at=now + _SLOT_LIFETIME,
            )
        )
        if stored.state == "finalized":
            return self._slot(stored, put_url=None, required_headers={})
        remaining_seconds = int((stored.expires_at - now).total_seconds())
        if stored.state != "pending" or remaining_seconds < 1:
            raise UploadExpiredError("upload authorization has expired")
        authorization = await _call_available(
            lambda: self._artifact_store.authorize_put(
                location=ObjectLocation(bucket=tenant.bucket, key=stored.object_key),
                content_type=stored.mime,
                checksum_sha256_b64=stored.sha256_b64,
                expires_in_seconds=min(900, remaining_seconds),
            )
        )
        return self._slot(
            stored,
            put_url=authorization.url,
            required_headers=dict(authorization.required_headers),
        )

    async def finalize(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
        caller_sha256_b64: str,
        caller_size: int,
    ) -> UploadSlot:
        now = self._clock()
        tenant = await _call_available(lambda: self._bucket_resolver.active_for_team(team_id))
        stored = await _call_available(
            lambda: self._repository.load_upload(
                tenant=tenant,
                analysis_id=analysis_id,
                upload_id=upload_id,
            )
        )
        if caller_size != stored.size or caller_sha256_b64 != stored.sha256_b64:
            raise UploadMismatchError("uploaded object metadata does not match")
        if stored.state == "finalized" and stored.version_id is not None:
            return self._slot(stored, put_url=None, required_headers={})
        if stored.state != "pending" or stored.expires_at <= now:
            raise UploadExpiredError("upload authorization has expired")
        metadata = await _head_for_finalize(
            self._artifact_store,
            ObjectLocation(bucket=tenant.bucket, key=stored.object_key),
        )
        storage_version_id = metadata.location.version_id
        if (
            metadata.location.bucket != tenant.bucket
            or metadata.location.key != stored.object_key
            or storage_version_id is None
            or metadata.size_bytes != stored.size
            or metadata.checksum_sha256_b64 != stored.sha256_b64
            or metadata.content_type != stored.mime
        ):
            raise UploadMismatchError("uploaded object metadata does not match")
        finalized = await _call_available(
            lambda: self._repository.finalize_upload(
                tenant=tenant,
                analysis_id=analysis_id,
                upload_id=upload_id,
                expected_version=stored.version,
                storage_version_id=storage_version_id,
                finalized_at=now,
                expires_at=now + _RETENTION_LIFETIME,
            )
        )
        if finalized is None:
            winning = await _call_available(
                lambda: self._repository.load_upload(
                    tenant=tenant,
                    analysis_id=analysis_id,
                    upload_id=upload_id,
                )
            )
            if (
                winning.state != "finalized"
                or winning.version_id is None
                or winning.size != caller_size
                or winning.sha256_b64 != caller_sha256_b64
            ):
                raise UploadUnavailableError("upload service is unavailable")
            finalized = winning
        return self._slot(finalized, put_url=None, required_headers={})

    async def download(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_tenant_resource_version: int | None = None,
        expected_artifact_version: int | None = None,
    ) -> DownloadAuthorization:
        now = self._clock()
        tenant = await _call_available(lambda: self._bucket_resolver.active_for_team(team_id))
        if expected_tenant_resource_version is not None and (
            type(expected_tenant_resource_version) is not int
            or tenant.resource_version != expected_tenant_resource_version
        ):
            raise UploadUnavailableError("upload service is unavailable")
        stored = await _call_available(
            lambda: self._repository.load_download(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
                now=now,
            )
        )
        if stored.state != "finalized" or stored.version_id is None:
            raise UploadNotFoundError("artifact was not found")
        if expected_artifact_version is not None and (
            type(expected_artifact_version) is not int
            or stored.version != expected_artifact_version
        ):
            raise UploadUnavailableError("upload service is unavailable")
        authorization = await _call_available(
            lambda: self._artifact_store.authorize_get(
                location=ObjectLocation(
                    bucket=tenant.bucket,
                    key=stored.object_key,
                    version_id=stored.version_id,
                ),
                expires_in_seconds=300,
            )
        )
        return DownloadAuthorization(
            artifact_id=stored.artifact_id,
            tenant_resource_version=tenant.resource_version,
            artifact_version=stored.version,
            artifact_kind=stored.artifact_kind,
            mime=stored.mime,
            size=stored.size,
            sha256_b64=stored.sha256_b64,
            url=authorization.url,
            expires_at=now + timedelta(seconds=authorization.expires_in_seconds),
        )

    @staticmethod
    def _slot(
        stored: StoredUpload,
        *,
        put_url: str | None,
        required_headers: dict[str, str],
    ) -> UploadSlot:
        state: Literal["pending", "finalized"] = (
            "finalized" if stored.state == "finalized" else "pending"
        )
        return UploadSlot(
            artifact_id=stored.artifact_id,
            upload_id=stored.upload_id,
            artifact_kind=stored.artifact_kind,
            mime=stored.mime,
            size=stored.size,
            sha256_b64=stored.sha256_b64,
            state=state,
            expires_at=stored.expires_at,
            finalized_at=stored.finalized_at,
            required_headers=required_headers,
            put_url=put_url,
            object_key=stored.object_key,
            version_id=stored.version_id,
        )


__all__ = [
    "BucketResolver",
    "DownloadAuthorization",
    "SQLAlchemyTenantBucketResolver",
    "SQLAlchemyUploadRepository",
    "StoredUpload",
    "TenantBucket",
    "UploadDescriptor",
    "UploadError",
    "UploadExpiredError",
    "UploadIdempotencyConflictError",
    "UploadInvalidRequestError",
    "UploadMismatchError",
    "UploadNotFoundError",
    "UploadRepository",
    "UploadService",
    "UploadSlot",
    "UploadUnavailableError",
    "canonical_upload_request_hash",
]
