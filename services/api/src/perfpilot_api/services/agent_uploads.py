from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.tenant.models import (
    Analysis,
    Artifact,
    ArtifactMultipartUpload,
    ScenarioResult,
)
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.services.agent_tasks import (
    AgentExecutionAccess,
    AgentTaskNotFound,
    StaleLeaseVersion,
    ValidatedAgentExecutionManifest,
)
from perfpilot_api.services.uploads import BucketResolver, TenantBucket
from perfpilot_api.storage.base import (
    ArtifactMetadataError,
    ArtifactNotFoundError,
    MultipartArtifactStore,
    MultipartPart,
    ObjectLocation,
)

_MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")
_ETAG = re.compile(r"[^\x00-\x1f\x7f]{1,1024}\Z")
_MAX_OUTPUT_BYTES = 512 * 1024 * 1024
_PART_SIZE_BYTES = 64 * 1024 * 1024
_MAX_PARTS = 10_000
_SLOT_LIFETIME = timedelta(minutes=15)
_RETENTION_LIFETIME = timedelta(days=30)

AgentUploadState = Literal["pending", "finalized", "aborted", "expired"]


class AgentUploadError(RuntimeError):
    """A stable Agent upload failure without object-store details."""


class AgentUploadInvalidRequest(AgentUploadError):
    pass


class AgentUploadNotFound(AgentUploadError):
    pass


class AgentUploadStaleLease(AgentUploadError):
    pass


class AgentUploadMismatch(AgentUploadError):
    pass


class AgentUploadExpired(AgentUploadError):
    pass


class AgentUploadUnavailable(AgentUploadError):
    pass


@dataclass(frozen=True, slots=True)
class AgentUploadDescriptor:
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredAgentInput:
    artifact_id: UUID
    analysis_id: UUID
    mime: str
    size: int
    sha256_b64: str = field(repr=False)
    object_key: str = field(repr=False)
    version_id: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AgentInputSlot:
    artifact_id: UUID
    mime: str
    size: int
    sha256_b64: str = field(repr=False)
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAgentUpload:
    artifact_id: UUID
    analysis_id: UUID
    execution_id: UUID
    upload_id: UUID
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str = field(repr=False)
    object_key: str = field(repr=False)
    storage_upload_id: str = field(repr=False)
    part_size_bytes: int
    part_count: int
    completed_parts: tuple[MultipartPart, ...]
    state: AgentUploadState
    expires_at: datetime
    version: int
    version_id: str | None = field(repr=False)
    finalized_at: datetime | None


@dataclass(frozen=True, slots=True)
class AgentUploadReservation:
    upload: StoredAgentUpload
    created: bool


@dataclass(frozen=True, slots=True)
class AgentUploadCompletionClaim:
    upload: StoredAgentUpload
    call_storage_complete: bool


@dataclass(frozen=True, slots=True)
class AgentUploadSlot:
    artifact_id: UUID
    upload_id: UUID
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str = field(repr=False)
    part_size_bytes: int
    part_count: int
    state: AgentUploadState
    expires_at: datetime
    finalized_at: datetime | None


@dataclass(frozen=True, slots=True)
class AgentUploadPartSlot:
    upload_id: UUID
    part_number: int
    url: str = field(repr=False)
    required_headers: dict[str, str] = field(repr=False)
    expires_at: datetime


class AgentExecutionAuthorizer(Protocol):
    async def authorize_execution(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess: ...


class AgentUploadRepository(Protocol):
    async def load_input(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        artifact_id: UUID,
    ) -> StoredAgentInput: ...

    async def reserve_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        descriptor: AgentUploadDescriptor,
        artifact_id: UUID,
        upload_id: UUID,
        object_key: str,
        storage_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        now: datetime,
        expires_at: datetime,
    ) -> AgentUploadReservation: ...

    async def load_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
    ) -> StoredAgentUpload: ...

    async def prepare_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        parts: tuple[MultipartPart, ...],
        now: datetime,
    ) -> AgentUploadCompletionClaim: ...

    async def finalize_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        storage_version_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> StoredAgentUpload: ...

    async def validate_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
    ) -> None: ...

    async def project_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> None: ...

    async def pending_execution_uploads(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
    ) -> tuple[StoredAgentUpload, ...]: ...

    async def mark_upload_aborted(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        now: datetime,
    ) -> None: ...

    async def project_cancellation(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        reason_code: str,
        now: datetime,
    ) -> None: ...


