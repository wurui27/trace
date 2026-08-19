from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.credentials import (
    AgentCredentials,
    CredentialStore,
    TaskSigningKey,
)
from perfpilot_agent.platform.base import AgentPlatform
from perfpilot_agent.security import (
    TaskSnapshot,
    VerifiedPatchVerificationTask,
    VerifiedSourceContextTask,
)

_MAXIMUM_RESPONSE_BYTES = 128 * 1024
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_AGENT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_ACCESS_TOKEN = re.compile(r"^ppat_[A-Za-z0-9_-]{43}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_MIME_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_RETRYABLE_STATUSES = frozenset({408, 425, 429})
_MAXIMUM_ATTEMPTS = 4


class ControlClientError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__("PerfPilot Agent control request failed")
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class LeaseLost(ControlClientError):
    def __init__(self) -> None:
        super().__init__(code="lease_lost", retryable=False)


def _validate_raw_public_key(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("public key is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("public key is invalid")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _require_string_timestamp(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    return value


def _validate_signed_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError("signed URL is invalid") from None
    hostname = parsed.hostname
    private_hostname = hostname in {"localhost", "127.0.0.1", "::1"}
    if hostname is not None and not private_hostname:
        try:
            private_hostname = ipaddress.ip_address(hostname).is_private
        except ValueError:
            private_hostname = False
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or (parsed.scheme == "http" and not private_hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("signed URL is invalid")
    return value


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "1.1"]
    registration_code: str = Field(pattern=r"^ppreg_[A-Za-z0-9_-]{43}$", repr=False)
    public_key_b64: str = Field(repr=False)
    platform: AgentPlatform
    agent_version: str
    hostname: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    os_version: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("public_key_b64")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return _validate_raw_public_key(value)

    @field_validator("agent_version")
    @classmethod
    def validate_agent_version(cls, value: str) -> str:
        if len(value) > 64 or _AGENT_VERSION.fullmatch(value) is None:
            raise ValueError("Agent version is invalid")
        return value


class AutoRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"]
    public_key_b64: str = Field(repr=False)
    platform: AgentPlatform
    agent_version: str
    hostname: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    os_version: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("public_key_b64")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return _validate_raw_public_key(value)

    @field_validator("agent_version")
    @classmethod
    def validate_agent_version(cls, value: str) -> str:
        if len(value) > 64 or _AGENT_VERSION.fullmatch(value) is None:
            raise ValueError("Agent version is invalid")
        return value


class TaskSigningKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kid: str
    public_key_b64: str = Field(repr=False)

    @field_validator("kid")
    @classmethod
    def validate_kid(cls, value: str) -> str:
        if _KEY_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("task-signing key identifier is invalid")
        return value

    @field_validator("public_key_b64")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return _validate_raw_public_key(value)


class RegistrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "1.1"]
    agent_id: UUID
    team_id: UUID | None = None
    access_token: str = Field(pattern=r"^ppat_[A-Za-z0-9_-]{43}$", repr=False)
    access_token_expires_at: datetime
    refresh_token: str = Field(pattern=r"^pprt_[A-Za-z0-9_-]{43}$", repr=False)
    refresh_token_expires_at: datetime
    task_signing_key: TaskSigningKeyResponse
    heartbeat_interval_seconds: Literal[10]

    @field_validator("access_token_expires_at", "refresh_token_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_expirations(self) -> Self:
        if self.refresh_token_expires_at <= self.access_token_expires_at:
            raise ValueError("credential expirations are invalid")
        if (self.schema_version == "1.1") != (self.team_id is not None):
            raise ValueError("credential team binding is invalid")
        return self


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "1.1"]
    agent_id: UUID
    refresh_token: str = Field(pattern=r"^pprt_[A-Za-z0-9_-]{43}$", repr=False)
    nonce: str
    timestamp: int = Field(strict=True)
    signature_b64: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$", repr=False)

    @field_validator("nonce")
    @classmethod
    def validate_nonce(cls, value: str) -> str:
        if _NONCE.fullmatch(value) is None:
            raise ValueError("refresh nonce is invalid")
        return value


class ExecutionSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["idle", "busy"]
    execution_id: UUID | None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "idle") != (self.execution_id is None):
            raise ValueError("execution slot is invalid")
        return self


class HeartbeatLaunchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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

    @model_validator(mode="after")
    def validate_component_package(self) -> Self:
        if self.launch_activity.split("/", 1)[0] != self.package_name:
            raise ValueError("launch target package is invalid")
        return self


class HeartbeatDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_ref: UUID
    serial: str = Field(min_length=1, max_length=255, pattern=r"^[!-~]+$", repr=False)
    manufacturer: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[^\x00-\x1f\x7f]*$",
    )
    model: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[^\x00-\x1f\x7f]*$",
    )
    android_release: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[^\x00-\x1f\x7f]*$",
    )
    api_level: int | None = Field(default=None, strict=True, ge=1, le=1_000)
    connection_type: Literal["usb", "wifi", "unknown"]
    adb_state: Literal["device", "unauthorized", "offline", "booting"]
    battery_percent: int | None = Field(default=None, strict=True, ge=0, le=100)
    temperature_c: float | None = Field(default=None, ge=-100, le=200)
    storage_available_bytes: int | None = Field(default=None, strict=True, ge=0)
    property_error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    launch_targets: tuple[HeartbeatLaunchTarget, ...] = Field(
        default=(),
        max_length=128,
    )

    @field_validator("launch_targets")
    @classmethod
    def validate_unique_launch_targets(
        cls,
        value: tuple[HeartbeatLaunchTarget, ...],
    ) -> tuple[HeartbeatLaunchTarget, ...]:
        if len({item.launch_activity for item in value}) != len(value):
            raise ValueError("launch targets must be unique")
        return value


class HeartbeatValidationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: UUID
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")


class HeartbeatWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    state: Literal["ready", "invalid"]
    git_branch: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    git_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    tracked_dirty_count: int = Field(strict=True, ge=0)
    snapshot_policy: Literal["tracked_worktree"]
    validation_profiles: tuple[HeartbeatValidationProfile, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_profile_ids(self) -> Self:
        identifiers = [profile.profile_id for profile in self.validation_profiles]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("validation profiles must be unique")
        return self


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "1.1"]
    agent_version: str
    platform: AgentPlatform
    hostname: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    observed_at: datetime
    clock_skew_ms: int = Field(strict=True, ge=-300_000, le=300_000)
    disk_available_bytes: int = Field(strict=True, ge=0, le=2**63 - 1)
    execution_slot: ExecutionSlot
    devices: tuple[HeartbeatDevice, ...] = Field(max_length=32)
    workspaces: tuple[HeartbeatWorkspace, ...] | None = Field(default=None, max_length=32)

    @field_validator("agent_version")
    @classmethod
    def validate_agent_version(cls, value: str) -> str:
        if len(value) > 64 or _AGENT_VERSION.fullmatch(value) is None:
            raise ValueError("Agent version is invalid")
        return value

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_workspace_version(self) -> Self:
        if self.schema_version == "1.0" and (
            self.workspaces is not None or "workspaces" in self.model_fields_set
        ):
            raise ValueError("heartbeat workspace version is invalid")
        if self.schema_version == "1.1" and self.workspaces is None:
            raise ValueError("heartbeat workspace version is invalid")
        if self.workspaces is not None:
            identifiers = [workspace.workspace_id for workspace in self.workspaces]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("heartbeat workspaces must be unique")
        return self


class HeartbeatDeviceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_ref: UUID
    device_id: UUID
    device_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    accepted_at: datetime
    next_heartbeat_seconds: Literal[10]
    devices: tuple[HeartbeatDeviceReceipt, ...] = Field(max_length=32)

    @field_validator("accepted_at", mode="before")
    @classmethod
    def require_string_timestamp(cls, value: object) -> object:
        return _require_string_timestamp(value)

    @field_validator("accepted_at")
    @classmethod
    def validate_accepted_at(cls, value: datetime) -> datetime:
        return _aware(value)


class TaskWaitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    action: Literal["wait"]
    retry_after_seconds: int = Field(strict=True, ge=1, le=20)


class TaskExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    action: Literal["execute"]
    snapshot_jws: str = Field(
        min_length=32,
        max_length=32_768,
        pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
        repr=False,
    )
    lease_expires_at: datetime
    renew_after_seconds: Literal[20]

    @field_validator("lease_expires_at", mode="before")
    @classmethod
    def require_string_timestamp(cls, value: object) -> object:
        return _require_string_timestamp(value)

    @field_validator("lease_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class SourceTaskExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"]
    task_kind: Literal["source"]
    lease_token: str = Field(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    snapshot: dict[str, object] = Field(repr=False)
    signature_b64: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$", repr=False)

    @field_validator("snapshot")
    @classmethod
    def validate_closed_snapshot(cls, value: dict[str, object]) -> dict[str, object]:
        TypeAdapter(
            Annotated[
                VerifiedSourceContextTask | VerifiedPatchVerificationTask,
                Field(discriminator="task_type"),
            ]
        ).validate_python(value)
        return value


class DeviceTaskExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"]
    task_kind: Literal["device"]
    lease_token: str = Field(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    snapshot: dict[str, object] = Field(repr=False)
    signature_b64: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$", repr=False)

    @field_validator("snapshot")
    @classmethod
    def validate_closed_snapshot(cls, value: dict[str, object]) -> dict[str, object]:
        TaskSnapshot.model_validate(value)
        return value


class TaskCancellationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    action: Literal["cancel"]
    execution_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    reason_code: str = Field(min_length=1, max_length=96)

    @field_validator("reason_code")
    @classmethod
    def validate_reason_code(cls, value: str) -> str:
        if _STABLE_CODE.fullmatch(value) is None:
            raise ValueError("cancellation reason is invalid")
        return value


class TaskRenewalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    execution_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    lease_expires_at: datetime
    renew_after_seconds: Literal[20]

    @field_validator("lease_expires_at", mode="before")
    @classmethod
    def require_string_timestamp(cls, value: object) -> object:
        return _require_string_timestamp(value)

    @field_validator("lease_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class SourceTaskMutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"]
    execution_id: UUID
    analysis_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    state: Literal["running", "cancel_requested"]
    accepted_at: datetime


class SourceTaskCancellationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"]
    execution_id: UUID
    analysis_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    state: Literal["canceled"]
    acknowledged_at: datetime


class SourceTaskCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"]
    execution_id: UUID
    analysis_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    state: Literal["completed", "failed", "canceled", "expired"]
    artifact_id: UUID
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_at: datetime


class CancellationAcknowledgementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    execution_id: UUID
    analysis_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    state: Literal["canceled"]
    acknowledged_at: datetime

    @field_validator("acknowledged_at", mode="before")
    @classmethod
    def require_string_timestamp(cls, value: object) -> object:
        return _require_string_timestamp(value)

    @field_validator("acknowledged_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class CompletionAcknowledgementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    execution_id: UUID
    analysis_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    analysis_state: Literal["completed", "partially_completed", "failed"]
    accepted_at: datetime

    @field_validator("accepted_at", mode="before")
    @classmethod
    def require_string_timestamp(cls, value: object) -> object:
        return _require_string_timestamp(value)

    @field_validator("accepted_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class InputAuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    artifact_id: UUID
    mime: str
    size: int = Field(strict=True, ge=1, le=5 * 1024 * 1024 * 1024)
    sha256_b64: str = Field(pattern=r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$", repr=False)
    download_url: str = Field(min_length=9, max_length=8_192, repr=False)
    expires_at: datetime

    @field_validator("mime")
    @classmethod
    def validate_mime(cls, value: str) -> str:
        if _MIME_TYPE.fullmatch(value) is None:
            raise ValueError("input MIME type is invalid")
        return value

    @field_validator("download_url")
    @classmethod
    def validate_download_url(cls, value: str) -> str:
        return _validate_signed_url(value)

    @field_validator("expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class UploadSlotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    artifact_id: UUID
    upload_id: UUID
    artifact_kind: Literal[
        "startup_trace",
        "scroll_trace",
        "memory_evidence",
        "agent_log",
    ]
    mime: str
    size: int = Field(strict=True, ge=1, le=512 * 1024 * 1024)
    sha256_b64: str = Field(pattern=r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$", repr=False)
    part_size_bytes: int = Field(strict=True, ge=1, le=512 * 1024 * 1024)
    part_count: int = Field(strict=True, ge=1, le=10_000)
    state: Literal["pending", "finalized", "aborted", "expired"]
    expires_at: datetime
    finalized_at: datetime | None

    @field_validator("mime")
    @classmethod
    def validate_mime(cls, value: str) -> str:
        if _MIME_TYPE.fullmatch(value) is None:
            raise ValueError("upload MIME type is invalid")
        return value

    @field_validator("expires_at", "finalized_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "finalized") != (self.finalized_at is not None):
            raise ValueError("upload finalization state is invalid")
        expected_parts = (self.size + self.part_size_bytes - 1) // self.part_size_bytes
        if expected_parts != self.part_count:
            raise ValueError("upload part count is invalid")
        return self


class UploadPartAuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    upload_id: UUID
    part_number: int = Field(strict=True, ge=1, le=10_000)
    put_url: str = Field(min_length=9, max_length=8_192, repr=False)
    required_headers: dict[str, str] = Field(max_length=32, repr=False)
    expires_at: datetime

    @field_validator("put_url")
    @classmethod
    def validate_put_url(cls, value: str) -> str:
        return _validate_signed_url(value)

    @field_validator("required_headers")
    @classmethod
    def validate_required_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            _HEADER_NAME.fullmatch(name) is None
            or len(item) > 4_096
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
            for name, item in value.items()
        ):
            raise ValueError("upload headers are invalid")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class UploadPartReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    part_number: int = Field(strict=True, ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=1_024, pattern=r"^[^\x00-\x1f\x7f]+$", repr=False)


class UnregistrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    agent_id: UUID
    state: Literal["revoked"]


class _ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=512)
    retryable: bool
    request_id: str = Field(min_length=1, max_length=128)


class _ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    error: _ErrorDetail


TaskPollResponse = (
    TaskWaitResponse
    | TaskExecuteResponse
    | TaskCancellationResponse
    | DeviceTaskExecuteResponse
    | SourceTaskExecuteResponse
)
TaskRenewResponse = TaskRenewalResponse | TaskCancellationResponse
_TASK_POLL_ADAPTER = TypeAdapter(TaskPollResponse)
_TASK_RENEW_ADAPTER = TypeAdapter(TaskRenewResponse)


class ControlClient:
    def __init__(
        self,
        config: AgentConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        credentials: AgentCredentials | None = None,
        credential_store: CredentialStore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: secrets.randbelow(250) / 1_000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        nonce_factory: Callable[[], str] = lambda: base64.urlsafe_b64encode(secrets.token_bytes(24))
        .rstrip(b"=")
        .decode("ascii"),
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            verify=str(config.ca_bundle),
            timeout=httpx.Timeout(25.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={"accept": "application/json"},
        )
        self._credential_store = credential_store
        self._credentials = credentials
        if self._credentials is None and credential_store is not None:
            self._credentials = credential_store.load()
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._refresh_lock = asyncio.Lock()

    @property
    def credentials(self) -> AgentCredentials:
        if self._credentials is None:
            raise ControlClientError
        return self._credentials

    def bind_credentials(
        self,
        credentials: AgentCredentials,
        *,
        store: CredentialStore | None = None,
    ) -> None:
        self._credentials = AgentCredentials.model_validate(credentials)
        if store is not None:
            self._credential_store = store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _payload(response: httpx.Response) -> bytes:
        payload = response.content
        if not payload or len(payload) > _MAXIMUM_RESPONSE_BYTES:
            raise ControlClientError
        return payload

    @staticmethod
    def _error(response: httpx.Response) -> ControlClientError:
        try:
            envelope = _ErrorEnvelope.model_validate_json(ControlClient._payload(response))
        except (ControlClientError, ValidationError, ValueError, TypeError, UnicodeError):
            return ControlClientError(status_code=response.status_code)
        return ControlClientError(
            status_code=response.status_code,
            code=envelope.error.code,
            retryable=envelope.error.retryable,
        )

    async def _backoff(self, attempt: int) -> None:
        jitter = self._jitter()
        if not isinstance(jitter, (int, float)) or isinstance(jitter, bool):
            jitter = 0
        await self._sleep(min(2.0, 0.25 * (2**attempt)) + max(0.0, min(0.25, jitter)))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        access_token: str | None = None,
        retry: bool,
        lease_fenced: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bytes:
        headers = dict(extra_headers or {})
        if access_token is not None:
            if _ACCESS_TOKEN.fullmatch(access_token) is None:
                raise ControlClientError
            headers["authorization"] = f"Bearer {access_token}"
        attempts = _MAXIMUM_ATTEMPTS if retry else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    f"{self._config.server_url}{path}",
                    json=json,
                    params=params,
                    headers=headers,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt + 1 == attempts:
                    raise ControlClientError(retryable=True) from None
                await self._backoff(attempt)
                continue
            except httpx.HTTPError:
                raise ControlClientError from None
            if response.status_code == expected_status:
                return self._payload(response)
            retryable_status = (
                response.status_code in _RETRYABLE_STATUSES or response.status_code >= 500
            )
            if retryable_status and attempt + 1 < attempts:
                await self._backoff(attempt)
                continue
            error = self._error(response)
            if lease_fenced and error.code in {"resource_not_found", "stale_lease_version"}:
                raise LeaseLost from None
            if retryable_status:
                raise ControlClientError(
                    status_code=response.status_code,
                    code=error.code,
                    retryable=True,
                ) from None
            raise error
        raise ControlClientError

    async def _authorized_request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        access_token: str | None = None,
        lease_fenced: bool = False,
        retry: bool = True,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bytes:
        bound = self._credentials
        token = bound.access_token if bound is not None else access_token
        if token is None:
            raise ControlClientError
        try:
            return await self._request(
                method,
                path,
                expected_status=expected_status,
                json=json,
                params=params,
                access_token=token,
                retry=retry,
                lease_fenced=lease_fenced,
                extra_headers=extra_headers,
            )
        except ControlClientError as error:
            if error.status_code != 401 or bound is None:
                raise
        await self._refresh_after_unauthorized(token)
        return await self._request(
            method,
            path,
            expected_status=expected_status,
            json=json,
            params=params,
            access_token=self.credentials.access_token,
            retry=retry,
            lease_fenced=lease_fenced,
            extra_headers=extra_headers,
        )

    @staticmethod
    def _source_lease_headers(lease_token: str) -> dict[str, str]:
        if not 16 <= len(lease_token) <= 256 or re.fullmatch(
            r"[A-Za-z0-9_-]+", lease_token
        ) is None:
            raise ControlClientError
        return {"x-perfpilot-lease-token": lease_token}

    async def register(
        self,
        request: RegistrationRequest | Mapping[str, object],
    ) -> RegistrationResponse:
        try:
            normalized = RegistrationRequest.model_validate(request)
            payload = await self._request(
                "POST",
                "/v1/agent/register",
                expected_status=201,
                json=normalized.model_dump(mode="json"),
                retry=False,
            )
            return RegistrationResponse.model_validate_json(payload)
        except ControlClientError:
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def auto_register(
        self,
        request: AutoRegistrationRequest | Mapping[str, object],
    ) -> RegistrationResponse:
        try:
            normalized = AutoRegistrationRequest.model_validate(request)
            payload = await self._request(
                "POST",
                "/v1/agent/auto-register",
                expected_status=201,
                json=normalized.model_dump(mode="json"),
                retry=False,
            )
            return RegistrationResponse.model_validate_json(payload)
        except ControlClientError:
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def _refresh_after_unauthorized(self, failed_access_token: str) -> None:
        async with self._refresh_lock:
            if self.credentials.access_token != failed_access_token:
                return
            await self._refresh_locked()

    async def refresh_credentials(self, *, force: bool = False) -> AgentCredentials:
        async with self._refresh_lock:
            current = self.credentials
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ControlClientError
            if not force and current.access_token_expires_at > now + timedelta(minutes=2):
                return current
            return await self._refresh_locked()

    async def _refresh_locked(self) -> AgentCredentials:
        try:
            current = self.credentials
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ControlClientError
            nonce = self._nonce_factory()
            timestamp = int(now.timestamp())
            message = f"{current.agent_id}\n{nonce}\n{timestamp}".encode("ascii")
            private_key = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(current.private_key_b64, validate=True)
            )
            request = RefreshRequest(
                schema_version=current.schema_version,
                agent_id=current.agent_id,
                refresh_token=current.refresh_token,
                nonce=nonce,
                timestamp=timestamp,
                signature_b64=base64.b64encode(private_key.sign(message)).decode("ascii"),
            )
            payload = await self._request(
                "POST",
                "/v1/agent/token/refresh",
                expected_status=200,
                json=request.model_dump(mode="json"),
                retry=False,
            )
            response = RegistrationResponse.model_validate_json(payload)
            if response.agent_id != current.agent_id:
                raise ControlClientError
            refreshed = AgentCredentials(
                schema_version=response.schema_version,
                agent_id=current.agent_id,
                team_id=response.team_id,
                private_key_b64=current.private_key_b64,
                access_token=response.access_token,
                access_token_expires_at=response.access_token_expires_at,
                refresh_token=response.refresh_token,
                refresh_token_expires_at=response.refresh_token_expires_at,
                task_signing_key=TaskSigningKey(
                    kid=response.task_signing_key.kid,
                    public_key_b64=response.task_signing_key.public_key_b64,
                ),
                heartbeat_interval_seconds=response.heartbeat_interval_seconds,
            )
            if self._credential_store is not None:
                self._credential_store.save(refreshed)
            self._credentials = refreshed
            return refreshed
        except ControlClientError:
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError, binascii.Error):
            raise ControlClientError from None

    async def heartbeat(
        self,
        request: HeartbeatRequest | Mapping[str, object],
        *,
        access_token: str | None = None,
    ) -> HeartbeatResponse:
        try:
            normalized = HeartbeatRequest.model_validate(request)
            document = normalized.model_dump(mode="json")
            if normalized.workspaces is None:
                document.pop("workspaces", None)
            if normalized.schema_version == "1.0":
                for device in document["devices"]:
                    device.pop("launch_targets", None)
            payload = await self._authorized_request(
                "POST",
                "/v1/agent/heartbeat",
                expected_status=200,
                json=document,
                access_token=access_token,
            )
            return HeartbeatResponse.model_validate_json(payload)
        except ControlClientError:
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def unregister(
        self,
        *,
        access_token: str | None = None,
    ) -> UnregistrationResponse:
        try:
            credentials = self._credentials
            payload = await self._authorized_request(
                "POST",
                "/v1/agent/unregister",
                expected_status=200,
                access_token=access_token,
                retry=False,
            )
            response = UnregistrationResponse.model_validate_json(payload)
            if credentials is not None and response.agent_id != credentials.agent_id:
                raise ControlClientError
            return response
        except ControlClientError:
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def poll_task(
        self,
        *,
        wait_seconds: int = 20,
        access_token: str | None = None,
    ) -> TaskPollResponse:
        try:
            if isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 20:
                raise ControlClientError
            payload = await self._authorized_request(
                "GET",
                "/v1/agent/tasks/next",
                expected_status=200,
                params={"wait_seconds": wait_seconds},
                access_token=access_token,
            )
            return _TASK_POLL_ADAPTER.validate_json(payload)
        except ControlClientError:
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def authorize_input(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        artifact_id: UUID,
        access_token: str | None = None,
    ) -> InputAuthorizationResponse:
        try:
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/inputs/{artifact_id}",
                expected_status=200,
                json={"schema_version": "1.0", "lease_version": lease_version},
                access_token=access_token,
                lease_fenced=True,
            )
            response = InputAuthorizationResponse.model_validate_json(payload)
            if response.artifact_id != artifact_id:
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def create_upload(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        artifact_kind: str,
        mime: str,
        size: int,
        sha256_b64: str,
        access_token: str | None = None,
    ) -> UploadSlotResponse:
        try:
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/uploads",
                expected_status=201,
                json={
                    "schema_version": "1.0",
                    "lease_version": lease_version,
                    "artifact_kind": artifact_kind,
                    "mime": mime,
                    "size": size,
                    "sha256_b64": sha256_b64,
                },
                access_token=access_token,
                lease_fenced=True,
            )
            response = UploadSlotResponse.model_validate_json(payload)
            if (
                response.artifact_kind != artifact_kind
                or response.mime != mime
                or response.size != size
                or response.sha256_b64 != sha256_b64
            ):
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def authorize_upload_part(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        upload_id: UUID,
        part_number: int,
        access_token: str | None = None,
    ) -> UploadPartAuthorizationResponse:
        try:
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/uploads/{upload_id}/parts",
                expected_status=200,
                json={
                    "schema_version": "1.0",
                    "lease_version": lease_version,
                    "part_number": part_number,
                },
                access_token=access_token,
                lease_fenced=True,
            )
            response = UploadPartAuthorizationResponse.model_validate_json(payload)
            if response.upload_id != upload_id or response.part_number != part_number:
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def complete_upload(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        upload_id: UUID,
        parts: Sequence[UploadPartReceipt | Mapping[str, object]],
        access_token: str | None = None,
    ) -> UploadSlotResponse:
        try:
            canonical = tuple(UploadPartReceipt.model_validate(part) for part in parts)
            if any(part.part_number != index for index, part in enumerate(canonical, start=1)):
                raise ControlClientError
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/uploads/{upload_id}/complete",
                expected_status=200,
                json={
                    "schema_version": "1.0",
                    "lease_version": lease_version,
                    "parts": [part.model_dump(mode="json") for part in canonical],
                },
                access_token=access_token,
                lease_fenced=True,
            )
            response = UploadSlotResponse.model_validate_json(payload)
            if response.upload_id != upload_id or response.state != "finalized":
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def renew_task(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        access_token: str | None = None,
    ) -> TaskRenewResponse:
        try:
            if isinstance(lease_version, bool) or lease_version < 1:
                raise ControlClientError
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/renew",
                expected_status=200,
                json={"schema_version": "1.0", "lease_version": lease_version},
                access_token=access_token,
                lease_fenced=True,
            )
            response = _TASK_RENEW_ADAPTER.validate_json(payload)
            if response.execution_id != execution_id or response.lease_version != lease_version:
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def renew_source_task(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        lease_token: str,
        access_token: str | None = None,
    ) -> SourceTaskMutationResponse:
        payload = await self._authorized_request(
            "POST",
            f"/v1/agent/tasks/{execution_id}/renew",
            expected_status=200,
            json={"schema_version": "1.0", "lease_version": lease_version},
            access_token=access_token,
            lease_fenced=True,
            extra_headers=self._source_lease_headers(lease_token),
        )
        response = SourceTaskMutationResponse.model_validate_json(payload)
        if response.execution_id != execution_id or response.lease_version != lease_version:
            raise ControlClientError
        return response

    async def acknowledge_source_cancellation(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        lease_token: str,
        access_token: str | None = None,
    ) -> SourceTaskCancellationResponse:
        payload = await self._authorized_request(
            "POST",
            f"/v1/agent/tasks/{execution_id}/cancel-ack",
            expected_status=200,
            json={
                "schema_version": "1.0",
                "lease_version": lease_version,
                "reason_code": "analysis_canceled",
            },
            access_token=access_token,
            lease_fenced=True,
            extra_headers=self._source_lease_headers(lease_token),
        )
        response = SourceTaskCancellationResponse.model_validate_json(payload)
        if response.execution_id != execution_id or response.lease_version != lease_version:
            raise ControlClientError
        return response

    async def complete_source_task(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        lease_token: str,
        completion: Mapping[str, object],
        access_token: str | None = None,
    ) -> SourceTaskCompletionResponse:
        document = dict(completion)
        credentials = self._credentials
        if credentials is None or credentials.team_id is None or "signature_b64" in document:
            raise ControlClientError
        document["agent_id"] = str(credentials.agent_id)
        document["team_id"] = str(credentials.team_id)
        unsigned = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(credentials.private_key_b64, validate=True)
            )
        except (ValueError, TypeError, binascii.Error):
            raise ControlClientError from None
        document["signature_b64"] = base64.b64encode(private_key.sign(unsigned)).decode(
            "ascii"
        )
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if (
            len(canonical) > 128 * 1024
            or document.get("execution_id") not in {execution_id, str(execution_id)}
            or document.get("lease_version") != lease_version
        ):
            raise ControlClientError
        payload = await self._authorized_request(
            "POST",
            f"/v1/agent/tasks/{execution_id}/complete",
            expected_status=200,
            json=document,
            access_token=access_token,
            lease_fenced=True,
            extra_headers=self._source_lease_headers(lease_token),
        )
        response = SourceTaskCompletionResponse.model_validate_json(payload)
        if response.execution_id != execution_id or response.lease_version != lease_version:
            raise ControlClientError
        return response

    async def acknowledge_cancellation(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        access_token: str | None = None,
    ) -> CancellationAcknowledgementResponse:
        try:
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/cancel-ack",
                expected_status=200,
                json={
                    "schema_version": "1.0",
                    "lease_version": lease_version,
                    "reason_code": "analysis_canceled",
                },
                access_token=access_token,
                lease_fenced=True,
            )
            response = CancellationAcknowledgementResponse.model_validate_json(payload)
            if response.execution_id != execution_id or response.lease_version != lease_version:
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None

    async def complete_execution(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        manifest: Mapping[str, object],
        access_token: str | None = None,
    ) -> CompletionAcknowledgementResponse:
        try:
            document = dict(manifest)
            if (
                document.get("execution_id") not in {execution_id, str(execution_id)}
                or document.get("lease_version") != lease_version
            ):
                raise ControlClientError
            payload = await self._authorized_request(
                "POST",
                f"/v1/agent/tasks/{execution_id}/complete",
                expected_status=200,
                json=document,
                access_token=access_token,
                lease_fenced=True,
            )
            response = CompletionAcknowledgementResponse.model_validate_json(payload)
            if response.execution_id != execution_id or response.lease_version != lease_version:
                raise ControlClientError
            return response
        except (ControlClientError, LeaseLost):
            raise
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None


__all__ = [
    "AutoRegistrationRequest",
    "CancellationAcknowledgementResponse",
    "CompletionAcknowledgementResponse",
    "ControlClient",
    "ControlClientError",
    "DeviceTaskExecuteResponse",
    "ExecutionSlot",
    "HeartbeatDevice",
    "HeartbeatDeviceReceipt",
    "HeartbeatRequest",
    "HeartbeatValidationProfile",
    "HeartbeatWorkspace",
    "HeartbeatResponse",
    "InputAuthorizationResponse",
    "LeaseLost",
    "RefreshRequest",
    "RegistrationRequest",
    "RegistrationResponse",
    "TaskCancellationResponse",
    "TaskExecuteResponse",
    "TaskPollResponse",
    "TaskRenewResponse",
    "TaskRenewalResponse",
    "TaskSigningKeyResponse",
    "TaskWaitResponse",
    "SourceTaskExecuteResponse",
    "SourceTaskCancellationResponse",
    "SourceTaskCompletionResponse",
    "SourceTaskMutationResponse",
    "UnregistrationResponse",
    "UploadPartAuthorizationResponse",
    "UploadPartReceipt",
    "UploadSlotResponse",
]
