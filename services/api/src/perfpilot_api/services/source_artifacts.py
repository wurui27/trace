"""Immutable tenant-private artifacts for source tasks and validation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Mapping
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.reports.source_context import (
    validate_source_context,
    validate_source_context_transport,
)
from perfpilot_api.services.source_tasks import SourceCompletionArtifact, SourceTaskView
from perfpilot_api.services.uploads import BucketResolver, TenantBucket


SourceArtifactKind = Literal[
    "source_context",
    "source_context_validated",
    "source_patch",
    "source_validation",
]
_NAMESPACE = UUID("35b5bcad-b79d-52f1-87ec-4b004440cc7a")
_RETENTION = timedelta(days=30)
_DIRECTORIES = {
    "source_context": "source-context",
    "source_context_validated": "source-context",
    "source_patch": "source-patches",
    "source_validation": "source-validation",
}


class SourceArtifactError(RuntimeError):
    pass


class SourceArtifactConflictError(SourceArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("source artifact integrity conflict")


class SourceArtifactUnavailableError(SourceArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("source artifact service is unavailable")


@dataclass(frozen=True, slots=True)
class SourceArtifactRecord:
    team_id: UUID
    analysis_id: UUID
    artifact_id: UUID
    kind: SourceArtifactKind
    mime_type: str
    size_bytes: int
    checksum: str = field(repr=False)
    object_key: str = field(repr=False)
    version_id: str = field(repr=False)
    expires_at: datetime


def source_artifact_id(execution_id: UUID, checksum: str) -> UUID:
    if not isinstance(execution_id, UUID) or not _checksum(checksum):
        raise SourceArtifactConflictError
    return uuid5(_NAMESPACE, f"{execution_id}:{checksum}")


def validated_source_artifact_id(source_artifact_id: UUID, checksum: str) -> UUID:
    if not isinstance(source_artifact_id, UUID) or not _checksum(checksum):
        raise SourceArtifactConflictError
    return uuid5(_NAMESPACE, f"validated:{source_artifact_id}:{checksum}")


def source_artifact_key(
    analysis_id: UUID,
    artifact_id: UUID,
    kind: SourceArtifactKind,
) -> str:
    if (
        not isinstance(analysis_id, UUID)
        or not isinstance(artifact_id, UUID)
        or kind not in _DIRECTORIES
    ):
        raise SourceArtifactConflictError
    suffix = ".patch" if kind == "source_patch" else ".json"
    return (
        f"raw/analyses/{analysis_id}/internal/{_DIRECTORIES[kind]}/"
        f"{artifact_id}{suffix}"
    )


def _checksum(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class SourceArtifactService:
    """Small immutable artifact boundary; production adapters may mirror this API."""

    def __init__(self) -> None:
        self._records: dict[UUID, SourceArtifactRecord] = {}
        self._objects: dict[tuple[UUID, str, str], bytes] = {}

    @classmethod
    def in_memory(cls) -> "SourceArtifactService":
        return cls()

    @staticmethod
    def canonical_bytes(document: object) -> bytes:
        try:
            return json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise SourceArtifactConflictError from None

    def record(self, artifact_id: UUID) -> SourceArtifactRecord:
        try:
            return self._records[artifact_id]
        except KeyError:
            raise SourceArtifactUnavailableError from None

    async def record_completion(
        self,
        *,
        task: SourceTaskView,
        document: Mapping[str, object],
        checksum: str,
        now: datetime,
    ) -> SourceCompletionArtifact:
        if (
            not isinstance(task, SourceTaskView)
            or task.task_type != "source_context"
            or not isinstance(document, Mapping)
            or not _checksum(checksum)
            or now.tzinfo is None
            or now.utcoffset() is None
            or document.get("task_type") != "source_context"
            or document.get("execution_id") != str(task.execution_id)
            or document.get("analysis_id") != str(task.analysis_id)
            or document.get("team_id") != str(task.team_id)
            or document.get("workspace_id") != str(task.workspace_id)
        ):
            raise SourceArtifactConflictError
        payload = self.canonical_bytes(document)
        if len(payload) > 128 * 1024 or not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(), checksum
        ):
            raise SourceArtifactConflictError
        if document.get("state") == "completed":
            result = document.get("result")
            if not isinstance(result, Mapping):
                raise SourceArtifactConflictError
            validate_source_context_transport(result)
        artifact_id = source_artifact_id(task.execution_id, checksum)
        key = source_artifact_key(task.analysis_id, artifact_id, "source_context")
        existing = self._records.get(artifact_id)
        if existing is not None:
            if (
                existing.team_id != task.team_id
                or existing.analysis_id != task.analysis_id
                or not hmac.compare_digest(existing.checksum, checksum)
            ):
                raise SourceArtifactConflictError
            return SourceCompletionArtifact(artifact_id=artifact_id, checksum=checksum)
        version_id = hashlib.sha256(
            f"{task.team_id}:{key}:{checksum}".encode("ascii")
        ).hexdigest()
        record = SourceArtifactRecord(
            team_id=task.team_id,
            analysis_id=task.analysis_id,
            artifact_id=artifact_id,
            kind="source_context",
            mime_type="application/json",
            size_bytes=len(payload),
            checksum=checksum,
            object_key=key,
            version_id=version_id,
            expires_at=now.astimezone(UTC) + _RETENTION,
        )
        self._objects[(task.team_id, key, version_id)] = bytes(payload)
        self._records[artifact_id] = record
        return SourceCompletionArtifact(artifact_id=artifact_id, checksum=checksum)

    async def read_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_checksum: str,
        direct_identifiers: tuple[str, ...] = (),
        allowed_finding_ids: tuple[str, ...] | None = (),
        allowed_evidence_ids: tuple[str, ...] | None = (),
    ) -> dict[str, object]:
        record = self._records.get(artifact_id)
        if (
            record is None
            or record.team_id != team_id
            or record.analysis_id != analysis_id
            or record.kind != "source_context"
            or record.mime_type != "application/json"
            or not _checksum(expected_checksum)
            or not hmac.compare_digest(record.checksum, expected_checksum)
        ):
            raise SourceArtifactUnavailableError
        payload = self._objects.get((team_id, record.object_key, record.version_id))
        if (
            payload is None
            or len(payload) != record.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), record.checksum)
        ):
            raise SourceArtifactUnavailableError
        try:
            completion = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            raise SourceArtifactUnavailableError from None
        if (
            not isinstance(completion, dict)
            or completion.get("state") != "completed"
            or completion.get("analysis_id") != str(analysis_id)
            or completion.get("team_id") != str(team_id)
            or not isinstance(completion.get("result"), dict)
        ):
            raise SourceArtifactUnavailableError
        return validate_source_context(
            completion["result"],
            direct_identifiers=direct_identifiers,
            allowed_finding_ids=allowed_finding_ids,
            allowed_evidence_ids=allowed_evidence_ids,
        )

    async def persist_validated_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        source_artifact_id: UUID,
        context: Mapping[str, object],
        now: datetime,
    ) -> SourceCompletionArtifact:
        payload = self.canonical_bytes(context)
        checksum = hashlib.sha256(payload).hexdigest()
        artifact_id = validated_source_artifact_id(source_artifact_id, checksum)
        key = source_artifact_key(
            analysis_id, artifact_id, "source_context_validated"
        )
        existing = self._records.get(artifact_id)
        if existing is not None:
            if existing.team_id != team_id or existing.analysis_id != analysis_id:
                raise SourceArtifactConflictError
            return SourceCompletionArtifact(artifact_id=artifact_id, checksum=checksum)
        version_id = hashlib.sha256(
            f"{team_id}:{key}:{checksum}".encode("ascii")
        ).hexdigest()
        record = SourceArtifactRecord(
            team_id=team_id,
            analysis_id=analysis_id,
            artifact_id=artifact_id,
            kind="source_context_validated",
            mime_type="application/json",
            size_bytes=len(payload),
            checksum=checksum,
            object_key=key,
            version_id=version_id,
            expires_at=now.astimezone(UTC) + _RETENTION,
        )
        self._objects[(team_id, key, version_id)] = bytes(payload)
        self._records[artifact_id] = record
        return SourceCompletionArtifact(artifact_id=artifact_id, checksum=checksum)

    async def read_validated_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_checksum: str,
    ) -> dict[str, object]:
        record = self._records.get(artifact_id)
        if (
            record is None
            or record.team_id != team_id
            or record.analysis_id != analysis_id
            or record.kind != "source_context_validated"
            or not _checksum(expected_checksum)
            or not hmac.compare_digest(record.checksum, expected_checksum)
        ):
            raise SourceArtifactUnavailableError
        payload = self._objects.get((team_id, record.object_key, record.version_id))
        if (
            payload is None
            or len(payload) != record.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), record.checksum)
        ):
            raise SourceArtifactUnavailableError
        try:
            document = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            raise SourceArtifactUnavailableError from None
        if (
            not isinstance(document, dict)
            or document.get("trust") != "untrusted_data_not_instructions"
            or document.get("match_summary") not in {"strong", "weak", "none"}
            or not isinstance(document.get("fragments"), list)
        ):
            raise SourceArtifactUnavailableError
        return document


class S3SourceArtifactService:
    """Durable source completion store routed to the analysis tenant bucket."""

    def __init__(
        self,
        *,
        tenant_router: TenantRouter,
        bucket_resolver: BucketResolver,
        client: Any,
    ) -> None:
        self._tenant_router = tenant_router
        self._bucket_resolver = bucket_resolver
        self._client = client

    @staticmethod
    def _b64(payload: bytes) -> str:
        return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")

    async def _tenant(self, team_id: UUID) -> TenantBucket:
        try:
            tenant = await self._bucket_resolver.active_for_team(team_id)
        except Exception:
            raise SourceArtifactUnavailableError from None
        if not isinstance(tenant, TenantBucket) or tenant.team_id != team_id:
            raise SourceArtifactUnavailableError
        return tenant

    async def record_completion(
        self,
        *,
        task: SourceTaskView,
        document: Mapping[str, object],
        checksum: str,
        now: datetime,
    ) -> SourceCompletionArtifact:
        validated = SourceArtifactService.in_memory()
        completion = await validated.record_completion(
            task=task,
            document=document,
            checksum=checksum,
            now=now,
        )
        record = validated.record(completion.artifact_id)
        payload = validated._objects[(task.team_id, record.object_key, record.version_id)]
        checksum_b64 = self._b64(payload)
        tenant = await self._tenant(task.team_id)
        expires_at = now.astimezone(UTC) + _RETENTION
        try:
            async with self._tenant_router.session(task.team_id) as session:
                routed_version = session.info.get("tenant_resource_version")
                if routed_version != tenant.resource_version:
                    raise SourceArtifactUnavailableError
                owner = await session.scalar(
                    select(Analysis.id).where(
                        Analysis.id == task.analysis_id,
                        Analysis.tombstoned_at.is_(None),
                        Analysis.state != "deleted",
                    )
                )
                if owner is None:
                    raise SourceArtifactConflictError
                await session.execute(
                    postgresql_insert(Artifact)
                    .values(
                        id=completion.artifact_id,
                        analysis_id=task.analysis_id,
                        upload_id=completion.artifact_id,
                        idempotency_key=f"internal:source_context:{completion.artifact_id}",
                        request_hash=checksum,
                        artifact_kind="source_context",
                        mime_type="application/json",
                        size_bytes=len(payload),
                        sha256_b64=checksum_b64,
                        object_key=record.object_key,
                        version_id=None,
                        state="pending",
                        finalized_at=None,
                        expires_at=expires_at,
                        deleted_at=None,
                        version=1,
                    )
                    .on_conflict_do_nothing(index_elements=(Artifact.id,))
                )
                row = await session.get(Artifact, completion.artifact_id)
                if (
                    row is None
                    or row.analysis_id != task.analysis_id
                    or row.request_hash != checksum
                    or row.artifact_kind != "source_context"
                    or row.mime_type != "application/json"
                    or row.size_bytes != len(payload)
                    or row.sha256_b64 != checksum_b64
                    or row.object_key != record.object_key
                    or row.state not in {"pending", "finalized"}
                ):
                    raise SourceArtifactConflictError
                if row.state == "finalized":
                    return completion
                expected_version = row.version
            receipt = await asyncio.to_thread(
                self._client.put_object,
                Bucket=tenant.bucket,
                Key=record.object_key,
                Body=payload,
                ContentType="application/json",
                ChecksumSHA256=checksum_b64,
            )
            version_id = receipt.get("VersionId") if isinstance(receipt, Mapping) else None
            returned_checksum = (
                receipt.get("ChecksumSHA256") if isinstance(receipt, Mapping) else None
            )
            if (
                not isinstance(version_id, str)
                or not version_id
                or returned_checksum != checksum_b64
            ):
                raise SourceArtifactUnavailableError
            async with self._tenant_router.session(task.team_id) as session:
                finalized = await session.scalar(
                    update(Artifact)
                    .where(
                        Artifact.id == completion.artifact_id,
                        Artifact.analysis_id == task.analysis_id,
                        Artifact.state == "pending",
                        Artifact.version == expected_version,
                    )
                    .values(
                        state="finalized",
                        version_id=version_id,
                        finalized_at=now,
                        expires_at=expires_at,
                        updated_at=now,
                        version=Artifact.version + 1,
                    )
                    .returning(Artifact.id)
                )
                if finalized != completion.artifact_id:
                    raise SourceArtifactConflictError
        except SourceArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise SourceArtifactUnavailableError from None
        return completion

    async def read_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_checksum: str,
        direct_identifiers: tuple[str, ...] = (),
        allowed_finding_ids: tuple[str, ...] | None = (),
        allowed_evidence_ids: tuple[str, ...] | None = (),
    ) -> dict[str, object]:
        tenant = await self._tenant(team_id)
        try:
            async with self._tenant_router.session(team_id) as session:
                row = await session.get(Artifact, artifact_id)
                if (
                    row is None
                    or row.analysis_id != analysis_id
                    or row.artifact_kind != "source_context"
                    or row.mime_type != "application/json"
                    or row.state != "finalized"
                    or row.version_id is None
                    or row.request_hash != expected_checksum
                    or row.object_key != source_artifact_key(
                        analysis_id, artifact_id, "source_context"
                    )
                ):
                    raise SourceArtifactUnavailableError
                object_key = row.object_key
                version_id = row.version_id
                size_bytes = row.size_bytes
                checksum_b64 = row.sha256_b64
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=tenant.bucket,
                Key=object_key,
                VersionId=version_id,
                ChecksumMode="ENABLED",
            )
            if not isinstance(response, Mapping):
                raise SourceArtifactUnavailableError
            body = response.get("Body")
            if not callable(getattr(body, "read", None)):
                raise SourceArtifactUnavailableError
            payload = await asyncio.to_thread(body.read)
            if (
                not isinstance(payload, bytes)
                or len(payload) != size_bytes
                or self._b64(payload) != checksum_b64
                or hashlib.sha256(payload).hexdigest() != expected_checksum
            ):
                raise SourceArtifactUnavailableError
            completion = json.loads(payload)
        except SourceArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise SourceArtifactUnavailableError from None
        if (
            not isinstance(completion, dict)
            or completion.get("state") != "completed"
            or not isinstance(completion.get("result"), dict)
        ):
            raise SourceArtifactUnavailableError
        return validate_source_context(
            completion["result"],
            direct_identifiers=direct_identifiers,
            allowed_finding_ids=allowed_finding_ids,
            allowed_evidence_ids=allowed_evidence_ids,
        )

    async def persist_validated_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        source_artifact_id: UUID,
        context: Mapping[str, object],
        now: datetime,
    ) -> SourceCompletionArtifact:
        temporary = SourceArtifactService.in_memory()
        completion = await temporary.persist_validated_context(
            team_id=team_id,
            analysis_id=analysis_id,
            source_artifact_id=source_artifact_id,
            context=context,
            now=now,
        )
        record = temporary.record(completion.artifact_id)
        payload = temporary._objects[(team_id, record.object_key, record.version_id)]
        checksum_b64 = self._b64(payload)
        tenant = await self._tenant(team_id)
        expires_at = now.astimezone(UTC) + _RETENTION
        try:
            async with self._tenant_router.session(team_id) as session:
                if session.info.get("tenant_resource_version") != tenant.resource_version:
                    raise SourceArtifactUnavailableError
                owner = await session.scalar(
                    select(Analysis.id).where(
                        Analysis.id == analysis_id,
                        Analysis.tombstoned_at.is_(None),
                        Analysis.state != "deleted",
                    )
                )
                if owner is None:
                    raise SourceArtifactConflictError
                await session.execute(
                    postgresql_insert(Artifact)
                    .values(
                        id=completion.artifact_id,
                        analysis_id=analysis_id,
                        upload_id=completion.artifact_id,
                        idempotency_key=(
                            f"internal:source_context_validated:{source_artifact_id}"
                        ),
                        request_hash=completion.checksum,
                        artifact_kind="source_context_validated",
                        mime_type="application/json",
                        size_bytes=len(payload),
                        sha256_b64=checksum_b64,
                        object_key=record.object_key,
                        version_id=None,
                        state="pending",
                        finalized_at=None,
                        expires_at=expires_at,
                        deleted_at=None,
                        version=1,
                    )
                    .on_conflict_do_nothing(index_elements=(Artifact.id,))
                )
                row = await session.get(Artifact, completion.artifact_id)
                if (
                    row is None
                    or row.analysis_id != analysis_id
                    or row.request_hash != completion.checksum
                    or row.artifact_kind != "source_context_validated"
                    or row.object_key != record.object_key
                    or row.state not in {"pending", "finalized"}
                ):
                    raise SourceArtifactConflictError
                if row.state == "finalized":
                    return completion
                expected_version = row.version
            receipt = await asyncio.to_thread(
                self._client.put_object,
                Bucket=tenant.bucket,
                Key=record.object_key,
                Body=payload,
                ContentType="application/json",
                ChecksumSHA256=checksum_b64,
            )
            version_id = receipt.get("VersionId") if isinstance(receipt, Mapping) else None
            returned_checksum = (
                receipt.get("ChecksumSHA256") if isinstance(receipt, Mapping) else None
            )
            if not isinstance(version_id, str) or not version_id or returned_checksum != checksum_b64:
                raise SourceArtifactUnavailableError
            async with self._tenant_router.session(team_id) as session:
                finalized = await session.scalar(
                    update(Artifact)
                    .where(
                        Artifact.id == completion.artifact_id,
                        Artifact.analysis_id == analysis_id,
                        Artifact.state == "pending",
                        Artifact.version == expected_version,
                    )
                    .values(
                        state="finalized",
                        version_id=version_id,
                        finalized_at=now,
                        expires_at=expires_at,
                        updated_at=now,
                        version=Artifact.version + 1,
                    )
                    .returning(Artifact.id)
                )
                if finalized != completion.artifact_id:
                    raise SourceArtifactConflictError
        except SourceArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise SourceArtifactUnavailableError from None
        return completion

    async def read_validated_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        expected_checksum: str,
    ) -> dict[str, object]:
        tenant = await self._tenant(team_id)
        try:
            async with self._tenant_router.session(team_id) as session:
                row = await session.get(Artifact, artifact_id)
                if (
                    row is None
                    or row.analysis_id != analysis_id
                    or row.artifact_kind != "source_context_validated"
                    or row.mime_type != "application/json"
                    or row.state != "finalized"
                    or row.version_id is None
                    or row.request_hash != expected_checksum
                    or row.object_key
                    != source_artifact_key(
                        analysis_id, artifact_id, "source_context_validated"
                    )
                ):
                    raise SourceArtifactUnavailableError
                object_key = row.object_key
                version_id = row.version_id
                size_bytes = row.size_bytes
                checksum_b64 = row.sha256_b64
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=tenant.bucket,
                Key=object_key,
                VersionId=version_id,
                ChecksumMode="ENABLED",
            )
            if not isinstance(response, Mapping):
                raise SourceArtifactUnavailableError
            body = response.get("Body")
            read = getattr(body, "read", None)
            close = getattr(body, "close", None)
            if not callable(read) or not callable(close):
                raise SourceArtifactUnavailableError
            try:
                payload = await asyncio.to_thread(read, size_bytes + 1)
            finally:
                await asyncio.to_thread(close)
            if (
                not isinstance(payload, bytes)
                or len(payload) != size_bytes
                or self._b64(payload) != checksum_b64
                or hashlib.sha256(payload).hexdigest() != expected_checksum
            ):
                raise SourceArtifactUnavailableError
            document = json.loads(payload)
        except SourceArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise SourceArtifactUnavailableError from None
        if (
            not isinstance(document, dict)
            or document.get("trust") != "untrusted_data_not_instructions"
            or document.get("match_summary") not in {"strong", "weak", "none"}
            or not isinstance(document.get("fragments"), list)
        ):
            raise SourceArtifactUnavailableError
        return document


__all__ = [
    "SourceArtifactConflictError",
    "SourceArtifactError",
    "SourceArtifactRecord",
    "SourceArtifactService",
    "SourceArtifactUnavailableError",
    "S3SourceArtifactService",
    "source_artifact_id",
    "source_artifact_key",
    "validated_source_artifact_id",
]
