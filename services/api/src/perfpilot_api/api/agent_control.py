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
    AgentTaskNotFound,
    AgentTaskService,
    AgentTaskUnavailable,
    StaleLeaseVersion,
)

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
    return {
        "schema_version": "1.0",
        "execution_id": str(renewal.execution_id),
        "lease_version": renewal.lease_version,
        "lease_expires_at": _utc(renewal.lease_expires_at),
        "renew_after_seconds": renewal.renew_after_seconds,
    }


__all__ = ["router"]