class InMemoryAgentUploadRepository:
    def __init__(self, *, inputs: Sequence[StoredAgentInput] = ()) -> None:
        self._uploads: dict[UUID, StoredAgentUpload] = {}
        self._inputs = {item.artifact_id: item for item in inputs}

    async def load_input(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        artifact_id: UUID,
    ) -> StoredAgentInput:
        del tenant
        stored = self._inputs.get(artifact_id)
        if stored is None or stored.analysis_id != access.analysis_id:
            raise AgentUploadNotFound("input artifact was not found")
        return stored

    async def reserve_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        descriptor: AgentUploadDescriptor,
        artifact_id: UUID,
        upload_id: UUID,
        object_key: str,
        storage_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        now: datetime,
        expires_at: datetime,
    ) -> AgentUploadReservation:
        del tenant, now
        existing = next(
            (
                item
                for item in self._uploads.values()
                if item.execution_id == access.execution_id
                and item.artifact_kind == descriptor.artifact_kind
            ),
            None,
        )
        if existing is not None:
            if not _same_descriptor(existing, descriptor):
                raise AgentUploadInvalidRequest("upload kind was reused with different metadata")
            return AgentUploadReservation(upload=existing, created=False)
        stored = StoredAgentUpload(
            artifact_id=artifact_id,
            analysis_id=access.analysis_id,
            execution_id=access.execution_id,
            upload_id=upload_id,
            artifact_kind=descriptor.artifact_kind,
            mime=descriptor.mime,
            size=descriptor.size,
            sha256_b64=descriptor.sha256_b64,
            object_key=object_key,
            storage_upload_id=storage_upload_id,
            part_size_bytes=part_size_bytes,
            part_count=part_count,
            completed_parts=(),
            state="pending",
            expires_at=expires_at,
            version=1,
            version_id=None,
            finalized_at=None,
        )
        self._uploads[upload_id] = stored
        return AgentUploadReservation(upload=stored, created=True)

    async def load_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
    ) -> StoredAgentUpload:
        del tenant
        stored = self._uploads.get(upload_id)
        if (
            stored is None
            or stored.analysis_id != access.analysis_id
            or stored.execution_id != access.execution_id
        ):
            raise AgentUploadNotFound("upload was not found")
        return stored

    async def prepare_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        parts: tuple[MultipartPart, ...],
        now: datetime,
    ) -> AgentUploadCompletionClaim:
        stored = await self.load_upload(tenant=tenant, access=access, upload_id=upload_id)
        if stored.state == "finalized":
            if stored.completed_parts != parts:
                raise AgentUploadInvalidRequest("multipart completion does not match")
            return AgentUploadCompletionClaim(upload=stored, call_storage_complete=False)
        if stored.state != "pending" or stored.expires_at <= now:
            raise AgentUploadExpired("upload has expired")
        if stored.completed_parts:
            if stored.completed_parts != parts:
                raise AgentUploadInvalidRequest("multipart completion does not match")
            return AgentUploadCompletionClaim(upload=stored, call_storage_complete=False)
        stored = replace(stored, completed_parts=parts, version=stored.version + 1)
        self._uploads[upload_id] = stored
        return AgentUploadCompletionClaim(upload=stored, call_storage_complete=True)

    async def finalize_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        storage_version_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> StoredAgentUpload:
        stored = await self.load_upload(tenant=tenant, access=access, upload_id=upload_id)
        if stored.state == "finalized":
            return stored
        if stored.state != "pending" or not stored.completed_parts:
            raise AgentUploadUnavailable("upload could not be finalized")
        stored = replace(
            stored,
            state="finalized",
            version=stored.version + 1,
            version_id=storage_version_id,
            finalized_at=now,
            expires_at=expires_at,
        )
        self._uploads[upload_id] = stored
        return stored

    async def validate_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
    ) -> None:
        del tenant
        stored = tuple(
            item
            for item in self._uploads.values()
            if item.execution_id == access.execution_id and item.state == "finalized"
        )
        _match_completion_artifacts(stored, manifest)

    async def project_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> None:
        del tenant, access, manifest, now

    async def pending_execution_uploads(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
    ) -> tuple[StoredAgentUpload, ...]:
        del tenant
        return tuple(
            item
            for item in self._uploads.values()
            if item.analysis_id == access.analysis_id
            and item.execution_id == access.execution_id
            and item.state == "pending"
        )

    async def mark_upload_aborted(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        now: datetime,
    ) -> None:
        del tenant, now
        stored = self._uploads.get(upload_id)
        if (
            stored is None
            or stored.analysis_id != access.analysis_id
            or stored.execution_id != access.execution_id
        ):
            raise AgentUploadNotFound("upload was not found")
        if stored.state == "pending":
            self._uploads[upload_id] = replace(
                stored,
                state="aborted",
                version=stored.version + 1,
            )

    async def project_cancellation(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        reason_code: str,
        now: datetime,
    ) -> None:
        del tenant, access, reason_code, now


