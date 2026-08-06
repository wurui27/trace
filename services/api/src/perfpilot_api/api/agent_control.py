from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from perfpilot_api.api.agents import get_agent_service, get_device_directory
from perfpilot_api.errors import ApiError
from perfpilot_api.services.agents import (
    AgentAuthenticationRejected,
    AgentNotFoundError,
    AgentRegistration,
    AgentRegistrationRejected,
    AgentService,
    IssuedAgentCredentials,
)
from perfpilot_api.services.device_directory import (
    AgentHeartbeat,
    DeviceDirectory,
    DeviceHeartbeatRejected,
)
from perfpilot_api.services.agent_tasks import (
    AgentTaskCancellation,
    AgentTaskConflict,
    AgentTaskNotFound,
    AgentTaskService,
    AgentTaskUnavailable,
    StaleLeaseVersion,
)
from perfpilot_api.services.agent_uploads import (
    AgentUploadError,
    AgentUploadExpired,
    AgentUploadInvalidRequest,
    AgentUploadMismatch,
    AgentUploadNotFound,
    AgentUploadService,
    AgentUploadStaleLease,
    AgentUploadUnavailable,
)
from perfpilot_api.storage.base import MultipartPart

_PUBLIC_KEY_PATTERN = r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$"
_REGISTRATION_CODE_PATTERN = r"^ppreg_[A-Za-z0-9_-]{43}$"
_REFRESH_TOKEN_PATTERN = r"^pprt_[A-Za-z0-9_-]{43}$"
_NONCE_PATTERN = r"^[A-Za-z0-9_-]{22,128}$"
_SIGNATURE_PATTERN = r"^[A-Za-z0-9+/]{86}==$"
_AGENT_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"


class RegisterAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    registration_code: str = Field(pattern=_REGISTRATION_CODE_PATTERN)
    public_key_b64: str = Field(min_length=44, max_length=44, pattern=_PUBLIC_KEY_PATTERN)
    platform: Literal["macos", "windows", "linux"]
    agent_version: str = Field(min_length=1, max_length=64, pattern=_AGENT_VERSION_PATTERN)
    hostname: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    os_version: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")


class RefreshAgentTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    agent_id: UUID
    refresh_token: str = Field(pattern=_REFRESH_TOKEN_PATTERN)
    nonce: str = Field(pattern=_NONCE_PATTERN)
    timestamp: int = Field(strict=True)
    signature_b64: str = Field(pattern=_SIGNATURE_PATTERN)


class ExecutionSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["idle", "busy"]
    execution_id: UUID | None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "idle") != (self.execution_id is None):
            raise ValueError("execution slot state is invalid")
        return self


class HeartbeatDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ref: UUID
    serial: str = Field(min_length=1, max_length=255, pattern=r"^[!-~]+$")
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
    api_level: int | None = Field(default=None, strict=True, ge=1, le=1000)
    connection_type: Literal["usb", "wifi", "unknown"]
    adb_state: Literal["device", "unauthorized", "offline", "booting"]
    battery_percent: int | None = Field(default=None, strict=True, ge=0, le=100)
    temperature_c: Decimal | None = Field(default=None, ge=-100, le=200)
    storage_available_bytes: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=2**63 - 1,
    )
    property_error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )


class AgentHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    agent_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=_AGENT_VERSION_PATTERN,
    )
    platform: Literal["macos", "windows", "linux"]
    hostname: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    observed_at: datetime
    clock_skew_ms: int = Field(strict=True, ge=-300_000, le=300_000)
    disk_available_bytes: int = Field(strict=True, ge=0, le=2**63 - 1)
    execution_slot: ExecutionSlot
    devices: tuple[HeartbeatDevice, ...] = Field(max_length=32)

    @field_validator("observed_at")
    @classmethod
    def require_aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class RenewAgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    lease_version: int = Field(strict=True, ge=1)


class AcknowledgeAgentCancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    lease_version: int = Field(strict=True, ge=1)
    reason_code: Literal["analysis_canceled"]


class CreateAgentUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    lease_version: int = Field(strict=True, ge=1)
    artifact_kind: Literal[
        "startup_trace",
        "scroll_trace",
        "memory_evidence",
        "agent_log",
    ]
    mime: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$",
    )
    size: int = Field(strict=True, ge=1, le=512 * 1024 * 1024)
    sha256_b64: str = Field(min_length=44, max_length=44, pattern=r"^[A-Za-z0-9+/]{43}=$")


class AuthorizeAgentUploadPartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    lease_version: int = Field(strict=True, ge=1)
    part_number: int = Field(strict=True, ge=1, le=10_000)


class AgentUploadCompletedPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(strict=True, ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=1024, pattern=r"^[^\x00-\x1f\x7f]+$")


class CompleteAgentUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    lease_version: int = Field(strict=True, ge=1)
    parts: tuple[AgentUploadCompletedPart, ...] = Field(min_length=1, max_length=10_000)


class AgentExecutionArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    kind: Literal["startup_trace", "scroll_trace", "memory_evidence", "agent_log"]
    mime: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$",
    )
    size: int = Field(strict=True, ge=1, le=512 * 1024 * 1024)
    sha256_b64: str = Field(
        min_length=44,
        max_length=44,
        pattern=r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$",
    )


class AgentExecutionScenarioPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["startup", "scroll", "memory_cycle"]
    state: Literal["completed", "failed", "skipped"]
    started_at: datetime
    completed_at: datetime
    temperature_start_c: Decimal | None = Field(default=None, ge=-100, le=200)
    temperature_end_c: Decimal | None = Field(default=None, ge=-100, le=200)
    artifact_ids: tuple[UUID, ...] = Field(max_length=16)
    diagnostic_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("execution timestamps must include a timezone")
        return value


class CompleteAgentExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    execution_id: UUID
    lease_version: int = Field(strict=True, ge=1)
    state: Literal["completed", "failed"]
    started_at: datetime
    completed_at: datetime
    agent_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=_AGENT_VERSION_PATTERN,
    )
    adb_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    artifacts: tuple[AgentExecutionArtifactPayload, ...] = Field(
        min_length=1,
        max_length=32,
    )
    scenarios: tuple[AgentExecutionScenarioPayload, ...] = Field(
        min_length=1,
        max_length=3,
    )
    diagnostic_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("execution timestamps must include a timezone")
        return value


def _reject_browser_credentials(request: Request) -> None:
    forbidden = (
        "cookie",
        "x-csrf-token",
        "x-perfpilot-proxy-timestamp",
        "x-perfpilot-proxy-signature",
        "x-perfpilot-client-identity",
    )
    if any(request.headers.getlist(name) for name in forbidden):
        raise ApiError("agent_authentication_failed", "Agent 认证失败", 401, False)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _credentials_payload(credentials: IssuedAgentCredentials) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "agent_id": str(credentials.agent_id),
        "access_token": credentials.access_token,
        "access_token_expires_at": _utc(credentials.access_token_expires_at),
        "refresh_token": credentials.refresh_token,
        "refresh_token_expires_at": _utc(credentials.refresh_token_expires_at),
        "task_signing_key": {
            "kid": credentials.task_signing_key.kid,
            "public_key_b64": credentials.task_signing_key.public_key_b64,
        },
        "heartbeat_interval_seconds": credentials.heartbeat_interval_seconds,
    }


async def _authenticate_access(
    request: Request,
    agent_service: AgentService,
):
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) != 1:
        raise ApiError("agent_authentication_failed", "Agent 认证失败", 401, False)
    scheme, separator, token = authorization_values[0].partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token or " " in token:
        raise ApiError("agent_authentication_failed", "Agent 认证失败", 401, False)
    try:
        return await agent_service.authenticate_access(token)
    except AgentAuthenticationRejected:
        raise ApiError("agent_authentication_failed", "Agent 认证失败", 401, False) from None


def get_agent_task_service(request: Request) -> AgentTaskService:
    service: AgentTaskService | None = request.app.state.agent_task_service
    if service is None:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return service


