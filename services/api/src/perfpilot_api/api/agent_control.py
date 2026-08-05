from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from perfpilot_api.api.agents import get_agent_service
from perfpilot_api.errors import ApiError
from perfpilot_api.services.agents import (
    AgentAuthenticationRejected,
    AgentRegistration,
    AgentRegistrationRejected,
    AgentService,
    IssuedAgentCredentials,
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


__all__ = ["router"]
