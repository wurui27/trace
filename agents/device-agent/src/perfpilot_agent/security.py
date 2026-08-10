from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
    TypeAdapter,
)

_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_KID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAXIMUM_COMPACT_BYTES = 32 * 1024
_MAXIMUM_LIFETIME = timedelta(seconds=90)
_MAXIMUM_ISSUED_AT_SKEW = timedelta(seconds=5)
Sha256Base64 = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")]
HexDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class TaskRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent task was rejected")


class TaskInputArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    kind: Literal["apk", "scenario_fixture", "dataset"]
    mime: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$",
    )
    size: int = Field(strict=True, ge=1, le=5 * 1024 * 1024 * 1024)
    sha256_b64: Sha256Base64


class TaskScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_type: Literal["startup", "scroll", "memory_cycle"]
    recipe_version: int = Field(strict=True, ge=1)
    recipe_hash: HexDigest
    duration_seconds: int = Field(strict=True, ge=0, le=300)
    memory_rounds: int = Field(strict=True, ge=0, le=20)
    swipe_count: int = Field(strict=True, ge=0, le=120)


class TaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    aud: Literal["perfpilot-agent"]
    agent_id: UUID
    device_digest: HexDigest
    execution_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    analysis_id: UUID
    issued_at: datetime
    expires_at: datetime
    package_name: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
    )
    launch_activity: str = Field(
        min_length=3,
        max_length=512,
        pattern=r"^[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+$",
    )
    cleanup_policy: Literal["keep_installed", "uninstall"]
    input_artifacts: tuple[TaskInputArtifact, ...] = Field(min_length=1, max_length=8)
    scenarios: tuple[TaskScenario, ...] = Field(min_length=1, max_length=3)
    allowed_uploads: tuple[
        Literal["startup_trace", "scroll_trace", "memory_evidence", "agent_log"],
        ...,
    ] = Field(min_length=1, max_length=8)

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def require_string_timestamp(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("task timestamp must be an ISO 8601 string")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_unique_items(self) -> Self:
        if len({item.artifact_id for item in self.input_artifacts}) != len(self.input_artifacts):
            raise ValueError("task input artifacts must be unique")
        if len({item.scenario_type for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("task scenarios must be unique")
        if len(set(self.allowed_uploads)) != len(self.allowed_uploads):
            raise ValueError("task uploads must be unique")
        return self


VerifiedCaptureTask = TaskSnapshot


class SourceFindingHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: UUID
    evidence_ids: tuple[UUID, ...] = Field(max_length=20)
    rule_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.]*$")
    symbol_hints: tuple[
        Annotated[
            str,
            StringConstraints(
                min_length=1,
                max_length=255,
                pattern=r"^[^\x00-\x1f\x7f]+$",
            ),
        ],
        ...,
    ] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_unique_hints(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or len(
            set(self.symbol_hints)
        ) != len(self.symbol_hints):
            raise ValueError("source finding hints must be unique")
        return self


class SourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_findings: Literal[3]
    max_files: Literal[12]
    max_bytes: Literal[98_304]


class VerifiedSourceTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    aud: Literal["perfpilot-agent"]
    task_type: str
    execution_id: UUID
    analysis_id: UUID
    team_id: UUID
    agent_id: UUID
    workspace_id: UUID
    snapshot_policy: Literal["tracked_worktree"]
    validation_profile_id: UUID | None
    lease_version: int = Field(strict=True, ge=1)
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def require_string_expiry(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("task timestamp must be an ISO 8601 string")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task timestamp must include a timezone")
        return value.astimezone(UTC)


class VerifiedSourceContextTask(VerifiedSourceTask):
    task_type: Literal["source_context"]
    finding_hints: tuple[SourceFindingHint, ...] = Field(max_length=3)
    limits: SourceLimits


class VerifiedPatchVerificationTask(VerifiedSourceTask):
    task_type: Literal["patch_verification"]
    validation_profile_id: UUID
    snapshot_id: UUID
    snapshot_hash: HexDigest
    fix_id: UUID
    patch: str = Field(min_length=1, max_length=65_536, repr=False)

    @field_validator("patch")
    @classmethod
    def validate_patch_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 65_536:
            raise ValueError("patch exceeds byte limit")
        return value


_SOURCE_TASK_ADAPTER = TypeAdapter(
    Annotated[
        VerifiedSourceContextTask | VerifiedPatchVerificationTask,
        Field(discriminator="task_type"),
    ]
)


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(value: str, *, maximum_bytes: int) -> bytes:
    if not value or len(value) > maximum_bytes * 2 or _SEGMENT_PATTERN.fullmatch(value) is None:
        raise TaskRejected
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise TaskRejected from None
    if len(decoded) > maximum_bytes or _encode_segment(decoded) != value:
        raise TaskRejected
    return decoded


def _closed_json(payload: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise TaskRejected
            result[key] = value
        return result

    try:
        parsed = json.loads(payload, object_pairs_hook=pairs)
    except (TaskRejected, UnicodeError, json.JSONDecodeError):
        raise TaskRejected from None
    if not isinstance(parsed, dict):
        raise TaskRejected
    return parsed


def _public_key(value: str) -> Ed25519PublicKey:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise TaskRejected from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise TaskRejected
    try:
        return Ed25519PublicKey.from_public_bytes(decoded)
    except ValueError:
        raise TaskRejected from None


class TaskVerifier:
    def __init__(
        self,
        *,
        public_key_b64: str,
        kid: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if _KID_PATTERN.fullmatch(kid) is None:
            raise ValueError("task verifier key identifier is invalid")
        self._public_key = _public_key(public_key_b64)
        self._kid = kid
        self._clock = clock

    def verify(
        self,
        compact: str,
        *,
        expected_agent_id: UUID,
        expected_lease_version: int | None,
        known_device_digests: Collection[str],
    ) -> TaskSnapshot:
        try:
            if (
                not isinstance(compact, str)
                or len(compact.encode("ascii")) > _MAXIMUM_COMPACT_BYTES
            ):
                raise TaskRejected
            protected_segment, payload_segment, signature_segment = compact.split(".")
            protected = _closed_json(_decode_segment(protected_segment, maximum_bytes=1_024))
            if (
                set(protected) != {"alg", "kid", "typ"}
                or protected.get("alg") != "EdDSA"
                or protected.get("kid") != self._kid
                or protected.get("typ") != "perfpilot-task+jws"
            ):
                raise TaskRejected
            payload = _decode_segment(payload_segment, maximum_bytes=_MAXIMUM_COMPACT_BYTES)
            signature = _decode_segment(signature_segment, maximum_bytes=64)
            if len(signature) != 64:
                raise TaskRejected
            self._public_key.verify(
                signature,
                f"{protected_segment}.{payload_segment}".encode("ascii"),
            )
            task = TaskSnapshot.model_validate(_closed_json(payload))
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise TaskRejected
            normalized_now = now.astimezone(UTC)
            lifetime = task.expires_at - task.issued_at
            if (
                lifetime <= timedelta(0)
                or lifetime > _MAXIMUM_LIFETIME
                or task.issued_at > normalized_now + _MAXIMUM_ISSUED_AT_SKEW
                or task.expires_at <= normalized_now
                or task.agent_id != expected_agent_id
                or (
                    expected_lease_version is not None
                    and task.lease_version != expected_lease_version
                )
                or task.device_digest not in known_device_digests
            ):
                raise TaskRejected
            return task
        except TaskRejected:
            raise
        except (
            InvalidSignature,
            ValidationError,
            UnicodeError,
            ValueError,
            TypeError,
        ):
            raise TaskRejected from None

    def verify_source(
        self,
        snapshot: object,
        signature_b64: str,
        *,
        expected_agent_id: UUID,
        expected_team_id: UUID,
    ) -> VerifiedSourceTask:
        try:
            if not isinstance(snapshot, dict):
                raise TaskRejected
            canonical = json.dumps(
                snapshot,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            signature = base64.b64decode(signature_b64, validate=True)
            if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != signature_b64:
                raise TaskRejected
            self._public_key.verify(signature, canonical)
            task = _SOURCE_TASK_ADAPTER.validate_python(snapshot)
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise TaskRejected
            remaining = task.expires_at - now.astimezone(UTC)
            if (
                remaining <= timedelta(0)
                or remaining > _MAXIMUM_LIFETIME
                or task.agent_id != expected_agent_id
                or task.team_id != expected_team_id
            ):
                raise TaskRejected
            return task
        except TaskRejected:
            raise
        except (
            InvalidSignature,
            ValidationError,
            UnicodeError,
            ValueError,
            TypeError,
            binascii.Error,
        ):
            raise TaskRejected from None


__all__ = [
    "TaskInputArtifact",
    "TaskRejected",
    "TaskScenario",
    "TaskSnapshot",
    "TaskVerifier",
    "VerifiedCaptureTask",
    "VerifiedPatchVerificationTask",
    "VerifiedSourceContextTask",
    "VerifiedSourceTask",
]
