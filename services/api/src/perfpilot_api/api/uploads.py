from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from perfpilot_api.api.auth import (
    get_auth_service,
    proxy_router_dependencies,
)
from perfpilot_api.api.analyses import analysis_error
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
from perfpilot_api.services.analyses import (
    AnalysisError,
    AnalysisService,
)
from perfpilot_api.services.uploads import (
    DownloadAuthorization,
    UploadExpiredError,
    UploadIdempotencyConflictError,
    UploadInvalidRequestError,
    UploadMismatchError,
    UploadNotFoundError,
    UploadService,
    UploadSlot,
    UploadUnavailableError,
)

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024
_ARTIFACT_KIND_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_MIME_PATTERN = (
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_SHA256_PATTERN = r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$"
_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,255}$"


class CreateUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: str = Field(pattern=_ARTIFACT_KIND_PATTERN)
    mime: str = Field(min_length=3, max_length=255, pattern=_MIME_PATTERN)
    size: int = Field(strict=True, ge=1, le=_MAX_UPLOAD_BYTES)
    sha256_b64: str = Field(pattern=_SHA256_PATTERN)


class FinalizeUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256_b64: str = Field(pattern=_SHA256_PATTERN)
    size: int = Field(strict=True, ge=1, le=_MAX_UPLOAD_BYTES)


def get_upload_service(request: Request) -> UploadService:
    service: UploadService | None = request.app.state.upload_service
    if service is None:
        raise ApiError(
            code="service_unavailable",
            message="服务暂时不可用",
            status_code=503,
            retryable=True,
        )
    return service


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


def _upload_error(error: Exception, *, downloading: bool = False) -> ApiError:
    if isinstance(error, UploadInvalidRequestError):
        return ApiError("request_validation_failed", "请求参数校验失败", 422, False)
    if isinstance(error, UploadNotFoundError):
        return ApiError("resource_not_found", "资源不存在", 404, False)
    if isinstance(error, UploadIdempotencyConflictError):
        return ApiError("idempotency_conflict", "幂等键与请求不匹配", 409, False)
    if isinstance(error, UploadMismatchError):
        return ApiError("upload_mismatch", "上传内容与授权不匹配", 409, False)
    if isinstance(error, UploadExpiredError):
        code = "artifact_expired" if downloading else "upload_authorization_expired"
        return ApiError(code, "上传授权或产物已过期", 410, False)
    return ApiError("artifact_store_unavailable", "对象存储暂时不可用", 503, True)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _slot_response(slot: UploadSlot) -> dict[str, object]:
    common: dict[str, object] = {
        "state": slot.state,
        "upload_id": str(slot.upload_id),
        "artifact_kind": slot.artifact_kind,
        "mime": slot.mime,
        "size": slot.size,
        "sha256_b64": slot.sha256_b64,
    }
    if slot.state == "pending":
        if slot.put_url is None:
            raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
        common.update(
            {
                "expires_at": _utc(slot.expires_at),
                "put_url": slot.put_url,
                "required_headers": dict(slot.required_headers),
            }
        )
    elif slot.state == "finalized":
        if slot.finalized_at is None:
            raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
        common.update(
            {
                "artifact_id": str(slot.artifact_id),
                "finalized_at": _utc(slot.finalized_at),
            }
        )
    else:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return {"schema_version": "1.0", "upload": common}


router = APIRouter(
    prefix="/v1/teams/{team_id}/analyses/{analysis_id}",
    dependencies=proxy_router_dependencies(),
)


@router.post("/uploads", status_code=201)
async def create_upload_slot(
    team_id: UUID,
    analysis_id: UUID,
    payload: CreateUploadRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    upload_service: Annotated[UploadService, Depends(get_upload_service)],
) -> dict[str, object]:
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise ApiError("request_validation_failed", "请求参数校验失败", 422, False)
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="write",
    )
    try:
        slot = await upload_service.create_slot(
            team_id=team_id,
            analysis_id=analysis_id,
            idempotency_key=idempotency_key,
            artifact_kind=payload.artifact_kind,
            mime=payload.mime,
            size=payload.size,
            sha256_b64=payload.sha256_b64,
        )
    except (
        UploadInvalidRequestError,
        UploadNotFoundError,
        UploadIdempotencyConflictError,
        UploadMismatchError,
        UploadExpiredError,
        UploadUnavailableError,
    ) as error:
        raise _upload_error(error) from None
    response.headers["cache-control"] = "no-store"
    return _slot_response(slot)


@router.post("/finalize-upload")
async def finalize_upload(
    team_id: UUID,
    analysis_id: UUID,
    payload: FinalizeUploadRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="write",
    )
    analysis_service: AnalysisService | None = request.app.state.analysis_service
    upload_service: UploadService | None = request.app.state.upload_service
    try:
        if analysis_service is not None:
            slot = await analysis_service.finalize_upload(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=payload.upload_id,
                caller_sha256_b64=payload.sha256_b64,
                caller_size=payload.size,
            )
        elif request.app.state.testing and upload_service is not None:
            slot = await upload_service.finalize(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=payload.upload_id,
                caller_sha256_b64=payload.sha256_b64,
                caller_size=payload.size,
            )
        else:
            raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    except AnalysisError as error:
        raise analysis_error(error) from None
    except (
        UploadInvalidRequestError,
        UploadNotFoundError,
        UploadIdempotencyConflictError,
        UploadMismatchError,
        UploadExpiredError,
        UploadUnavailableError,
    ) as error:
        raise _upload_error(error) from None
    response.headers["cache-control"] = "no-store"
    return _slot_response(slot)


@router.post("/artifacts/{artifact_id}/download")
async def download_artifact(
    team_id: UUID,
    analysis_id: UUID,
    artifact_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    upload_service: Annotated[UploadService, Depends(get_upload_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="read",
    )
    try:
        authorization: DownloadAuthorization = await upload_service.download(
            team_id=team_id,
            analysis_id=analysis_id,
            artifact_id=artifact_id,
        )
    except (
        UploadInvalidRequestError,
        UploadNotFoundError,
        UploadIdempotencyConflictError,
        UploadMismatchError,
        UploadExpiredError,
        UploadUnavailableError,
    ) as error:
        raise _upload_error(error, downloading=True) from None
    response.headers["cache-control"] = "no-store"
    response.headers["x-content-type-options"] = "nosniff"
    return {
        "schema_version": "1.0",
        "download": {
            "artifact_id": str(authorization.artifact_id),
            "url": authorization.url,
            "expires_at": _utc(authorization.expires_at),
        },
    }
