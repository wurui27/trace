"""Authenticated API for server-owned Android memory capture manifests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from perfpilot_api.api.auth import get_auth_service, proxy_router_dependencies
from perfpilot_api.engines.android_memory_contracts import MemoryArtifactRef, MemorySubject
from perfpilot_api.errors import ApiError
from perfpilot_api.security.csrf import OriginNotAllowedError, require_allowed_origin
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.auth import (
    AuthService,
    InvalidCsrfError,
    InvalidSessionError,
    RoleForbiddenError,
    TeamAccessNotFoundError,
)
from perfpilot_api.services.memory_analyses import (
    MemoryCaptureConflictError,
    MemoryCaptureInvalidRequestError,
    MemoryCaptureNotFoundError,
    MemoryCaptureService,
    MemoryCaptureUnavailableError,
)


class CreateMemoryCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    phase: Literal["single", "before", "after", "cooldown"]
    source: Literal["manual_upload"]
    captured_at: datetime | None = None
    subject: MemorySubject
    artifacts: tuple[MemoryArtifactRef, ...] = Field(min_length=1, max_length=2048)

    @field_validator("captured_at", mode="before")
    @classmethod
    def _require_timestamp_text_or_datetime(cls, value: object) -> object:
        if value is not None and not isinstance(value, str | datetime):
            raise ValueError("captured_at must be an RFC3339 string or datetime")
        return value

    @field_validator("captured_at")
    @classmethod
    def _require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be timezone-aware UTC")
        return value.astimezone(UTC)


def get_memory_capture_service(request: Request) -> MemoryCaptureService:
    service: MemoryCaptureService | None = request.app.state.memory_capture_service
    if service is None:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return service


def _require_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        raise ApiError("origin_not_allowed", "请求来源不允许", 403, False)
    try:
        require_allowed_origin(origins[0], request.app.state.settings.allowed_origins)
    except OriginNotAllowedError:
        raise ApiError("origin_not_allowed", "请求来源不允许", 403, False) from None


async def _authorize_write(
    *,
    request: Request,
    auth_service: AuthService,
    team_id: UUID,
) -> None:
    _require_origin(request)
    csrf_values = request.headers.getlist("x-csrf-token")
    if len(csrf_values) != 1:
        raise ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False)
    try:
        await auth_service.authorize_team_request(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=csrf_values[0],
            team_id=team_id,
            access="write",
        )
    except InvalidSessionError:
        raise ApiError("unauthenticated", "需要重新登录", 401, False) from None
    except InvalidCsrfError:
        raise ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False) from None
    except TeamAccessNotFoundError:
        raise ApiError("resource_not_found", "资源不存在", 404, False) from None
    except RoleForbiddenError:
        raise ApiError("role_forbidden", "当前团队角色无权执行此操作", 403, False) from None


def _capture_error(error: Exception) -> ApiError:
    if isinstance(error, MemoryCaptureInvalidRequestError):
        return ApiError("request_validation_failed", "请求参数校验失败", 422, False)
    if isinstance(error, MemoryCaptureNotFoundError):
        return ApiError("resource_not_found", "资源不存在", 404, False)
    if isinstance(error, MemoryCaptureConflictError):
        return ApiError("idempotency_conflict", "请求与已有采集清单不匹配", 409, False)
    return ApiError("service_unavailable", "服务暂时不可用", 503, True)


router = APIRouter(
    prefix="/v1/teams/{team_id}/analyses/{analysis_id}",
    dependencies=proxy_router_dependencies(),
)


@router.post("/memory-captures", status_code=201)
async def create_memory_capture(
    team_id: UUID,
    analysis_id: UUID,
    payload: CreateMemoryCaptureRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    service: Annotated[MemoryCaptureService, Depends(get_memory_capture_service)],
) -> dict[str, object]:
    await _authorize_write(request=request, auth_service=auth_service, team_id=team_id)
    try:
        created = await service.create_capture(
            team_id=team_id,
            analysis_id=analysis_id,
            phase=payload.phase,
            source=payload.source,
            captured_at=payload.captured_at,
            subject=payload.subject,
            artifacts=payload.artifacts,
        )
    except (
        MemoryCaptureInvalidRequestError,
        MemoryCaptureNotFoundError,
        MemoryCaptureConflictError,
        MemoryCaptureUnavailableError,
    ) as error:
        raise _capture_error(error) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "capture_id": str(created.manifest.capture_id),
        "manifest_artifact_id": str(created.artifact_id),
        "manifest_sha256": created.manifest_sha256,
        "state": "created",
    }


__all__ = ["CreateMemoryCaptureRequest", "get_memory_capture_service", "router"]