class SQLAlchemyAgentUploadRepository:
    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    @asynccontextmanager
    async def _session(self, tenant: TenantBucket) -> AsyncIterator[AsyncSession]:
        async with self._tenant_router.session(tenant.team_id) as session:
            if session.info.get("tenant_resource_version") != tenant.resource_version:
                raise AgentUploadUnavailable("upload service is unavailable")
            yield session

    @staticmethod
    def _stored(artifact: Artifact, multipart: ArtifactMultipartUpload) -> StoredAgentUpload:
        if artifact.analysis_id is None:
            raise AgentUploadUnavailable("upload service is unavailable")
        completed_parts = _parse_stored_parts(multipart.completed_parts)
        state: AgentUploadState
        if artifact.state == "finalized" and multipart.state == "completed":
            state = "finalized"
        elif artifact.state == "pending" and multipart.state == "pending":
            state = "pending"
        elif multipart.state == "aborted":
            state = "aborted"
        elif multipart.state == "expired" or artifact.state == "expired":
            state = "expired"
        else:
            raise AgentUploadUnavailable("upload service is unavailable")
        return StoredAgentUpload(
            artifact_id=artifact.id,
            analysis_id=artifact.analysis_id,
            execution_id=multipart.execution_id,
            upload_id=multipart.id,
            artifact_kind=artifact.artifact_kind,
            mime=artifact.mime_type,
            size=artifact.size_bytes,
            sha256_b64=artifact.sha256_b64,
            object_key=artifact.object_key,
            storage_upload_id=multipart.storage_upload_id,
            part_size_bytes=multipart.part_size_bytes,
            part_count=multipart.part_count,
            completed_parts=completed_parts,
            state=state,
            expires_at=multipart.expires_at,
            version=multipart.version,
            version_id=artifact.version_id,
            finalized_at=artifact.finalized_at,
        )

    @staticmethod
    async def _load_rows(
        session: AsyncSession,
        *,
        access: AgentExecutionAccess,
        upload_id: UUID,
        lock: bool = False,
    ) -> tuple[Artifact, ArtifactMultipartUpload]:
        statement = (
            select(Artifact, ArtifactMultipartUpload)
            .join(ArtifactMultipartUpload, ArtifactMultipartUpload.artifact_id == Artifact.id)
            .where(
                ArtifactMultipartUpload.id == upload_id,
                ArtifactMultipartUpload.execution_id == access.execution_id,
                Artifact.analysis_id == access.analysis_id,
                Artifact.deleted_at.is_(None),
            )
        )
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise AgentUploadNotFound("upload was not found")
        return row

    async def reserve_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        descriptor: AgentUploadDescriptor,
        artifact_id: UUID,
        upload_id: UUID,
        object_key: str,
        storage_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        now: datetime,
        expires_at: datetime,
    ) -> AgentUploadReservation:
        idempotency_key = f"agent-output:{access.execution_id}:{descriptor.artifact_kind}"
        request_hash = _request_hash(access=access, descriptor=descriptor)
        async with self._session(tenant) as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(
                    Analysis.id == access.analysis_id,
                    Analysis.analysis_mode == "device",
                    Analysis.tombstoned_at.is_(None),
                    Analysis.state != "deleted",
                )
                .with_for_update()
            )
            if analysis is None:
                raise AgentUploadNotFound("analysis was not found")
            existing_row = (
                await session.execute(
                    select(Artifact, ArtifactMultipartUpload)
                    .join(
                        ArtifactMultipartUpload,
                        ArtifactMultipartUpload.artifact_id == Artifact.id,
                    )
                    .where(
                        Artifact.analysis_id == access.analysis_id,
                        Artifact.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if existing_row is not None:
                existing = self._stored(*existing_row)
                if (
                    existing.execution_id != access.execution_id
                    or existing_row[0].request_hash is None
                    or not hmac.compare_digest(existing_row[0].request_hash, request_hash)
                    or not _same_descriptor(existing, descriptor)
                ):
                    raise AgentUploadInvalidRequest(
                        "upload kind was reused with different metadata"
                    )
                return AgentUploadReservation(upload=existing, created=False)
            artifact = Artifact(
                id=artifact_id,
                application_version_id=None,
                analysis_id=access.analysis_id,
                scenario_result_id=None,
                sample_attempt_id=None,
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
            multipart = ArtifactMultipartUpload(
                id=upload_id,
                artifact_id=artifact_id,
                execution_id=access.execution_id,
                storage_upload_id=storage_upload_id,
                part_size_bytes=part_size_bytes,
                part_count=part_count,
                completed_parts=[],
                state="pending",
                expires_at=expires_at,
                completed_at=None,
                version=1,
            )
            session.add_all((artifact, multipart))
            await session.flush()
            return AgentUploadReservation(
                upload=self._stored(artifact, multipart),
                created=True,
            )

    async def load_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
    ) -> StoredAgentUpload:
        async with self._session(tenant) as session:
            rows = await self._load_rows(session, access=access, upload_id=upload_id)
            return self._stored(*rows)

    async def load_input(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        artifact_id: UUID,
    ) -> StoredAgentInput:
        async with self._session(tenant) as session:
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == access.analysis_id,
                    Artifact.artifact_kind == "apk",
                    Artifact.state == "finalized",
                    Artifact.version_id.is_not(None),
                    Artifact.deleted_at.is_(None),
                )
            )
        if artifact is None or artifact.analysis_id is None or artifact.version_id is None:
            raise AgentUploadNotFound("input artifact was not found")
        return StoredAgentInput(
            artifact_id=artifact.id,
            analysis_id=artifact.analysis_id,
            mime=artifact.mime_type,
            size=artifact.size_bytes,
            sha256_b64=artifact.sha256_b64,
            object_key=artifact.object_key,
            version_id=artifact.version_id,
            expires_at=artifact.expires_at,
        )

    async def prepare_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        parts: tuple[MultipartPart, ...],
        now: datetime,
    ) -> AgentUploadCompletionClaim:
        async with self._session(tenant) as session:
            artifact, multipart = await self._load_rows(
                session,
                access=access,
                upload_id=upload_id,
                lock=True,
            )
            stored = self._stored(artifact, multipart)
            if stored.state == "finalized":
                if stored.completed_parts != parts:
                    raise AgentUploadInvalidRequest("multipart completion does not match")
                return AgentUploadCompletionClaim(stored, False)
            if stored.state != "pending" or stored.expires_at <= now:
                raise AgentUploadExpired("upload has expired")
            if stored.completed_parts:
                if stored.completed_parts != parts:
                    raise AgentUploadInvalidRequest("multipart completion does not match")
                return AgentUploadCompletionClaim(stored, False)
            multipart.completed_parts = [
                {"part_number": part.part_number, "etag": part.etag} for part in parts
            ]
            multipart.version += 1
            multipart.updated_at = now
            await session.flush()
            return AgentUploadCompletionClaim(self._stored(artifact, multipart), True)

    async def finalize_upload(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        storage_version_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> StoredAgentUpload:
        async with self._session(tenant) as session:
            artifact, multipart = await self._load_rows(
                session,
                access=access,
                upload_id=upload_id,
                lock=True,
            )
            stored = self._stored(artifact, multipart)
            if stored.state == "finalized":
                return stored
            if stored.state != "pending" or not stored.completed_parts:
                raise AgentUploadUnavailable("upload could not be finalized")
            artifact.state = "finalized"
            artifact.version_id = storage_version_id
            artifact.finalized_at = now
            artifact.expires_at = expires_at
            artifact.version += 1
            artifact.updated_at = now
            multipart.state = "completed"
            multipart.completed_at = now
            multipart.expires_at = expires_at
            multipart.version += 1
            multipart.updated_at = now
            await session.flush()
            return self._stored(artifact, multipart)

    async def validate_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
    ) -> None:
        async with self._session(tenant) as session:
            rows = tuple(
                (
                    await session.execute(
                        select(Artifact, ArtifactMultipartUpload)
                        .join(
                            ArtifactMultipartUpload,
                            ArtifactMultipartUpload.artifact_id == Artifact.id,
                        )
                        .where(
                            Artifact.analysis_id == access.analysis_id,
                            ArtifactMultipartUpload.execution_id == access.execution_id,
                            Artifact.deleted_at.is_(None),
                        )
                    )
                ).all()
            )
        stored = tuple(self._stored(*row) for row in rows)
        _match_completion_artifacts(
            tuple(item for item in stored if item.state == "finalized"),
            manifest,
        )

    async def project_completion(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> None:
        async with self._session(tenant) as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(
                    Analysis.id == access.analysis_id,
                    Analysis.analysis_mode == "device",
                    Analysis.tombstoned_at.is_(None),
                )
                .with_for_update()
            )
            if analysis is None:
                raise AgentUploadNotFound("analysis was not found")
            scenario_rows = tuple(
                (
                    await session.scalars(
                        select(ScenarioResult)
                        .where(ScenarioResult.analysis_id == access.analysis_id)
                        .with_for_update()
                    )
                ).all()
            )
            by_type = {
                "startup" if row.scenario_type == "cold_start" else row.scenario_type: row
                for row in scenario_rows
            }
            if set(by_type) != {item.scenario_type for item in manifest.scenarios}:
                raise AgentUploadUnavailable("scenario projection is unavailable")
            if manifest.state == "failed":
                analysis.state = "failed"
                analysis.completed_at = now
                analysis.failure_code = manifest.diagnostic_code or "agent_execution_failed"
            else:
                analysis.state = "analyzing"
                analysis.completed_at = None
                analysis.failure_code = None
            analysis.started_at = analysis.started_at or manifest.started_at
            analysis.version += 1
            analysis.updated_at = now
            manifest_scenarios = {item.scenario_type: item for item in manifest.scenarios}
            for scenario_type, result in by_type.items():
                observed = manifest_scenarios[scenario_type]
                if manifest.state == "completed" and observed.state == "completed":
                    result.state = "analyzing"
                    result.failure_code = None
                    result.completed_at = None
                else:
                    result.state = "failed"
                    result.failure_code = (
                        observed.diagnostic_code
                        or manifest.diagnostic_code
                        or "agent_scenario_failed"
                    )
                    result.completed_at = now
                result.started_at = result.started_at or observed.started_at
                result.version += 1
                result.updated_at = now
            await session.flush()

    async def pending_execution_uploads(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
    ) -> tuple[StoredAgentUpload, ...]:
        async with self._session(tenant) as session:
            rows = tuple(
                (
                    await session.execute(
                        select(Artifact, ArtifactMultipartUpload)
                        .join(
                            ArtifactMultipartUpload,
                            ArtifactMultipartUpload.artifact_id == Artifact.id,
                        )
                        .where(
                            Artifact.analysis_id == access.analysis_id,
                            ArtifactMultipartUpload.execution_id == access.execution_id,
                            ArtifactMultipartUpload.state == "pending",
                            Artifact.deleted_at.is_(None),
                        )
                        .order_by(ArtifactMultipartUpload.created_at, ArtifactMultipartUpload.id)
                    )
                ).all()
            )
        return tuple(self._stored(*row) for row in rows)

    async def mark_upload_aborted(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        upload_id: UUID,
        now: datetime,
    ) -> None:
        async with self._session(tenant) as session:
            artifact, multipart = await self._load_rows(
                session,
                access=access,
                upload_id=upload_id,
                lock=True,
            )
            if multipart.state == "aborted":
                return
            if multipart.state != "pending":
                return
            multipart.state = "aborted"
            multipart.completed_parts = []
            multipart.version += 1
            multipart.updated_at = now
            artifact.state = "expired"
            artifact.version += 1
            artifact.updated_at = now
            await session.flush()

    async def project_cancellation(
        self,
        *,
        tenant: TenantBucket,
        access: AgentExecutionAccess,
        reason_code: str,
        now: datetime,
    ) -> None:
        if reason_code != "analysis_canceled":
            raise AgentUploadInvalidRequest("cancellation reason is invalid")
        async with self._session(tenant) as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(
                    Analysis.id == access.analysis_id,
                    Analysis.analysis_mode == "device",
                    Analysis.tombstoned_at.is_(None),
                )
                .with_for_update()
            )
            if analysis is None:
                raise AgentUploadNotFound("analysis was not found")
            if analysis.state not in ("completed", "partially_completed", "failed", "canceled"):
                analysis.state = "canceled"
                analysis.completed_at = now
                analysis.failure_code = None
                analysis.version += 1
                analysis.updated_at = now
            scenario_rows = tuple(
                (
                    await session.scalars(
                        select(ScenarioResult)
                        .where(ScenarioResult.analysis_id == access.analysis_id)
                        .with_for_update()
                    )
                ).all()
            )
            for result in scenario_rows:
                if result.state in ("completed", "failed", "canceled"):
                    continue
                result.state = "canceled"
                result.completed_at = now
                result.failure_code = None
                result.version += 1
                result.updated_at = now
            await session.flush()