def get_agent_upload_service(request: Request) -> AgentUploadService:
    service: AgentUploadService | None = request.app.state.agent_upload_service
    if service is None:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return service


def _raise_agent_upload_error(error: AgentUploadError) -> None:
    if isinstance(error, AgentUploadInvalidRequest):
        raise ApiError("invalid_request", "上传请求无效", 400, False) from None
    if isinstance(error, AgentUploadStaleLease):
        raise ApiError("stale_lease_version", "租约版本已经变化", 409, True) from None
    if isinstance(error, AgentUploadNotFound):
        raise ApiError("resource_not_found", "资源不存在", 404, False) from None
    if isinstance(error, AgentUploadExpired):
        raise ApiError("upload_expired", "上传任务已经过期", 410, False) from None
    if isinstance(error, AgentUploadMismatch):
        raise ApiError("upload_mismatch", "上传内容校验失败", 409, False) from None
    if isinstance(error, AgentUploadUnavailable):
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True) from None
    raise ApiError("service_unavailable", "服务暂时不可用", 503, True) from None


def _agent_upload_payload(upload) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "artifact_id": str(upload.artifact_id),
        "upload_id": str(upload.upload_id),
        "artifact_kind": upload.artifact_kind,
        "mime": upload.mime,
        "size": upload.size,
        "sha256_b64": upload.sha256_b64,
        "part_size_bytes": upload.part_size_bytes,
        "part_count": upload.part_count,
        "state": upload.state,
        "expires_at": _utc(upload.expires_at),
        "finalized_at": None if upload.finalized_at is None else _utc(upload.finalized_at),
    }


def _cancellation_payload(cancellation: AgentTaskCancellation) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "action": "cancel",
        "execution_id": str(cancellation.execution_id),
        "lease_version": cancellation.lease_version,
        "reason_code": cancellation.reason_code,
    }


router = APIRouter(
    prefix="/v1/agent",
    dependencies=[Depends(_reject_browser_credentials)],
)


