from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from uuid import UUID
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from perfpilot_api.security.agent_signatures import (
    AgentProofRejected,
    decode_ed25519_public_key,
)

_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_KID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAXIMUM_COMPACT_BYTES = 32_768
_MAXIMUM_LIFETIME = timedelta(seconds=90)
_MAXIMUM_ISSUED_AT_SKEW = timedelta(seconds=5)
_CONTRACT = (
    Path(__file__).resolve().parents[5]
    / "contracts"
    / "v1"
    / "agents"
    / "task-snapshot.schema.json"
)
_SOURCE_CONTRACT = (
    Path(__file__).resolve().parents[5]
    / "contracts"
    / "v1"
    / "agents"
    / "source-task-snapshot.schema.json"
)


class TaskSnapshotRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Task snapshot was rejected")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise TaskSnapshotRejected from None


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(value: str, *, maximum_bytes: int) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_bytes * 2
        or _SEGMENT_PATTERN.fullmatch(value) is None
    ):
        raise TaskSnapshotRejected
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise TaskSnapshotRejected from None
    if len(decoded) > maximum_bytes or _encode_segment(decoded) != value:
        raise TaskSnapshotRejected
    return decoded


def _closed_json(value: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise TaskSnapshotRejected
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=pairs)
    except (TaskSnapshotRejected, UnicodeError, json.JSONDecodeError):
        raise TaskSnapshotRejected from None
    if not isinstance(decoded, dict):
        raise TaskSnapshotRejected
    return decoded


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(_CONTRACT.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        raise TaskSnapshotRejected from None
    return Draft202012Validator(schema, format_checker=FormatChecker())


@lru_cache(maxsize=1)
def _source_validator() -> Draft202012Validator:
    try:
        schema = json.loads(_SOURCE_CONTRACT.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        raise TaskSnapshotRejected from None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TaskSnapshotRejected
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TaskSnapshotRejected from None
    if parsed.tzinfo is None:
        raise TaskSnapshotRejected
    return parsed.astimezone(UTC)


def _validate_claims(claims: dict[str, object], *, now: datetime) -> None:
    if now.tzinfo is None:
        raise TaskSnapshotRejected
    try:
        _validator().validate(claims)
    except ValidationError:
        raise TaskSnapshotRejected from None
    issued_at = _parse_timestamp(claims.get("issued_at"))
    expires_at = _parse_timestamp(claims.get("expires_at"))
    lifetime = expires_at - issued_at
    normalized_now = now.astimezone(UTC)
    if (
        lifetime <= timedelta(0)
        or lifetime > _MAXIMUM_LIFETIME
        or issued_at > normalized_now + _MAXIMUM_ISSUED_AT_SKEW
        or expires_at <= normalized_now
    ):
        raise TaskSnapshotRejected


def validate_source_task_snapshot(
    snapshot: dict[str, object], *, now: datetime
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise TaskSnapshotRejected
    try:
        _source_validator().validate(snapshot)
    except ValidationError:
        raise TaskSnapshotRejected from None
    expires_at = _parse_timestamp(snapshot.get("expires_at"))
    remaining = expires_at - now.astimezone(UTC)
    if remaining <= timedelta(0) or remaining > _MAXIMUM_LIFETIME:
        raise TaskSnapshotRejected


class TaskSnapshotSigner:
    __slots__ = ("_clock", "_kid", "_private_key")

    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey,
        kid: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey) or _KID_PATTERN.fullmatch(kid) is None:
            raise ValueError("Task snapshot signer configuration is invalid")
        self._private_key = private_key
        self._kid = kid
        self._clock = clock

    def __repr__(self) -> str:
        return f"TaskSnapshotSigner(kid={self._kid!r})"

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def sign(self, claims: dict[str, object]) -> str:
        _validate_claims(claims, now=self._clock())
        protected = _encode_segment(
            _canonical_json(
                {
                    "alg": "EdDSA",
                    "kid": self._kid,
                    "typ": "perfpilot-task+jws",
                }
            )
        )
        payload = _encode_segment(_canonical_json(claims))
        signing_input = f"{protected}.{payload}".encode("ascii")
        compact = f"{protected}.{payload}.{_encode_segment(self._private_key.sign(signing_input))}"
        if len(compact.encode("ascii")) > _MAXIMUM_COMPACT_BYTES:
            raise TaskSnapshotRejected
        return compact


class SourceTaskSnapshotSigner:
    """Signs a closed source-task snapshot without wrapping or logging its body."""

    __slots__ = ("_clock", "_kid", "_private_key")

    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey,
        kid: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey) or _KID_PATTERN.fullmatch(kid) is None:
            raise ValueError("Source task signer configuration is invalid")
        self._private_key = private_key
        self._kid = kid
        self._clock = clock

    def __repr__(self) -> str:
        return f"SourceTaskSnapshotSigner(kid={self._kid!r})"

    def sign(self, snapshot: dict[str, object]) -> str:
        now = self._clock()
        validate_source_task_snapshot(snapshot, now=now)
        canonical = _canonical_json(snapshot)
        return base64.b64encode(self._private_key.sign(canonical)).decode("ascii")


def verify_task_jws(
    compact: str,
    public_key_b64: str,
    *,
    now: datetime | None = None,
    expected_kid: str | None = None,
    expected_team_id: UUID | None = None,
) -> dict[str, object]:
    try:
        if not isinstance(compact, str) or len(compact.encode("ascii")) > _MAXIMUM_COMPACT_BYTES:
            raise TaskSnapshotRejected
        protected_segment, payload_segment, signature_segment = compact.split(".")
        protected = _closed_json(_decode_segment(protected_segment, maximum_bytes=1_024))
        if (
            set(protected) != {"alg", "kid", "typ"}
            or protected.get("alg") != "EdDSA"
            or protected.get("typ") != "perfpilot-task+jws"
            or not isinstance(protected.get("kid"), str)
            or _KID_PATTERN.fullmatch(str(protected["kid"])) is None
            or (expected_kid is not None and protected["kid"] != expected_kid)
        ):
            raise TaskSnapshotRejected
        payload = _decode_segment(payload_segment, maximum_bytes=_MAXIMUM_COMPACT_BYTES)
        signature = _decode_segment(signature_segment, maximum_bytes=64)
        if len(signature) != 64:
            raise TaskSnapshotRejected
        public_key = decode_ed25519_public_key(public_key_b64)
        public_key.verify(
            signature,
            f"{protected_segment}.{payload_segment}".encode("ascii"),
        )
        claims = _closed_json(payload)
        _validate_claims(claims, now=now or datetime.now(UTC))
        schema_version = claims.get("schema_version")
        if schema_version in {"1.1", "1.2"} and (
            expected_team_id is None or claims.get("team_id") != str(expected_team_id)
        ):
            raise TaskSnapshotRejected
        return claims
    except (
        AgentProofRejected,
        InvalidSignature,
        TaskSnapshotRejected,
        UnicodeError,
        ValueError,
    ):
        raise TaskSnapshotRejected from None


def snapshot_digest(compact: str) -> str:
    if not isinstance(compact, str):
        raise TaskSnapshotRejected
    try:
        encoded = compact.encode("ascii")
    except UnicodeError:
        raise TaskSnapshotRejected from None
    if not encoded or len(encoded) > _MAXIMUM_COMPACT_BYTES:
        raise TaskSnapshotRejected
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "SourceTaskSnapshotSigner",
    "TaskSnapshotRejected",
    "TaskSnapshotSigner",
    "snapshot_digest",
    "validate_source_task_snapshot",
    "verify_task_jws",
]