class AgentUploadService:
    def __init__(
        self,
        *,
        repository: AgentUploadRepository,
        artifact_store: MultipartArtifactStore,
        bucket_resolver: BucketResolver,
        execution_authorizer: AgentExecutionAuthorizer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._bucket_resolver = bucket_resolver
        self._execution_authorizer = execution_authorizer
        self._clock = clock
        self._uuid_source = uuid_source

    async def authorize_input(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        artifact_id: UUID,
    ) -> AgentInputSlot:
        now = _aware(self._clock())
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        if artifact_id not in access.input_artifact_ids:
            raise AgentUploadNotFound("input artifact was not found")
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        stored = await _available(
            self._repository.load_input(
                tenant=tenant,
                access=access,
                artifact_id=artifact_id,
            )
        )
        if stored.expires_at <= now:
            raise AgentUploadExpired("input artifact has expired")
        authorization = await _available(
            self._artifact_store.authorize_get(
                location=ObjectLocation(
                    bucket=tenant.bucket,
                    key=stored.object_key,
                    version_id=stored.version_id,
                ),
                expires_in_seconds=300,
            )
        )
        return AgentInputSlot(
            artifact_id=stored.artifact_id,
            mime=stored.mime,
            size=stored.size,
            sha256_b64=stored.sha256_b64,
            url=authorization.url,
            expires_at=now + timedelta(seconds=authorization.expires_in_seconds),
        )

    async def create_upload(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        artifact_kind: str,
        mime: str,
        size: int,
        sha256_b64: str,
    ) -> AgentUploadSlot:
        descriptor = _validate_descriptor(
            artifact_kind=artifact_kind,
            mime=mime,
            size=size,
            sha256_b64=sha256_b64,
        )
        now = _aware(self._clock())
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        if descriptor.artifact_kind not in access.allowed_uploads:
            raise AgentUploadInvalidRequest("artifact kind is not allowed by this task")
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        artifact_id = self._uuid_source()
        upload_id = self._uuid_source()
        object_key = (
            f"raw/analyses/{access.analysis_id}/agent/{access.execution_id}/"
            f"{descriptor.artifact_kind}/{upload_id}"
        )
        location = ObjectLocation(bucket=tenant.bucket, key=object_key)
        created = await _available(
            self._artifact_store.create_multipart(
                location=location,
                content_type=descriptor.mime,
            )
        )
        try:
            reservation = await _available(
                self._repository.reserve_upload(
                    tenant=tenant,
                    access=access,
                    descriptor=descriptor,
                    artifact_id=artifact_id,
                    upload_id=upload_id,
                    object_key=object_key,
                    storage_upload_id=created.storage_upload_id,
                    part_size_bytes=_PART_SIZE_BYTES,
                    part_count=math.ceil(descriptor.size / _PART_SIZE_BYTES),
                    now=now,
                    expires_at=now + _SLOT_LIFETIME,
                )
            )
        except BaseException:
            await self._best_effort_abort(location, created.storage_upload_id)
            raise
        if not reservation.created:
            await self._best_effort_abort(location, created.storage_upload_id)
        return _slot(reservation.upload)

    async def authorize_part(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        upload_id: UUID,
        part_number: int,
    ) -> AgentUploadPartSlot:
        now = _aware(self._clock())
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        stored = await _available(
            self._repository.load_upload(
                tenant=tenant,
                access=access,
                upload_id=upload_id,
            )
        )
        if stored.state != "pending" or stored.expires_at <= now:
            raise AgentUploadExpired("upload has expired")
        if (
            isinstance(part_number, bool)
            or not isinstance(part_number, int)
            or not 1 <= part_number <= stored.part_count
        ):
            raise AgentUploadInvalidRequest("part number is invalid")
        authorization = await _available(
            self._artifact_store.authorize_part(
                location=ObjectLocation(bucket=tenant.bucket, key=stored.object_key),
                storage_upload_id=stored.storage_upload_id,
                part_number=part_number,
                expires_in_seconds=900,
            )
        )
        return AgentUploadPartSlot(
            upload_id=upload_id,
            part_number=part_number,
            url=authorization.url,
            required_headers=dict(authorization.required_headers),
            expires_at=now + timedelta(seconds=authorization.expires_in_seconds),
        )

    async def complete_upload(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        upload_id: UUID,
        parts: Sequence[MultipartPart],
    ) -> AgentUploadSlot:
        now = _aware(self._clock())
        access = await self._authorize(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        stored = await _available(
            self._repository.load_upload(
                tenant=tenant,
                access=access,
                upload_id=upload_id,
            )
        )
        canonical_parts = _validate_parts(parts, expected_count=stored.part_count)
        claim = await _available(
            self._repository.prepare_completion(
                tenant=tenant,
                access=access,
                upload_id=upload_id,
                parts=canonical_parts,
                now=now,
            )
        )
        if claim.upload.state == "finalized":
            return _slot(claim.upload)
        location = ObjectLocation(bucket=tenant.bucket, key=claim.upload.object_key)
        if claim.call_storage_complete:
            await _available(
                self._artifact_store.complete_multipart(
                    location=location,
                    storage_upload_id=claim.upload.storage_upload_id,
                    parts=canonical_parts,
                )
            )
        metadata = await _head(self._artifact_store, location)
        version_id = metadata.location.version_id
        if (
            metadata.location.bucket != tenant.bucket
            or metadata.location.key != claim.upload.object_key
            or version_id is None
            or metadata.size_bytes != claim.upload.size
            or metadata.content_type != claim.upload.mime
            or not hmac.compare_digest(
                metadata.checksum_sha256_b64,
                claim.upload.sha256_b64,
            )
        ):
            raise AgentUploadMismatch("uploaded object metadata does not match")
        finalized = await _available(
            self._repository.finalize_upload(
                tenant=tenant,
                access=access,
                upload_id=upload_id,
                storage_version_id=version_id,
                now=now,
                expires_at=now + _RETENTION_LIFETIME,
            )
        )
        return _slot(finalized)

    async def validate_completion(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
    ) -> None:
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        await _available(
            self._repository.validate_completion(
                tenant=tenant,
                access=access,
                manifest=manifest,
            )
        )

    async def project_completion(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> None:
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        await _available(
            self._repository.project_completion(
                tenant=tenant,
                access=access,
                manifest=manifest,
                now=now,
            )
        )

    async def abort_execution(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> None:
        canonical_now = _aware(now)
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        uploads = await _available(
            self._repository.pending_execution_uploads(
                tenant=tenant,
                access=access,
            )
        )
        for upload in uploads:
            await _available(
                self._artifact_store.abort_multipart(
                    location=ObjectLocation(bucket=tenant.bucket, key=upload.object_key),
                    storage_upload_id=upload.storage_upload_id,
                )
            )
            await _available(
                self._repository.mark_upload_aborted(
                    tenant=tenant,
                    access=access,
                    upload_id=upload.upload_id,
                    now=canonical_now,
                )
            )

    async def project_cancellation(
        self,
        *,
        access: AgentExecutionAccess,
        reason_code: str,
        now: datetime,
    ) -> None:
        canonical_now = _aware(now)
        tenant = await _available(self._bucket_resolver.active_for_team(access.team_id))
        await _available(
            self._repository.project_cancellation(
                tenant=tenant,
                access=access,
                reason_code=reason_code,
                now=canonical_now,
            )
        )

    async def _authorize(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess:
        try:
            return await self._execution_authorizer.authorize_execution(
                agent_id=agent_id,
                execution_id=execution_id,
                lease_version=lease_version,
                now=now,
            )
        except AgentUploadError:
            raise
        except StaleLeaseVersion:
            raise AgentUploadStaleLease("lease version is stale") from None
        except AgentTaskNotFound:
            raise AgentUploadNotFound("execution was not found") from None
        except Exception:
            raise AgentUploadUnavailable("upload service is unavailable") from None

    async def _best_effort_abort(self, location: ObjectLocation, storage_upload_id: str) -> None:
        try:
            await self._artifact_store.abort_multipart(
                location=location,
                storage_upload_id=storage_upload_id,
            )
        except BaseException:
            return


def _validate_descriptor(
    *,
    artifact_kind: str,
    mime: str,
    size: int,
    sha256_b64: str,
) -> AgentUploadDescriptor:
    if (
        not isinstance(artifact_kind, str)
        or not artifact_kind
        or len(artifact_kind) > 96
        or _MIME_TYPE.fullmatch(mime) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= _MAX_OUTPUT_BYTES
    ):
        raise AgentUploadInvalidRequest("upload request is invalid")
    try:
        raw = base64.b64decode(sha256_b64, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise AgentUploadInvalidRequest("upload request is invalid") from None
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != sha256_b64:
        raise AgentUploadInvalidRequest("upload request is invalid")
    part_count = math.ceil(size / _PART_SIZE_BYTES)
    if part_count < 1 or part_count > _MAX_PARTS:
        raise AgentUploadInvalidRequest("upload request is invalid")
    return AgentUploadDescriptor(artifact_kind, mime, size, sha256_b64)


def _match_completion_artifacts(
    stored: tuple[StoredAgentUpload, ...],
    manifest: ValidatedAgentExecutionManifest,
) -> None:
    by_id = {item.artifact_id: item for item in stored}
    if len(by_id) != len(stored) or set(by_id) != {item.artifact_id for item in manifest.artifacts}:
        raise AgentUploadMismatch("execution artifacts do not match finalized uploads")
    for artifact in manifest.artifacts:
        observed = by_id[artifact.artifact_id]
        if (
            observed.state != "finalized"
            or observed.execution_id != manifest.execution_id
            or observed.artifact_kind != artifact.kind
            or observed.mime != artifact.mime
            or observed.size != artifact.size
            or not hmac.compare_digest(observed.sha256_b64, artifact.sha256_b64)
        ):
            raise AgentUploadMismatch("execution artifacts do not match finalized uploads")


def _validate_parts(
    parts: Sequence[MultipartPart],
    *,
    expected_count: int,
) -> tuple[MultipartPart, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != expected_count:
        raise AgentUploadInvalidRequest("multipart completion is invalid")
    canonical = tuple(parts)
    if any(
        not isinstance(part, MultipartPart)
        or part.part_number != index
        or _ETAG.fullmatch(part.etag) is None
        for index, part in enumerate(canonical, start=1)
    ):
        raise AgentUploadInvalidRequest("multipart completion is invalid")
    return canonical


def _parse_stored_parts(value: object) -> tuple[MultipartPart, ...]:
    if not isinstance(value, list):
        raise AgentUploadUnavailable("upload service is unavailable")
    parts: list[MultipartPart] = []
    for index, item in enumerate(value, start=1):
        if (
            not isinstance(item, dict)
            or set(item) != {"part_number", "etag"}
            or item.get("part_number") != index
            or not isinstance(item.get("etag"), str)
            or _ETAG.fullmatch(item["etag"]) is None
        ):
            raise AgentUploadUnavailable("upload service is unavailable")
        parts.append(MultipartPart(part_number=index, etag=item["etag"]))
    return tuple(parts)


def _same_descriptor(stored: StoredAgentUpload, descriptor: AgentUploadDescriptor) -> bool:
    return (
        stored.artifact_kind == descriptor.artifact_kind
        and stored.mime == descriptor.mime
        and stored.size == descriptor.size
        and hmac.compare_digest(stored.sha256_b64, descriptor.sha256_b64)
    )


def _request_hash(
    *,
    access: AgentExecutionAccess,
    descriptor: AgentUploadDescriptor,
) -> str:
    payload = json.dumps(
        {
            "analysis_id": str(access.analysis_id),
            "execution_id": str(access.execution_id),
            "kind": descriptor.artifact_kind,
            "mime": descriptor.mime,
            "sha256_b64": descriptor.sha256_b64,
            "size": descriptor.size,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


async def _available(awaitable):
    try:
        return await awaitable
    except AgentUploadError:
        raise
    except Exception:
        raise AgentUploadUnavailable("upload service is unavailable") from None


async def _head(store: MultipartArtifactStore, location: ObjectLocation):
    try:
        return await store.head(location=location)
    except (ArtifactMetadataError, ArtifactNotFoundError):
        raise AgentUploadMismatch("uploaded object metadata does not match") from None
    except Exception:
        raise AgentUploadUnavailable("upload service is unavailable") from None


def _slot(stored: StoredAgentUpload) -> AgentUploadSlot:
    return AgentUploadSlot(
        artifact_id=stored.artifact_id,
        upload_id=stored.upload_id,
        artifact_kind=stored.artifact_kind,
        mime=stored.mime,
        size=stored.size,
        sha256_b64=stored.sha256_b64,
        part_size_bytes=stored.part_size_bytes,
        part_count=stored.part_count,
        state=stored.state,
        expires_at=stored.expires_at,
        finalized_at=stored.finalized_at,
    )


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AgentUploadUnavailable("upload service is unavailable")
    return value.astimezone(UTC)


__all__ = [
    "AgentInputSlot",
    "AgentUploadError",
    "AgentUploadExpired",
    "AgentUploadInvalidRequest",
    "AgentUploadMismatch",
    "AgentUploadNotFound",
    "AgentUploadPartSlot",
    "AgentUploadService",
    "AgentUploadSlot",
    "AgentUploadStaleLease",
    "AgentUploadUnavailable",
    "InMemoryAgentUploadRepository",
    "SQLAlchemyAgentUploadRepository",
    "StoredAgentUpload",
    "StoredAgentInput",
]