@router.post("/register", status_code=201)
async def register_agent(
    payload: RegisterAgentRequest,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    try:
        credentials = await agent_service.register(
            AgentRegistration(
                registration_code=payload.registration_code,
                public_key_b64=payload.public_key_b64,
                platform=payload.platform,
                agent_version=payload.agent_version,
                hostname=payload.hostname,
                os_version=payload.os_version,
            )
        )
    except AgentRegistrationRejected:
        raise ApiError("registration_rejected", "Agent 注册失败", 401, False) from None
    response.headers["cache-control"] = "no-store"
    return _credentials_payload(credentials)


@router.post("/token/refresh")
async def refresh_agent_token(
    payload: RefreshAgentTokenRequest,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    try:
        credentials = await agent_service.refresh(
            agent_id=payload.agent_id,
            refresh_token=payload.refresh_token,
            nonce=payload.nonce,
            timestamp=payload.timestamp,
            signature_b64=payload.signature_b64,
        )
    except AgentAuthenticationRejected:
        raise ApiError("agent_authentication_failed", "Agent 认证失败", 401, False) from None
    response.headers["cache-control"] = "no-store"
    return _credentials_payload(credentials)


@router.post("/unregister")
async def unregister_agent(
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        agent = await agent_service.revoke(
            team_id=principal.team_id,
            agent_id=principal.agent_id,
        )
    except AgentNotFoundError:
        raise ApiError("agent_authentication_failed", "Agent 认证失败", 401, False) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "agent_id": str(agent.agent_id),
        "state": "revoked",
    }


@router.post("/heartbeat")
async def heartbeat(
    payload: AgentHeartbeatRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    device_directory: Annotated[DeviceDirectory, Depends(get_device_directory)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        observations = tuple(
            device_directory.sanitize_observation(
                client_ref=device.client_ref,
                serial=device.serial,
                manufacturer=device.manufacturer,
                model=device.model,
                android_release=device.android_release,
                api_level=device.api_level,
                connection_type=device.connection_type,
                adb_state=device.adb_state,
                battery_percent=device.battery_percent,
                temperature_c=device.temperature_c,
                storage_available_bytes=device.storage_available_bytes,
                property_error_code=device.property_error_code,
            )
            for device in payload.devices
        )
        receipt = await device_directory.replace_heartbeat(
            agent_id=principal.agent_id,
            heartbeat=AgentHeartbeat(
                agent_version=payload.agent_version,
                platform=payload.platform,
                hostname=payload.hostname,
                observed_at=payload.observed_at,
                clock_skew_ms=payload.clock_skew_ms,
                disk_available_bytes=payload.disk_available_bytes,
                execution_state=payload.execution_slot.state,
                execution_id=payload.execution_slot.execution_id,
            ),
            devices=observations,
        )
    except DeviceHeartbeatRejected:
        raise ApiError("heartbeat_rejected", "设备状态上报失败", 409, False) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "accepted_at": _utc(receipt.accepted_at),
        "next_heartbeat_seconds": receipt.next_heartbeat_seconds,
        "devices": [
            {
                "client_ref": str(device.client_ref),
                "device_id": str(device.device_id),
                "device_digest": device.device_digest,
            }
            for device in receipt.devices
        ],
    }


@router.get("/tasks/next")
async def poll_next_task(
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    task_service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
    wait_seconds: Annotated[int, Query(ge=0, le=20)] = 20,
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        task = await task_service.poll(
            agent_id=principal.agent_id,
            wait_seconds=wait_seconds,
        )
    except AgentTaskUnavailable:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True) from None
    response.headers["cache-control"] = "no-store"
    if task is None:
        return {
            "schema_version": "1.0",
            "action": "wait",
            "retry_after_seconds": max(1, min(20, wait_seconds or 1)),
        }
    if isinstance(task, AgentTaskCancellation):
        return _cancellation_payload(task)
    return {
        "schema_version": "1.0",
        "action": "execute",
        "snapshot_jws": task.snapshot_jws,
        "lease_expires_at": _utc(task.lease_expires_at),
        "renew_after_seconds": task.renew_after_seconds,
    }


@router.post("/tasks/{execution_id}/renew")
async def renew_task(
    execution_id: UUID,
    payload: RenewAgentTaskRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    task_service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        renewal = await task_service.renew(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
        )
    except StaleLeaseVersion:
        raise ApiError("stale_lease_version", "租约版本已经变化", 409, True) from None
    except AgentTaskNotFound:
        raise ApiError("resource_not_found", "资源不存在", 404, False) from None
    except AgentTaskUnavailable:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True) from None
    response.headers["cache-control"] = "no-store"
    if isinstance(renewal, AgentTaskCancellation):
        return _cancellation_payload(renewal)
    return {
        "schema_version": "1.0",
        "execution_id": str(renewal.execution_id),
        "lease_version": renewal.lease_version,
        "lease_expires_at": _utc(renewal.lease_expires_at),
        "renew_after_seconds": renewal.renew_after_seconds,
    }


@router.post("/tasks/{execution_id}/cancel-ack")
async def acknowledge_agent_cancellation(
    execution_id: UUID,
    payload: AcknowledgeAgentCancellationRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    task_service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
    upload_service: Annotated[AgentUploadService, Depends(get_agent_upload_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        acknowledgement = await task_service.acknowledge_cancellation(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
            reason_code=payload.reason_code,
            artifact_coordinator=upload_service,
        )
    except StaleLeaseVersion:
        raise ApiError("stale_lease_version", "租约版本已经变化", 409, True) from None
    except AgentTaskNotFound:
        raise ApiError("resource_not_found", "资源不存在", 404, False) from None
    except AgentUploadError as error:
        _raise_agent_upload_error(error)
    except AgentTaskUnavailable:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "execution_id": str(acknowledgement.execution_id),
        "analysis_id": str(acknowledgement.analysis_id),
        "lease_version": acknowledgement.lease_version,
        "state": "canceled",
        "acknowledged_at": _utc(acknowledgement.acknowledged_at),
    }


@router.post("/tasks/{execution_id}/complete")
async def complete_agent_execution(
    execution_id: UUID,
    payload: CompleteAgentExecutionRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    task_service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
    upload_service: Annotated[AgentUploadService, Depends(get_agent_upload_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        completion = await task_service.complete(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
            manifest_document=payload.model_dump(mode="json"),
            artifact_validator=upload_service,
        )
    except StaleLeaseVersion:
        raise ApiError("stale_lease_version", "租约版本已经变化", 409, True) from None
    except AgentTaskConflict:
        raise ApiError("execution_manifest_conflict", "执行结果校验失败", 409, False) from None
    except AgentTaskNotFound:
        raise ApiError("resource_not_found", "资源不存在", 404, False) from None
    except AgentUploadError as error:
        _raise_agent_upload_error(error)
    except AgentTaskUnavailable:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "execution_id": str(completion.execution_id),
        "analysis_id": str(completion.analysis_id),
        "lease_version": completion.lease_version,
        "analysis_state": completion.analysis_state,
        "accepted_at": _utc(completion.accepted_at),
    }


@router.post("/tasks/{execution_id}/uploads", status_code=201)
async def create_agent_upload(
    execution_id: UUID,
    payload: CreateAgentUploadRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    upload_service: Annotated[AgentUploadService, Depends(get_agent_upload_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        upload = await upload_service.create_upload(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
            artifact_kind=payload.artifact_kind,
            mime=payload.mime,
            size=payload.size,
            sha256_b64=payload.sha256_b64,
        )
    except AgentUploadError as error:
        _raise_agent_upload_error(error)
    response.headers["cache-control"] = "no-store"
    return _agent_upload_payload(upload)


@router.post("/tasks/{execution_id}/inputs/{artifact_id}")
async def authorize_agent_input(
    execution_id: UUID,
    artifact_id: UUID,
    payload: RenewAgentTaskRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    upload_service: Annotated[AgentUploadService, Depends(get_agent_upload_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        input_slot = await upload_service.authorize_input(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
            artifact_id=artifact_id,
        )
    except AgentUploadError as error:
        _raise_agent_upload_error(error)
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "artifact_id": str(input_slot.artifact_id),
        "mime": input_slot.mime,
        "size": input_slot.size,
        "sha256_b64": input_slot.sha256_b64,
        "download_url": input_slot.url,
        "expires_at": _utc(input_slot.expires_at),
    }


@router.post("/tasks/{execution_id}/uploads/{upload_id}/parts")
async def authorize_agent_upload_part(
    execution_id: UUID,
    upload_id: UUID,
    payload: AuthorizeAgentUploadPartRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    upload_service: Annotated[AgentUploadService, Depends(get_agent_upload_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        part = await upload_service.authorize_part(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
            upload_id=upload_id,
            part_number=payload.part_number,
        )
    except AgentUploadError as error:
        _raise_agent_upload_error(error)
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "upload_id": str(part.upload_id),
        "part_number": part.part_number,
        "put_url": part.url,
        "required_headers": part.required_headers,
        "expires_at": _utc(part.expires_at),
    }


@router.post("/tasks/{execution_id}/uploads/{upload_id}/complete")
async def complete_agent_upload(
    execution_id: UUID,
    upload_id: UUID,
    payload: CompleteAgentUploadRequest,
    request: Request,
    response: Response,
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    upload_service: Annotated[AgentUploadService, Depends(get_agent_upload_service)],
) -> dict[str, object]:
    principal = await _authenticate_access(request, agent_service)
    try:
        upload = await upload_service.complete_upload(
            agent_id=principal.agent_id,
            execution_id=execution_id,
            lease_version=payload.lease_version,
            upload_id=upload_id,
            parts=tuple(
                MultipartPart(part_number=part.part_number, etag=part.etag)
                for part in payload.parts
            ),
        )
    except AgentUploadError as error:
        _raise_agent_upload_error(error)
    response.headers["cache-control"] = "no-store"
    return _agent_upload_payload(upload)


__all__ = ["router"]
