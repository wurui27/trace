"""Read one finalized canonical engine result from its immutable S3 version."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.services.engine_result_artifacts import (
    EngineResultArtifactRecord,
    EngineResultArtifactRepository,
)
from perfpilot_api.services.uploads import BucketResolver, TenantBucket


_JSON_MIME = "application/json"
_T = TypeVar("_T")


class CanonicalResultError(RuntimeError):
    """Stable error boundary for private canonical result reads."""


class CanonicalResultUnavailableError(CanonicalResultError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("canonical result is unavailable")


class CanonicalResultIntegrityError(CanonicalResultError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("canonical result integrity failure")


@dataclass(frozen=True, slots=True)
class LoadedCanonicalResult:
    team_id: UUID
    analysis_id: UUID
    execution_id: UUID
    artifact_id: UUID
    tenant_resource_version: int
    sha256_b64: str = field(repr=False)
    document: dict[str, object] = field(repr=False)
    canonical_bytes: bytes = field(repr=False)


class _Execution(Protocol):
    id: UUID
    team_id: UUID
    analysis_id: UUID
    raw_result_artifact_id: UUID | None
    tenant_resource_version: int
    engine_id: str
    state: str
    adapter_version: str
    engine_commit_sha: str
    engine_image_digest: str
    attempt_number: int


def _safe_version_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value == "null"
        or len(value) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


@lru_cache
def _validator() -> Draft202012Validator:
    root = Path(__file__).resolve().parents[5] / "contracts" / "v1"
    try:
        schema = json.loads(
            (root / "engines/canonical-engine-result.schema.json").read_text("utf-8")
        )
    except (OSError, UnicodeError, ValueError):
        raise CanonicalResultIntegrityError from None
    return Draft202012Validator(schema, format_checker=FormatChecker())


class CanonicalResultReader:
    """Fence the tenant route, row, and S3 object to one exact result version."""

    def __init__(
        self,
        *,
        artifact_repository: EngineResultArtifactRepository,
        bucket_resolver: BucketResolver,
        client: Any,
    ) -> None:
        self._artifact_repository = artifact_repository
        self._bucket_resolver = bucket_resolver
        self._client = client

    @staticmethod
    async def _dependency(operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except CanonicalResultError:
            raise
        except Exception:
            raise CanonicalResultUnavailableError from None

    @staticmethod
    def _execution_values(execution: object) -> tuple[UUID, UUID, UUID, UUID, int]:
        fields = (
            getattr(execution, "team_id", None),
            getattr(execution, "analysis_id", None),
            getattr(execution, "id", None),
            getattr(execution, "raw_result_artifact_id", None),
            getattr(execution, "tenant_resource_version", None),
        )
        team_id, analysis_id, execution_id, artifact_id, resource_version = fields
        if (
            not all(isinstance(value, UUID) for value in fields[:4])
            or type(resource_version) is not int
            or resource_version < 1
            or getattr(execution, "engine_id", None) != "smartperfetto"
            or getattr(execution, "state", None) not in {"completed", "insufficient_data"}
        ):
            raise CanonicalResultIntegrityError
        return team_id, analysis_id, execution_id, artifact_id, resource_version  # type: ignore[return-value]

    @staticmethod
    def _record_matches(
        record: object,
        *,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
    ) -> EngineResultArtifactRecord:
        if not isinstance(record, EngineResultArtifactRecord):
            raise CanonicalResultIntegrityError
        version_id = _safe_version_id(record.version_id)
        expected_key = f"raw/analyses/{analysis_id}/internal/engine-results/{artifact_id}.json"
        if (
            record.artifact_id != artifact_id
            or record.analysis_id != analysis_id
            or record.upload_id != artifact_id
            or record.idempotency_key != f"internal:engine_result:{execution_id}"
            or record.artifact_kind != "engine_result"
            or record.mime_type != _JSON_MIME
            or type(record.size_bytes) is not int
            or record.size_bytes < 1
            or not isinstance(record.sha256_b64, str)
            or len(record.sha256_b64) != 44
            or not hmac.compare_digest(record.object_key, expected_key)
            or record.state != "finalized"
            or record.version != 2
            or version_id is None
        ):
            raise CanonicalResultIntegrityError
        return record

    @staticmethod
    def _read_sync(
        client: Any,
        *,
        tenant: TenantBucket,
        record: EngineResultArtifactRecord,
    ) -> bytes:
        version_id = _safe_version_id(record.version_id)
        if version_id is None:
            raise CanonicalResultIntegrityError
        try:
            response = client.get_object(
                Bucket=tenant.bucket,
                Key=record.object_key,
                VersionId=version_id,
                ChecksumMode="ENABLED",
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CanonicalResultUnavailableError from None
        if not isinstance(response, Mapping):
            raise CanonicalResultUnavailableError
        body = response.get("Body")
        close = getattr(body, "close", None)
        read = getattr(body, "read", None)
        if not callable(close) or not callable(read):
            raise CanonicalResultUnavailableError
        try:
            if (
                response.get("VersionId") != version_id
                or response.get("ContentType") != _JSON_MIME
                or response.get("ContentLength") != record.size_bytes
                or response.get("DeleteMarker", False) is not False
                or not isinstance(response.get("ChecksumSHA256"), str)
                or not hmac.compare_digest(response["ChecksumSHA256"], record.sha256_b64)
            ):
                raise CanonicalResultIntegrityError
            payload = read(record.size_bytes + 1)
        except CanonicalResultError:
            raise
        except Exception:
            raise CanonicalResultUnavailableError from None
        finally:
            try:
                close()
            except Exception:
                raise CanonicalResultUnavailableError from None
        if not isinstance(payload, bytes) or len(payload) != record.size_bytes:
            raise CanonicalResultIntegrityError
        checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        if not hmac.compare_digest(checksum, record.sha256_b64):
            raise CanonicalResultIntegrityError
        return payload

    @staticmethod
    def _parse(
        payload: bytes,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        tenant_resource_version: int,
        execution: _Execution,
    ) -> dict[str, object]:
        try:
            document = json.loads(payload)
            if not isinstance(document, dict):
                raise ValueError
            _validator().validate(document)
            if canonical_json_bytes(document) != payload:
                raise ValueError
            result = document["result"]
            engine = document["engine"]
            attempt = document["attempt"]
            if not isinstance(result, Mapping) or not isinstance(engine, Mapping) or not isinstance(attempt, Mapping):
                raise ValueError
            payload_hash = hashlib.sha256(canonical_json_bytes(result["payload"])).hexdigest()
            if (
                document["analysis_id"] != str(analysis_id)
                or document["execution_id"] != str(execution_id)
                or document["artifact_id"] != str(artifact_id)
                or document["tenant_resource_version"] != tenant_resource_version
                or engine.get("engine_id") != execution.engine_id
                or engine.get("adapter_version") != execution.adapter_version
                or engine.get("source_commit_sha") != execution.engine_commit_sha
                or engine.get("image_digest") != execution.engine_image_digest
                or attempt.get("number") != execution.attempt_number
                or result.get("state") != execution.state
                or result.get("payload_sha256") != payload_hash
            ):
                raise ValueError
            return document
        except CanonicalResultError:
            raise
        except Exception:
            raise CanonicalResultIntegrityError from None

    async def read(self, execution: _Execution) -> LoadedCanonicalResult:
        team_id, analysis_id, execution_id, artifact_id, resource_version = self._execution_values(
            execution
        )
        tenant = await self._dependency(self._bucket_resolver.active_for_team(team_id))
        if (
            not isinstance(tenant, TenantBucket)
            or tenant.team_id != team_id
            or tenant.resource_version != resource_version
            or not tenant.bucket
        ):
            raise CanonicalResultUnavailableError
        await self._dependency(self._artifact_repository.require_resource_version(tenant))
        record = await self._dependency(
            self._artifact_repository.reload(
                tenant=tenant,
                analysis_id=analysis_id,
                artifact_id=artifact_id,
            )
        )
        record = self._record_matches(
            record,
            analysis_id=analysis_id,
            execution_id=execution_id,
            artifact_id=artifact_id,
        )
        payload = await self._dependency(
            asyncio.to_thread(self._read_sync, self._client, tenant=tenant, record=record)
        )
        await self._dependency(self._artifact_repository.require_resource_version(tenant))
        document = self._parse(
            payload,
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
            artifact_id=artifact_id,
            tenant_resource_version=resource_version,
            execution=execution,
        )
        return LoadedCanonicalResult(
            team_id=team_id,
            analysis_id=analysis_id,
            execution_id=execution_id,
            artifact_id=artifact_id,
            tenant_resource_version=resource_version,
            sha256_b64=record.sha256_b64,
            document=document,
            canonical_bytes=payload,
        )


__all__ = [
    "CanonicalResultIntegrityError",
    "CanonicalResultReader",
    "CanonicalResultUnavailableError",
    "LoadedCanonicalResult",
]
