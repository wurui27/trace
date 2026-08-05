from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from perfpilot_api.api.auth import (
    get_auth_service,
    proxy_router_dependencies,
)
from perfpilot_api.errors import ApiError
from perfpilot_api.security.csrf import OriginNotAllowedError, require_allowed_origin
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.agents import (
    AgentInvalidRequestError,
    AgentNameConflictError,
    AgentNotFoundError,
    AgentService,
    AgentView,
)
from perfpilot_api.services.auth import (
    AuthService,
    InvalidCsrfError,
    InvalidSessionError,
    RoleForbiddenError,
    TeamAccessNotFoundError,
)
from perfpilot_api.services.device_directory import DeviceDirectory, DeviceView


class CreateRegistrationCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    name: str = Field(min_length=1, max_length=200)


class RenameAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    name: str = Field(min_length=1, max_length=200)


class RevokeAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]


def get_agent_service(request: Request) -> AgentService:
    service: AgentService | None = request.app.state.agent_service
    if service is None:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return service


def get_device_directory(request: Request) -> DeviceDirectory:
    directory: DeviceDirectory | None = request.app.state.device_directory
    if directory is None:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return directory


def _require_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        raise ApiError("origin_not_allowed", "请求来源不允许", 403, False)
    try:
        require_allowed_origin(origins[0], request.app.state.settings.allowed_origins)
    except OriginNotAllowedError:
        raise ApiError("origin_not_allowed", "请求来源不允许", 403, False) from None


async def _authorize_team(
    *,
    request: Request,
    auth_service: AuthService,
    team_id: UUID,
    access: Literal["read", "write"],
    owner_required: bool,
):
    _require_origin(request)
    csrf_values = request.headers.getlist("x-csrf-token")
    if len(csrf_values) != 1:
        raise ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False)
    try:
        context = await auth_service.authorize_team_request(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=csrf_values[0],
            team_id=team_id,
            access=access,
        )
    except InvalidSessionError:
        raise ApiError("unauthenticated", "需要重新登录", 401, False) from None
    except InvalidCsrfError:
        raise ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False) from None
    except TeamAccessNotFoundError:
        raise ApiError("resource_not_found", "资源不存在", 404, False) from None
    except RoleForbiddenError:
        raise ApiError("role_forbidden", "当前团队角色无权执行此操作", 403, False) from None
    if owner_required and context.role != "team_owner":
        raise ApiError("role_forbidden", "当前团队角色无权执行此操作", 403, False)
    return context


def _agent_operation_error(error: Exception) -> ApiError:
    if isinstance(error, AgentNotFoundError):
        return ApiError("resource_not_found", "资源不存在", 404, False)
    if isinstance(error, AgentNameConflictError):
        return ApiError("agent_name_conflict", "Agent 名称已被使用", 409, False)
    if isinstance(error, AgentInvalidRequestError):
        return ApiError("request_validation_failed", "请求参数校验失败", 422, False)
    return ApiError("service_unavailable", "服务暂时不可用", 503, True)


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _agent_payload(agent: AgentView) -> dict[str, object]:
    return {
        "agent_id": str(agent.agent_id),
        "name": agent.name,
        "platform": agent.platform,
        "agent_version": agent.agent_version,
        "hostname": agent.hostname,
        "os_version": agent.os_version,
        "state": agent.state,
        "last_heartbeat_at": _utc(agent.last_heartbeat_at),
        "created_at": _utc(agent.created_at),
        "updated_at": _utc(agent.updated_at),
    }


def _device_payload(device: DeviceView) -> dict[str, object]:
    return {
        "device_id": str(device.device_id),
        "agent_id": str(device.agent_id),
        "agent_name": device.agent_name,
        "serial_suffix": device.serial_suffix,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "android_release": device.android_release,
        "api_level": device.api_level,
        "connection_type": device.connection_type,
        "adb_state": device.adb_state,
        "state": device.state,
        "last_seen_at": _utc(device.last_seen_at),
    }


router = APIRouter(
    prefix="/v1/teams",
    dependencies=proxy_router_dependencies(),
)


@router.post("/{team_id}/agents/registration-codes", status_code=201)
async def create_registration_code(
    team_id: UUID,
    payload: CreateRegistrationCodeRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    context = await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="write",
        owner_required=True,
    )
    try:
        issued = await agent_service.create_registration_code(
            team_id=team_id,
            owner_user_id=context.user_id,
            name=payload.name,
        )
    except (AgentNameConflictError, AgentInvalidRequestError) as error:
        raise _agent_operation_error(error) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "agent_id": str(issued.agent_id),
        "registration_code": issued.registration_code,
        "expires_at": _utc(issued.expires_at),
    }


@router.get("/{team_id}/agents")
async def list_agents(
    team_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="read",
        owner_required=False,
    )
    agents = await agent_service.list_agents(team_id=team_id)
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "agents": [_agent_payload(agent) for agent in agents],
    }


@router.get("/{team_id}/devices")
async def list_devices(
    team_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    device_directory: Annotated[DeviceDirectory, Depends(get_device_directory)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="read",
        owner_required=False,
    )
    devices = await device_directory.list_devices(team_id=team_id)
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "devices": [_device_payload(device) for device in devices],
    }


@router.patch("/{team_id}/agents/{agent_id}")
async def rename_agent(
    team_id: UUID,
    agent_id: UUID,
    payload: RenameAgentRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="write",
        owner_required=True,
    )
    try:
        agent = await agent_service.rename(
            team_id=team_id,
            agent_id=agent_id,
            name=payload.name,
        )
    except (AgentNotFoundError, AgentNameConflictError, AgentInvalidRequestError) as error:
        raise _agent_operation_error(error) from None
    response.headers["cache-control"] = "no-store"
    return {"schema_version": "1.0", "agent": _agent_payload(agent)}


@router.post("/{team_id}/agents/{agent_id}/revoke")
async def revoke_agent(
    team_id: UUID,
    agent_id: UUID,
    payload: RevokeAgentRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="write",
        owner_required=True,
    )
    try:
        agent = await agent_service.revoke(team_id=team_id, agent_id=agent_id)
    except AgentNotFoundError as error:
        raise _agent_operation_error(error) from None
    response.headers["cache-control"] = "no-store"
    return {"schema_version": "1.0", "agent": _agent_payload(agent)}


__all__ = ["get_agent_service", "get_device_directory", "router"]
