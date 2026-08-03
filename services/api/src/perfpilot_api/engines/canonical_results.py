"""Pure validation and deterministic serialization for external-engine results."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote
from uuid import UUID, uuid5

from perfpilot_api.engines.android_memory_contracts import AndroidMemoryContext
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.engines.smartperfetto_contracts import (
    validate_sanitized_report_payload,
)


_RESULT_NAMESPACE = UUID("a1c50ce0-6144-553e-8721-18f466991f32")
_ENGINE_CONTRACTS = {
    "smartperfetto": "workspace-agent-v1",
    "android_memory": "android-memory-ai-context-1.2",
}
_TERMINAL_RESULT_STATES = frozenset({"completed", "insufficient_data"})
_ADAPTER_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_MAX_DEPTH = 64
_MAX_NODES = 200_000
_MAX_COLLECTION_ITEMS = 50_000
_MAX_KEY_CHARS = 1_024
_MAX_STRING_BYTES = 1024 * 1024
_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
_MAX_PRIVACY_DECODE_PASSES = 8

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_TRAVERSAL = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_SIGNED_HTTP_URL = re.compile(
    r"https?://[^\s]+[?&](?:x-amz-signature|x-goog-signature|x-amz-credential|"
    r"x-goog-credential|signature|sig|token)=",
    re.IGNORECASE,
)
_DATABASE_URL = re.compile(
    r"(?<![a-z0-9+.-])(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|"
    r"redis(?:s)?|sqlite|sqlserver|oracle)(?:\+[a-z0-9_.-]+)?://\S+",
    re.IGNORECASE,
)
_CREDENTIAL_URL = re.compile(
    r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@\S+",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_BASIC_CREDENTIAL = re.compile(
    r"\bbasic\s+([a-z0-9+/]{4,}={0,2})(?=$|[\s,;])",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![a-z0-9])[\"']?(?:authorization|api[ _-]?key|access[ _-]?token|"
    r"access[ _-]?key|aws[ _-]?access[ _-]?key[ _-]?id|"
    r"aws[ _-]?secret[ _-]?access[ _-]?key|client[ _-]?secret|credential|"
    r"private[ _-]?key|refresh[ _-]?token|secret[ _-]?access[ _-]?key|"
    r"session[ _-]?token|token|secret(?:[ _-]?key)?|password|passwd)"
    r"[\"']?\s*[:=]\s*[\"']?\S+",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE,
)
_OBJECT_STORE_URI = re.compile(r"(?:s3|gs|az|r2)://\S+", re.IGNORECASE)
_OBJECT_KEY_SECRET = re.compile(
    r"^object[\s_-]*key(?:\s*[:=]\s*\S.*)?$",
    re.IGNORECASE,
)
_SENSITIVE_KEYS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authorization",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "bucket",
        "bucketname",
        "clientsecret",
        "credential",
        "credentials",
        "downloadurl",
        "externalerror",
        "objectkey",
        "password",
        "passwd",
        "presignedurl",
        "privatekey",
        "refreshtoken",
        "reporterror",
        "secret",
        "secretaccesskey",
        "secretkey",
        "sessiontoken",
        "signedurl",
        "storageversionid",
        "teamid",
        "token",
        "uploadurl",
        "versionid",
        "xamzcredential",
        "xgoogcredential",
    }
)


class EngineResultValidationError(ValueError):
    """A redacted canonical-result validation failure."""

    def __init__(self, _detail: object = None) -> None:
        super().__init__("engine result is invalid")


@dataclass(frozen=True, slots=True)
class EngineResultWrite:
    team_id: UUID
    analysis_id: UUID
    execution_id: UUID
    expected_execution_version: int
    tenant_resource_version: int
    artifact_id: UUID
    engine_id: Literal["smartperfetto", "android_memory"]
    adapter_version: str
    engine_commit_sha: str
    engine_image_digest: str
    attempt_number: int
    input_manifest_hash: str
    config_hash: str
    result: EngineResult = field(repr=False)


@dataclass(frozen=True, slots=True)
class CanonicalEngineResult:
    document: dict[str, object] = field(repr=False)
    canonical_bytes: bytes = field(repr=False)
    payload_sha256_hex: str
    request_hash_hex: str
    checksum_sha256_b64: str = field(repr=False)


@dataclass(slots=True)
class _TraversalState:
    nodes: int = 0
    active_collections: set[int] = field(default_factory=set)


def result_artifact_id(execution_id: UUID) -> UUID:
    """Derive the sole result Artifact identity for an engine execution."""

    return uuid5(_RESULT_NAMESPACE, str(execution_id))


def _invalid() -> EngineResultValidationError:
    return EngineResultValidationError()


def _normalize_key(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key)
    return _NON_ALPHANUMERIC.sub("", normalized.casefold())


def _privacy_variants(value: str) -> tuple[str, ...]:
    current = unicodedata.normalize("NFKC", value.strip())
    variants = [current]
    for _ in range(_MAX_PRIVACY_DECODE_PASSES):
        if "%" not in current:
            return tuple(variants)
        decoded = unquote(current, encoding="utf-8", errors="strict")
        decoded = unicodedata.normalize("NFKC", decoded.strip())
        if decoded == current:
            return tuple(variants)
        variants.append(decoded)
        current = decoded
    if "%" in current:
        decoded = unquote(current, encoding="utf-8", errors="strict")
        decoded = unicodedata.normalize("NFKC", decoded.strip())
        if decoded != current:
            raise _invalid()
    return tuple(variants)


def _contains_basic_credential(value: str) -> bool:
    for match in _BASIC_CREDENTIAL.finditer(value):
        encoded = match.group(1)
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True)
        except ValueError:
            continue
        if b":" in decoded:
            return True
    return False


def _is_sensitive_key(value: str) -> bool:
    return any(
        _normalize_key(variant) in _SENSITIVE_KEYS
        for variant in _privacy_variants(value)
    )


def _is_private_string(value: str) -> bool:
    for variant in _privacy_variants(value):
        folded = variant.casefold()
        if "file://" in folded:
            return True
        if variant.startswith(("/", "\\")):
            return True
        if _WINDOWS_DRIVE_PATH.match(variant) is not None:
            return True
        if _PATH_TRAVERSAL.search(variant) is not None:
            return True
        if _SIGNED_HTTP_URL.search(variant) is not None:
            return True
        if _OBJECT_STORE_URI.search(variant) is not None:
            return True
        if _DATABASE_URL.search(variant) is not None:
            return True
        if _CREDENTIAL_URL.search(variant) is not None:
            return True
        if _BEARER_CREDENTIAL.search(variant) is not None:
            return True
        if _contains_basic_credential(variant):
            return True
        if _CREDENTIAL_ASSIGNMENT.search(variant) is not None:
            return True
        if _PRIVATE_KEY_BLOCK.search(variant) is not None:
            return True
        if _OBJECT_KEY_SECRET.fullmatch(variant) is not None:
            return True
    return False


def _visit_node(state: _TraversalState, *, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise _invalid()
    state.nodes += 1
    if state.nodes > _MAX_NODES:
        raise _invalid()


def _visit_key(state: _TraversalState, key: object) -> str:
    state.nodes += 1
    if state.nodes > _MAX_NODES or not isinstance(key, str):
        raise _invalid()
    if len(key) > _MAX_KEY_CHARS or _is_sensitive_key(key):
        raise _invalid()
    try:
        encoded = key.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None
    if len(encoded) > _MAX_STRING_BYTES or _is_private_string(key):
        raise _invalid()
    return key


def _copy_json_value(
    value: object,
    *,
    depth: int,
    state: _TraversalState,
) -> object:
    _visit_node(state, depth=depth)
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid()
        return value
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise _invalid() from None
        if len(encoded) > _MAX_STRING_BYTES or _is_private_string(value):
            raise _invalid()
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise _invalid()
        identity = id(value)
        if identity in state.active_collections:
            raise _invalid()
        state.active_collections.add(identity)
        try:
            copied: dict[str, object] = {}
            for key, nested in value.items():
                copied[_visit_key(state, key)] = _copy_json_value(
                    nested,
                    depth=depth + 1,
                    state=state,
                )
            return copied
        finally:
            state.active_collections.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise _invalid()
        identity = id(value)
        if identity in state.active_collections:
            raise _invalid()
        state.active_collections.add(identity)
        try:
            return [
                _copy_json_value(nested, depth=depth + 1, state=state)
                for nested in value
            ]
        finally:
            state.active_collections.remove(identity)
    raise _invalid()


def _copy_payload(payload: object) -> dict[str, object]:
    copied = _copy_json_value(payload, depth=0, state=_TraversalState())
    if not isinstance(copied, dict):
        raise _invalid()
    return copied


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_request(request: EngineResultWrite) -> None:
    if not isinstance(request, EngineResultWrite):
        raise _invalid()
    if any(
        type(value) is not UUID
        for value in (
            request.team_id,
            request.analysis_id,
            request.execution_id,
            request.artifact_id,
        )
    ):
        raise _invalid()
    if request.artifact_id != result_artifact_id(request.execution_id):
        raise _invalid()
    if not _is_positive_int(request.expected_execution_version):
        raise _invalid()
    if not _is_positive_int(request.tenant_resource_version):
        raise _invalid()
    if not _is_positive_int(request.attempt_number):
        raise _invalid()
    expected_contract = _ENGINE_CONTRACTS.get(request.engine_id)
    if expected_contract is None or not isinstance(request.result, EngineResult):
        raise _invalid()
    if request.result.contract != expected_contract:
        raise _invalid()
    if request.result.state not in _TERMINAL_RESULT_STATES:
        raise _invalid()
    if (
        not isinstance(request.adapter_version, str)
        or not 1 <= len(request.adapter_version) <= 32
        or _ADAPTER_VERSION.fullmatch(request.adapter_version) is None
    ):
        raise _invalid()
    if (
        not isinstance(request.engine_commit_sha, str)
        or _COMMIT_SHA.fullmatch(request.engine_commit_sha) is None
    ):
        raise _invalid()
    if (
        not isinstance(request.engine_image_digest, str)
        or _IMAGE_DIGEST.fullmatch(request.engine_image_digest) is None
    ):
        raise _invalid()
    for value in (request.input_manifest_hash, request.config_hash):
        if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
            raise _invalid()


def _validate_android_payload(payload: dict[str, object]) -> dict[str, object]:
    model = AndroidMemoryContext.model_validate(payload, strict=True)
    validated = model.model_dump(mode="json")
    contract = validated.get("analysis_contract")
    if not isinstance(contract, dict):
        raise _invalid()
    privacy = contract.get("privacy")
    if not isinstance(privacy, dict):
        raise _invalid()
    if privacy.get("raw_contents_embedded") is not False:
        raise _invalid()
    if privacy.get("local_paths_included") is not False:
        raise _invalid()
    return validated


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def canonicalize_engine_result(request: EngineResultWrite) -> CanonicalEngineResult:
    """Validate, defensively copy, and serialize an engine result without I/O."""

    try:
        _validate_request(request)
        copied_payload = _copy_payload(request.result.payload)
        if request.engine_id == "smartperfetto":
            validated_payload = validate_sanitized_report_payload(copied_payload)
        else:
            validated_payload = _validate_android_payload(copied_payload)
        payload = _copy_payload(validated_payload)
        payload_bytes = _canonical_json(payload)
        payload_sha256_hex = hashlib.sha256(payload_bytes).hexdigest()
        document: dict[str, object] = {
            "schema_version": "1.0",
            "result_type": "canonical-engine-result",
            "artifact_id": str(request.artifact_id),
            "analysis_id": str(request.analysis_id),
            "execution_id": str(request.execution_id),
            "tenant_resource_version": request.tenant_resource_version,
            "engine": {
                "engine_id": request.engine_id,
                "adapter_version": request.adapter_version,
                "source_contract": request.result.contract,
                "source_commit_sha": request.engine_commit_sha,
                "image_digest": request.engine_image_digest,
            },
            "attempt": {
                "number": request.attempt_number,
                "input_manifest_hash": request.input_manifest_hash,
                "config_hash": request.config_hash,
            },
            "result": {
                "state": request.result.state,
                "payload_sha256": payload_sha256_hex,
                "payload": payload,
            },
        }
        canonical_bytes = _canonical_json(document)
        if len(canonical_bytes) > _MAX_ENVELOPE_BYTES:
            raise _invalid()
        envelope_digest = hashlib.sha256(canonical_bytes)
        return CanonicalEngineResult(
            document=document,
            canonical_bytes=canonical_bytes,
            payload_sha256_hex=payload_sha256_hex,
            request_hash_hex=envelope_digest.hexdigest(),
            checksum_sha256_b64=base64.b64encode(envelope_digest.digest()).decode("ascii"),
        )
    except EngineResultValidationError:
        raise
    except Exception:
        raise EngineResultValidationError() from None


__all__ = [
    "CanonicalEngineResult",
    "EngineResultValidationError",
    "EngineResultWrite",
    "canonicalize_engine_result",
    "result_artifact_id",
]
