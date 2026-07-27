from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from perfpilot_api.api.auth import get_auth_service, proxy_router_dependencies
from perfpilot_api.errors import ApiError
from perfpilot_api.security.csrf import OriginNotAllowedError, require_allowed_origin
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.auth import (
    AuthService,
    InvalidCsrfError,
    InvalidSessionError,
    LastOwnerError,
    MemberNotFoundError,
    RoleForbiddenError,
    TargetUserNotFoundError,
    TeamAccessNotFoundError,
)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["team_owner", "team_member", "team_viewer"]


class UpdateMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["team_owner", "team_member", "team_viewer"]


_MEMBER_OPERATION_ERRORS = (
    InvalidSessionError,
    InvalidCsrfError,
    TeamAccessNotFoundError,
    RoleForbiddenError,
    TargetUserNotFoundError,
    MemberNotFoundError,
    LastOwnerError,
)


def _require_member_origin(request: Request) -> None:
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


def _member_api_error(exc: Exception) -> ApiError:
    if isinstance(exc, InvalidSessionError):
        return ApiError("unauthenticated", "需要重新登录", 401, False)
    if isinstance(exc, InvalidCsrfError):
        return ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False)
    if isinstance(exc, TeamAccessNotFoundError):
        return ApiError("team_not_found", "团队不存在", 404, False)
    if isinstance(exc, RoleForbiddenError):
        return ApiError(
            "role_forbidden",
            "当前团队角色无权执行此操作",
            403,
            False,
        )
    if isinstance(exc, TargetUserNotFoundError):
        return ApiError("user_not_found", "用户不存在", 404, False)
    if isinstance(exc, MemberNotFoundError):
        return ApiError("member_not_found", "团队成员不存在", 404, False)
    return ApiError(
        "last_owner_required",
        "团队必须保留至少一名所有者",
        409,
        False,
    )


router = APIRouter(
    prefix="/v1/teams",
    dependencies=proxy_router_dependencies(),
)


@router.get("/{team_id}/members")
async def list_members(
    team_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, object]:
    try:
        members = await auth_service.list_team_members(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            team_id=team_id,
        )
    except InvalidSessionError:
        raise ApiError(
            code="unauthenticated",
            message="需要重新登录",
            status_code=401,
            retryable=False,
        ) from None
    except TeamAccessNotFoundError:
        raise ApiError(
            code="team_not_found",
            message="团队不存在",
            status_code=404,
            retryable=False,
        ) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "members": [
            {
                "id": str(member.id),
                "user": {
                    "id": str(member.user_id),
                    "username": member.username,
                },
                "role": member.role,
            }
            for member in members
        ],
    }


@router.post("/{team_id}/members", status_code=201)
async def add_member(
    team_id: UUID,
    payload: AddMemberRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, object]:
    _require_member_origin(request)
    try:
        member = await auth_service.add_team_member(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=request.headers.get("x-csrf-token", ""),
            team_id=team_id,
            user_id=payload.user_id,
            role=payload.role,
        )
    except _MEMBER_OPERATION_ERRORS as exc:
        raise _member_api_error(exc) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "member": {
            "id": str(member.id),
            "user": {"id": str(member.user_id), "username": member.username},
            "role": member.role,
        },
    }


@router.patch("/{team_id}/members/{member_id}")
async def update_member(
    team_id: UUID,
    member_id: UUID,
    payload: UpdateMemberRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, object]:
    _require_member_origin(request)
    try:
        member = await auth_service.update_team_member(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=request.headers.get("x-csrf-token", ""),
            team_id=team_id,
            member_id=member_id,
            role=payload.role,
        )
    except _MEMBER_OPERATION_ERRORS as exc:
        raise _member_api_error(exc) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "member": {
            "id": str(member.id),
            "user": {"id": str(member.user_id), "username": member.username},
            "role": member.role,
        },
    }


@router.delete("/{team_id}/members/{member_id}", status_code=204)
async def delete_member(
    team_id: UUID,
    member_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    _require_member_origin(request)
    try:
        await auth_service.delete_team_member(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=request.headers.get("x-csrf-token", ""),
            team_id=team_id,
            member_id=member_id,
        )
    except _MEMBER_OPERATION_ERRORS as exc:
        raise _member_api_error(exc) from None
    response.status_code = 204
