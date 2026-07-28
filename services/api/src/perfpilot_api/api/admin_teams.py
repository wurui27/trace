from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from perfpilot_api.api.auth import proxy_router_dependencies
from perfpilot_api.errors import ApiError
from perfpilot_api.security.csrf import OriginNotAllowedError, require_allowed_origin
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.provisioning import (
    AdminCsrfInvalid,
    AdminIdempotencyConflict,
    AdminNotPlatformAdministrator,
    AdminOwnerNotFound,
    AdminRequestInvalid,
    AdminSessionInvalid,
    AdminTeamNotFound,
    AdminTeamService,
)

TeamName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class CreateTeamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: TeamName
    owner_user_id: UUID


def get_admin_team_service(request: Request) -> AdminTeamService:
    service: AdminTeamService | None = request.app.state.admin_team_service
    if service is None:
        raise ApiError(
            code="service_unavailable",
            message="服务暂时不可用",
            status_code=503,
            retryable=True,
        )
    return service


def _require_origin(request: Request) -> None:
    try:
        require_allowed_origin(
            request.headers.get("origin"),
            request.app.state.settings.allowed_origins,
        )
    except OriginNotAllowedError:
        raise ApiError(
            code="origin_not_allowed",
            message="请求来源不允许",
            status_code=403,
            retryable=False,
        ) from None


def _admin_error(error: Exception) -> ApiError:
    if isinstance(error, AdminSessionInvalid):
        return ApiError("unauthenticated", "需要重新登录", 401, False)
    if isinstance(error, AdminCsrfInvalid):
        return ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False)
    if isinstance(error, AdminNotPlatformAdministrator):
        return ApiError("platform_admin_required", "需要平台管理员权限", 403, False)
    if isinstance(error, AdminOwnerNotFound):
        return ApiError("user_not_found", "用户不存在", 404, False)
    if isinstance(error, AdminTeamNotFound):
        return ApiError("team_not_found", "团队不存在", 404, False)
    if isinstance(error, AdminIdempotencyConflict):
        return ApiError("idempotency_conflict", "幂等键与请求不匹配", 409, False)
    return ApiError("request_validation_failed", "请求参数校验失败", 422, False)


router = APIRouter(
    prefix="/v1/admin/teams",
    dependencies=proxy_router_dependencies(),
)


@router.get("/{team_id}")
async def get_team_status(
    team_id: UUID,
    request: Request,
    service: Annotated[AdminTeamService, Depends(get_admin_team_service)],
) -> dict[str, object]:
    try:
        result = await service.get_team_status(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            team_id=team_id,
        )
    except (
        AdminSessionInvalid,
        AdminNotPlatformAdministrator,
        AdminTeamNotFound,
    ) as error:
        raise _admin_error(error) from None
    return {
        "schema_version": "1.0",
        "team": {
            "id": str(result.team_id),
            "name": result.team_name,
            "state": result.team_state,
        },
        "provisioning": {
            "state": result.resource_state,
            "provisioning_step": result.provisioning_step,
            "transition_kind": result.transition_kind,
            "transition_step": result.transition_step,
            "retry_count": result.retry_count,
            "next_retry_at": (
                result.next_retry_at.astimezone(UTC).isoformat()
                if result.next_retry_at is not None
                else None
            ),
            "last_error_code": result.last_error_code,
            "resource_version": result.resource_version,
            "credential_version": result.credential_version,
            "write_paused": result.write_paused,
        },
    }


@router.post("", status_code=202)
async def create_team(
    payload: CreateTeamRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
    service: Annotated[AdminTeamService, Depends(get_admin_team_service)],
) -> dict[str, object]:
    _require_origin(request)
    raw_idempotency_headers = request.headers.getlist("idempotency-key")
    if len(raw_idempotency_headers) != 1:
        raise ApiError(
            code="request_validation_failed",
            message="请求参数校验失败",
            status_code=422,
            retryable=False,
        )
    try:
        result = await service.create_team(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=request.headers.get("x-csrf-token", ""),
            idempotency_key=idempotency_key,
            name=payload.name,
            owner_user_id=payload.owner_user_id,
            request_id=request.state.request_id,
        )
    except (
        AdminSessionInvalid,
        AdminCsrfInvalid,
        AdminNotPlatformAdministrator,
        AdminOwnerNotFound,
        AdminIdempotencyConflict,
        AdminRequestInvalid,
    ) as error:
        raise _admin_error(error) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "team": {
            "id": str(result.team_id),
            "name": result.team_name,
            "provisioning_state": result.resource_state,
        },
    }
