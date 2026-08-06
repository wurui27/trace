"""Prepare tenant-owned Android memory inputs for durable engine execution."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select

from perfpilot_api.db.tenant.models import Analysis, Artifact
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.engines.contracts import EngineInput
from perfpilot_api.services.engine_executions import EngineExecutionRecord
from perfpilot_api.services.internal_artifacts import manifest_artifact_id
from perfpilot_api.services.uploads import (
    BucketResolver,
    DownloadAuthorization,
    TenantBucket,
    UploadError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadService,
)


_MANIFEST_KIND = "memory_capture_manifest"
_MANIFEST_MIME = "application/json"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_EVIDENCE_KINDS = frozenset({"memory_evidence", "capture_manifest", "log", "screenshot", "trace"})
_MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")
_CAPTURE_SOURCES_BY_MODE = {
    "device": "adb_agent",
    "memory_upload": "manual_upload",
}


class MemoryExecutionError(RuntimeError):
    """A stable execution-preparation failure without tenant-private metadata."""


class MemoryExecutionNotFoundError(MemoryExecutionError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("memory capture was not found")


class MemoryExecutionUnavailableError(MemoryExecutionError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("memory execution service is unavailable")


@dataclass(frozen=True, slots=True)
class MemoryExecutionArtifact:
    """Tenant-authoritative public metadata for one immutable input."""

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


@dataclass(frozen=True, slots=True)
class LoadedMemoryCapture:
    analysis_id: UUID
    analysis_mode: str
    analysis_state: str
    tombstoned_at: datetime | None
    tenant_resource_version: int
    question: str | None
    manifest: MemoryCaptureManifest
    manifest_bytes: bytes = field(repr=False)
    manifest_artifact: MemoryExecutionArtifact
    evidence_artifacts: tuple[MemoryExecutionArtifact, ...]


@dataclass(frozen=True, slots=True)
class PreparedMemoryExecution:
    execution: EngineExecutionRecord
    inputs: tuple[EngineInput, ...]
    question: str | None


class MemoryExecutionRepository(Protocol):
    async def load_capture(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        capture_id: UUID,
    ) -> LoadedMemoryCapture: ...

    async def require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None: ...


class MemoryAttemptService(Protocol):
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


@dataclass(frozen=True, slots=True)
class _StoredArtifact:
    public: MemoryExecutionArtifact
    object_key: str = field(repr=False)
    version_id: str = field(repr=False)


def _sha256_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _is_canonical_checksum(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 32 and base64.b64encode(decoded).decode("ascii") == value


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _safe_version(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value == "null"
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def _is_unexpired(value: object, *, now: datetime) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None or now.tzinfo is None:
        return False
    try:
        return value > now
    except (TypeError, ValueError):
        return False


def _public_artifact(row: Artifact) -> MemoryExecutionArtifact:
    if row.analysis_id is None:
        raise MemoryExecutionNotFoundError
    return MemoryExecutionArtifact(
        artifact_id=row.id,
        analysis_id=row.analysis_id,
        artifact_kind=row.artifact_kind,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256_b64=row.sha256_b64,
        version=row.version,
        state=row.state,
        expires_at=row.expires_at,
        deleted_at=row.deleted_at,
    )


def canonical_memory_config_hash(
    *,
    capture_id: UUID,
    question: str | None,
    timeout_seconds: int,
) -> str:
    if (
        not isinstance(capture_id, UUID)
        or (question is not None and (not isinstance(question, str) or len(question) > 2_000))
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 3_600
    ):
        raise ValueError("memory execution config is invalid")
    payload = json.dumps(
        {
            "capture_id": str(capture_id),
            "question": question,
            "timeout_seconds": timeout_seconds,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class SQLAlchemyMemoryExecutionRepository:
    """Read one capture through a matching DB route and immutable bucket version."""

    def __init__(
        self,
        *,
        tenant_router: TenantRouter,
        bucket_resolver: BucketResolver,
        client: Any,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._tenant_router = tenant_router
        self._bucket_resolver = bucket_resolver
        self._client = client
        self._clock = clock

    @staticmethod
    def _tenant_matches(
        tenant: object,
        *,
        team_id: UUID,
        resource_version: int | None = None,
    ) -> bool:
        return (
            isinstance(tenant, TenantBucket)
            and tenant.team_id == team_id
            and isinstance(tenant.bucket, str)
            and bool(tenant.bucket)
            and type(tenant.resource_version) is int
            and tenant.resource_version >= 1
            and (resource_version is None or tenant.resource_version == resource_version)
        )

    async def _resolve_tenant(
        self,
        *,
        team_id: UUID,
        resource_version: int | None = None,
    ) -> TenantBucket:
        try:
            tenant = await self._bucket_resolver.active_for_team(team_id)
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        if not self._tenant_matches(
            tenant,
            team_id=team_id,
            resource_version=resource_version,
        ):
            raise MemoryExecutionUnavailableError
        return tenant

    async def require_resource_version(
        self,
        *,
        team_id: UUID,
        expected_resource_version: int,
    ) -> None:
        if type(expected_resource_version) is not int or expected_resource_version < 1:
            raise MemoryExecutionUnavailableError
        await self._resolve_tenant(
            team_id=team_id,
            resource_version=expected_resource_version,
        )
        try:
            async with self._tenant_router.session(team_id) as session:
                routed_version = session.info.get("tenant_resource_version")
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        if type(routed_version) is not int or routed_version != expected_resource_version:
            raise MemoryExecutionUnavailableError

    @staticmethod
    def _stored_manifest(row: Artifact, *, analysis_id: UUID, now: datetime) -> _StoredArtifact:
        expected_id = row.id
        expected_key = f"raw/analyses/{analysis_id}/internal/{_MANIFEST_KIND}/{expected_id}"
        version_id = _safe_version(row.version_id)
        public = _public_artifact(row)
        if (
            public.analysis_id != analysis_id
            or public.artifact_kind != _MANIFEST_KIND
            or public.mime_type != _MANIFEST_MIME
            or public.state != "finalized"
            or public.deleted_at is not None
            or not _is_unexpired(public.expires_at, now=now)
            or not 1 <= public.size_bytes <= _MAX_MANIFEST_BYTES
            or not _is_canonical_checksum(public.sha256_b64)
            or row.object_key != expected_key
            or version_id is None
        ):
            raise MemoryExecutionNotFoundError
        return _StoredArtifact(public=public, object_key=row.object_key, version_id=version_id)

    async def _read_manifest(
        self,
        *,
        tenant: TenantBucket,
        stored: _StoredArtifact,
    ) -> bytes:
        response: object
        payload: object = None
        body: object = None
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=tenant.bucket,
                Key=stored.object_key,
                VersionId=stored.version_id,
                ChecksumMode="ENABLED",
            )
            metadata = _mapping(response)
            if metadata is None:
                raise MemoryExecutionNotFoundError
            returned_version = _safe_version(metadata.get("VersionId"))
            checksum = metadata.get("ChecksumSHA256")
            content_length = metadata.get("ContentLength")
            if (
                returned_version != stored.version_id
                or not isinstance(checksum, str)
                or not hmac.compare_digest(checksum, stored.public.sha256_b64)
                or metadata.get("ContentType") != stored.public.mime_type
                or type(content_length) is not int
                or content_length != stored.public.size_bytes
                or metadata.get("DeleteMarker", False) is not False
            ):
                raise MemoryExecutionNotFoundError
            body = metadata.get("Body")
            read = getattr(body, "read", None)
            if not callable(read):
                raise MemoryExecutionNotFoundError
            payload = await asyncio.to_thread(read)
        except MemoryExecutionError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:
                    pass
        if (
            not isinstance(payload, bytes)
            or len(payload) != stored.public.size_bytes
            or not hmac.compare_digest(_sha256_b64(payload), stored.public.sha256_b64)
        ):
            raise MemoryExecutionNotFoundError
        return payload

    @staticmethod
    def _validate_evidence(
        row: Artifact,
        *,
        analysis_id: UUID,
        now: datetime,
    ) -> MemoryExecutionArtifact:
        public = _public_artifact(row)
        if (
            public.analysis_id != analysis_id
            or public.artifact_kind not in _EVIDENCE_KINDS
            or _MIME_TYPE.fullmatch(public.mime_type) is None
            or public.size_bytes < 1
            or not _is_canonical_checksum(public.sha256_b64)
            or public.state != "finalized"
            or public.deleted_at is not None
            or not _is_unexpired(public.expires_at, now=now)
            or _safe_version(row.version_id) is None
        ):
            raise MemoryExecutionNotFoundError
        return public

    async def load_capture(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        capture_id: UUID,
    ) -> LoadedMemoryCapture:
        if not all(isinstance(value, UUID) for value in (team_id, analysis_id, capture_id)):
            raise MemoryExecutionNotFoundError
        tenant = await self._resolve_tenant(team_id=team_id)
        manifest_id = manifest_artifact_id(capture_id)
        now = self._clock()
        try:
            async with self._tenant_router.session(team_id) as session:
                routed_version = session.info.get("tenant_resource_version")
                if type(routed_version) is not int or routed_version != tenant.resource_version:
                    raise MemoryExecutionUnavailableError
                analysis = await session.scalar(select(Analysis).where(Analysis.id == analysis_id))
                manifest_row = await session.scalar(
                    select(Artifact).where(
                        Artifact.id == manifest_id,
                        Artifact.analysis_id == analysis_id,
                    )
                )
                if analysis is None or manifest_row is None:
                    raise MemoryExecutionNotFoundError
                stored_manifest = self._stored_manifest(
                    manifest_row,
                    analysis_id=analysis_id,
                    now=now,
                )
                analysis_mode = analysis.analysis_mode
                analysis_state = analysis.state
                tombstoned_at = analysis.tombstoned_at
                question = analysis.question
        except MemoryExecutionError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None

        if (
            analysis_mode not in _CAPTURE_SOURCES_BY_MODE
            or analysis_state == "deleted"
            or tombstoned_at is not None
            or (question is not None and (not isinstance(question, str) or len(question) > 2_000))
        ):
            raise MemoryExecutionNotFoundError

        payload = await self._read_manifest(tenant=tenant, stored=stored_manifest)
        try:
            manifest = MemoryCaptureManifest.model_validate_json(payload)
        except Exception:
            raise MemoryExecutionNotFoundError from None
        if (
            manifest.analysis_id != analysis_id
            or manifest.capture_id != capture_id
            or manifest.source != _CAPTURE_SOURCES_BY_MODE[analysis_mode]
            or manifest_artifact_id(manifest.capture_id) != stored_manifest.public.artifact_id
            or manifest.canonical_bytes() != payload
            or not hmac.compare_digest(
                manifest.sha256_hex(),
                hashlib.sha256(payload).hexdigest(),
            )
        ):
            raise MemoryExecutionNotFoundError

        artifact_ids = tuple(reference.artifact_id for reference in manifest.artifacts)
        try:
            async with self._tenant_router.session(team_id) as session:
                routed_version = session.info.get("tenant_resource_version")
                if routed_version != tenant.resource_version:
                    raise MemoryExecutionUnavailableError
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
        except MemoryExecutionError:
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        by_id = {row.id: row for row in rows}
        if len(rows) != len(artifact_ids) or len(by_id) != len(artifact_ids):
            raise MemoryExecutionNotFoundError
        evidence = tuple(
            self._validate_evidence(by_id[artifact_id], analysis_id=analysis_id, now=now)
            for artifact_id in artifact_ids
        )
        await self.require_resource_version(
            team_id=team_id,
            expected_resource_version=tenant.resource_version,
        )
        return LoadedMemoryCapture(
            analysis_id=analysis_id,
            analysis_mode=analysis_mode,
            analysis_state=analysis_state,
            tombstoned_at=tombstoned_at,
            tenant_resource_version=tenant.resource_version,
            question=question,
            manifest=manifest,
            manifest_bytes=payload,
            manifest_artifact=stored_manifest.public,
            evidence_artifacts=evidence,
        )


class MemoryExecutionService:
    def __init__(
        self,
        *,
        repository: MemoryExecutionRepository,
        upload_service: UploadService,
        engine_service: MemoryAttemptService,
        timeout_seconds: int = 900,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3_600:
            raise ValueError("memory timeout is invalid")
        self._repository = repository
        self._upload_service = upload_service
        self._engine_service = engine_service
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    @staticmethod
    def _valid_artifact(
        artifact: MemoryExecutionArtifact,
        *,
        analysis_id: UUID,
        allowed_kinds: frozenset[str],
        now: datetime,
    ) -> bool:
        return (
            isinstance(artifact, MemoryExecutionArtifact)
            and artifact.analysis_id == analysis_id
            and artifact.artifact_kind in allowed_kinds
            and _MIME_TYPE.fullmatch(artifact.mime_type) is not None
            and type(artifact.size_bytes) is int
            and artifact.size_bytes >= 1
            and _is_canonical_checksum(artifact.sha256_b64)
            and type(artifact.version) is int
            and artifact.version >= 1
            and artifact.state == "finalized"
            and artifact.deleted_at is None
            and _is_unexpired(artifact.expires_at, now=now)
        )

    def _validate_capture(
        self,
        capture: LoadedMemoryCapture,
        *,
        analysis_id: UUID,
        capture_id: UUID,
    ) -> tuple[MemoryExecutionArtifact, ...]:
        now = self._clock()
        if (
            not isinstance(capture, LoadedMemoryCapture)
            or capture.analysis_id != analysis_id
            or capture.analysis_mode not in _CAPTURE_SOURCES_BY_MODE
            or capture.analysis_state == "deleted"
            or capture.tombstoned_at is not None
            or type(capture.tenant_resource_version) is not int
            or capture.tenant_resource_version < 1
            or (
                capture.question is not None
                and (not isinstance(capture.question, str) or len(capture.question) > 2_000)
            )
            or not isinstance(capture.manifest, MemoryCaptureManifest)
            or type(capture.manifest_bytes) is not bytes
            or capture.manifest.analysis_id != analysis_id
            or capture.manifest.capture_id != capture_id
            or capture.manifest.source
            != _CAPTURE_SOURCES_BY_MODE.get(capture.analysis_mode)
            or manifest_artifact_id(capture_id) != capture.manifest_artifact.artifact_id
            or capture.manifest.canonical_bytes() != capture.manifest_bytes
            or not self._valid_artifact(
                capture.manifest_artifact,
                analysis_id=analysis_id,
                allowed_kinds=frozenset({_MANIFEST_KIND}),
                now=now,
            )
            or capture.manifest_artifact.mime_type != _MANIFEST_MIME
            or capture.manifest_artifact.size_bytes != len(capture.manifest_bytes)
            or not hmac.compare_digest(
                capture.manifest_artifact.sha256_b64,
                _sha256_b64(capture.manifest_bytes),
            )
        ):
            raise MemoryExecutionNotFoundError
        if capture.analysis_mode == "device" and (
            len(capture.manifest.artifacts) != 1
            or capture.manifest.artifacts[0].role != "handoff_archive"
            or len(capture.evidence_artifacts) != 1
            or capture.evidence_artifacts[0].artifact_kind != "memory_evidence"
        ):
            raise MemoryExecutionNotFoundError
        referenced_ids = tuple(reference.artifact_id for reference in capture.manifest.artifacts)
        evidence_ids = tuple(artifact.artifact_id for artifact in capture.evidence_artifacts)
        if referenced_ids != evidence_ids or any(
            not self._valid_artifact(
                artifact,
                analysis_id=analysis_id,
                allowed_kinds=_EVIDENCE_KINDS,
                now=now,
            )
            for artifact in capture.evidence_artifacts
        ):
            raise MemoryExecutionNotFoundError
        return (capture.manifest_artifact, *capture.evidence_artifacts)

    async def _claim_input(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact: MemoryExecutionArtifact,
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
            raise MemoryExecutionNotFoundError from None
        except UploadError:
            raise MemoryExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        if (
            not isinstance(authorization, DownloadAuthorization)
            or authorization.artifact_id != artifact.artifact_id
            or type(authorization.tenant_resource_version) is not int
            or authorization.tenant_resource_version != resource_version
            or type(authorization.artifact_version) is not int
            or authorization.artifact_version != artifact.version
            or authorization.artifact_kind != artifact.artifact_kind
            or authorization.mime != artifact.mime_type
            or authorization.size != artifact.size_bytes
            or not isinstance(authorization.sha256_b64, str)
            or not hmac.compare_digest(
                authorization.sha256_b64,
                artifact.sha256_b64,
            )
            or not isinstance(authorization.url, str)
            or not authorization.url
            or not _is_unexpired(authorization.expires_at, now=self._clock())
        ):
            raise MemoryExecutionUnavailableError
        try:
            await self._repository.require_resource_version(
                team_id=team_id,
                expected_resource_version=resource_version,
            )
        except MemoryExecutionError:
            raise MemoryExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
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

    async def prepare(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        capture_id: UUID,
    ) -> PreparedMemoryExecution:
        try:
            capture = await self._repository.load_capture(
                team_id=team_id,
                analysis_id=analysis_id,
                capture_id=capture_id,
            )
        except MemoryExecutionNotFoundError:
            raise MemoryExecutionNotFoundError from None
        except MemoryExecutionError:
            raise MemoryExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        artifacts = self._validate_capture(
            capture,
            analysis_id=analysis_id,
            capture_id=capture_id,
        )
        try:
            await self._repository.require_resource_version(
                team_id=team_id,
                expected_resource_version=capture.tenant_resource_version,
            )
        except MemoryExecutionError:
            raise MemoryExecutionUnavailableError from None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        claimed = tuple(
            [
                await self._claim_input(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    artifact=artifact,
                    resource_version=capture.tenant_resource_version,
                )
                for artifact in artifacts
            ]
        )
        if any(not _is_unexpired(expires_at, now=self._clock()) for _, expires_at in claimed):
            raise MemoryExecutionUnavailableError
        inputs = tuple(engine_input for engine_input, _expires_at in claimed)
        try:
            execution = await self._engine_service.create_attempt(
                team_id=team_id,
                analysis_id=analysis_id,
                engine_id="android_memory",
                tenant_resource_version=capture.tenant_resource_version,
                input_manifest_hash=capture.manifest.sha256_hex(),
                config_hash=canonical_memory_config_hash(
                    capture_id=capture_id,
                    question=capture.question,
                    timeout_seconds=self._timeout_seconds,
                ),
            )
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MemoryExecutionUnavailableError from None
        return PreparedMemoryExecution(
            execution=execution,
            inputs=inputs,
            question=capture.question,
        )


__all__ = [
    "LoadedMemoryCapture",
    "MemoryExecutionArtifact",
    "MemoryExecutionError",
    "MemoryExecutionNotFoundError",
    "MemoryExecutionRepository",
    "MemoryExecutionService",
    "MemoryExecutionUnavailableError",
    "PreparedMemoryExecution",
    "SQLAlchemyMemoryExecutionRepository",
    "canonical_memory_config_hash",
]
