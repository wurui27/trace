from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from perfpilot_api.api.auth import get_auth_service, proxy_router_dependencies
from perfpilot_api.errors import ApiError
from perfpilot_api.security.csrf import OriginNotAllowedError, require_allowed_origin
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.analyses import (
    AnalysisIdempotencyConflictError,
    AnalysisInvalidRequestError,
    AnalysisNotFoundError,
    AnalysisQueueLimitError,
    AnalysisService,
    AnalysisUnavailableError,
    AnalysisView,
    ApkInspectionError,
    ApkInspectionUnavailableError,
    ReportNotAvailableError,
    SampleVerdictCounts,
    ScenarioView,
    StaleTaskVersionError,
)
from perfpilot_api.services.auth import (
    AuthService,
    InvalidCsrfError,
    InvalidSessionError,
    RoleForbiddenError,
    TeamAccessNotFoundError,
)
from perfpilot_api.services.uploads import UploadSlot

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024
_SHA256_PATTERN = r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$"
_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,255}$"


class ApkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["apk"]
    mime: Literal["application/vnd.android.package-archive"]
    size: int = Field(strict=True, ge=1, le=_MAX_UPLOAD_BYTES)
    sha256_b64: str = Field(pattern=_SHA256_PATTERN)


class CreateAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    analysis_mode: Literal["device"]
    scenarios: tuple[
        Literal["cold_start"],
        Literal["scroll"],
        Literal["memory_cycle"],
    ]
    apk: ApkInput


def get_analysis_service(request: Request) -> AnalysisService:
    service: AnalysisService | None = request.app.state.analysis_service
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
) -> Any:
    _require_origin(request)
    csrf_values = request.headers.getlist("x-csrf-token")
    if len(csrf_values) != 1:
        raise ApiError("csrf_validation_failed", "CSRF 校验失败", 403, False)
    try:
        return await auth_service.authorize_team_request(
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


def analysis_error(error: Exception) -> ApiError:
    if isinstance(error, AnalysisInvalidRequestError):
        return ApiError("request_validation_failed", "请求参数校验失败", 422, False)
    if isinstance(error, ApkInspectionError):
        return ApiError(error.code, "APK 无法用于自动化分析", 422, False)
    if isinstance(error, AnalysisNotFoundError):
        return ApiError("resource_not_found", "资源不存在", 404, False)
    if isinstance(error, ReportNotAvailableError):
        return ApiError("report_not_available", "分析报告尚不可用", 404, False)
    if isinstance(error, AnalysisIdempotencyConflictError):
        return ApiError("idempotency_conflict", "幂等键与请求不匹配", 409, False)
    if isinstance(error, StaleTaskVersionError):
        return ApiError("stale_task_version", "任务版本已经变化", 409, True)
    if isinstance(error, AnalysisQueueLimitError):
        return ApiError("team_queue_limit", "团队排队任务已达上限", 429, True)
    if isinstance(error, (ApkInspectionUnavailableError, AnalysisUnavailableError)):
        return ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return ApiError("service_unavailable", "服务暂时不可用", 503, True)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _failure(code: str | None) -> dict[str, object] | None:
    if code is None:
        return None
    return {"code": code, "message": "任务未能完成", "retryable": False}


def _verdicts(counts: SampleVerdictCounts) -> dict[str, int]:
    if (
        min(
            counts.valid,
            counts.invalid,
            counts.pending,
            counts.validation_error,
            counts.total,
        )
        < 0
        or counts.valid + counts.invalid + counts.pending + counts.validation_error != counts.total
    ):
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return {
        "valid": counts.valid,
        "invalid": counts.invalid,
        "pending": counts.pending,
        "validation_error": counts.validation_error,
        "total": counts.total,
    }


def _scenario(item: ScenarioView) -> dict[str, object]:
    return {
        "scenario_job_id": (
            str(item.scenario_job_id) if item.scenario_job_id is not None else None
        ),
        "scenario_type": item.scenario_type,
        "state": item.state,
        "version": item.version,
        "device_group_id": (
            str(item.device_group_id) if item.device_group_id is not None else None
        ),
        "sample_verdict_counts": _verdicts(item.sample_verdict_counts),
        "started_at": _utc(item.started_at) if item.started_at is not None else None,
        "completed_at": (_utc(item.completed_at) if item.completed_at is not None else None),
        "failure": _failure(item.failure_code),
    }


def _apk_upload(slot: UploadSlot) -> dict[str, object]:
    common: dict[str, object] = {
        "state": slot.state,
        "upload_id": str(slot.upload_id),
        "artifact_kind": slot.artifact_kind,
        "mime": slot.mime,
        "size": slot.size,
        "sha256_b64": slot.sha256_b64,
    }
    if slot.state == "pending":
        common["expires_at"] = _utc(slot.expires_at)
        if slot.put_url is not None:
            if not slot.required_headers:
                raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
            common["put_url"] = slot.put_url
            common["required_headers"] = dict(slot.required_headers)
        elif slot.required_headers:
            raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    elif slot.state == "finalized":
        if slot.finalized_at is None:
            raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
        common["artifact_id"] = str(slot.artifact_id)
        common["finalized_at"] = _utc(slot.finalized_at)
    else:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    return common


def analysis_response(view: AnalysisView) -> dict[str, object]:
    if view.apk_upload is None:
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    if (
        view.analysis_mode != "device"
        or tuple(item.scenario_type for item in view.scenarios)
        != ("cold_start", "scroll", "memory_cycle")
        or (view.application_version_id is None) != (view.application_metadata is None)
    ):
        raise ApiError("service_unavailable", "服务暂时不可用", 503, True)
    metadata = view.application_metadata
    application_metadata: dict[str, object] | None = None
    if metadata is not None:
        application_metadata = {
            "package_name": metadata.package_name,
            "version_name": metadata.version_name,
            "version_code": metadata.version_code,
            "launch_activity": metadata.launch_activity,
            "min_sdk": metadata.min_sdk,
            "target_sdk": metadata.target_sdk,
            "supported_abis": list(metadata.supported_abis),
            "has_native_libraries": metadata.has_native_libraries,
        }
    active_lease: dict[str, object] | None = None
    if view.active_lease is not None:
        active_lease = {
            "lease_id": str(view.active_lease.lease_id),
            "device_id": str(view.active_lease.device_id),
            "state": "active",
            "expires_at": _utc(view.active_lease.expires_at),
        }
    return {
        "schema_version": "1.0",
        "analysis_id": str(view.analysis_id),
        "team_id": str(view.team_id),
        "analysis_mode": view.analysis_mode,
        "state": view.state,
        "version": view.version,
        "application_version_id": (
            str(view.application_version_id) if view.application_version_id is not None else None
        ),
        "application_metadata": application_metadata,
        "apk_upload": _apk_upload(view.apk_upload),
        "scenarios": [_scenario(item) for item in view.scenarios],
        "sample_verdict_counts": _verdicts(view.sample_verdict_counts),
        "active_lease": active_lease,
        "report_available": view.report_available,
        "created_at": _utc(view.created_at),
        "started_at": _utc(view.started_at) if view.started_at is not None else None,
        "completed_at": (_utc(view.completed_at) if view.completed_at is not None else None),
        "failure": _failure(view.failure_code),
    }


router = APIRouter(
    prefix="/v1/teams/{team_id}/analyses",
    dependencies=proxy_router_dependencies(),
)


@router.post("", status_code=201)
async def create_analysis(
    team_id: UUID,
    payload: CreateAnalysisRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    analysis_service: Annotated[AnalysisService, Depends(get_analysis_service)],
) -> dict[str, object]:
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise ApiError("request_validation_failed", "请求参数校验失败", 422, False)
    principal = await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="write",
    )
    try:
        view = await analysis_service.create_device_analysis(
            team_id=team_id,
            requested_by_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            scenarios=payload.scenarios,
            apk_mime=payload.apk.mime,
            apk_size=payload.apk.size,
            apk_sha256_b64=payload.apk.sha256_b64,
        )
    except Exception as error:
        if not isinstance(
            error,
            (
                AnalysisInvalidRequestError,
                AnalysisNotFoundError,
                AnalysisIdempotencyConflictError,
                AnalysisQueueLimitError,
                StaleTaskVersionError,
                AnalysisUnavailableError,
            ),
        ):
            raise
        raise analysis_error(error) from None
    response.headers["cache-control"] = "no-store"
    return analysis_response(view)


@router.get("/{analysis_id}")
async def get_analysis(
    team_id: UUID,
    analysis_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    analysis_service: Annotated[AnalysisService, Depends(get_analysis_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="read",
    )
    try:
        view = await analysis_service.get_analysis(
            team_id=team_id,
            analysis_id=analysis_id,
        )
    except (AnalysisNotFoundError, AnalysisUnavailableError) as error:
        raise analysis_error(error) from None
    response.headers["cache-control"] = "no-store"
    return analysis_response(view)


@router.get("/{analysis_id}/report")
async def get_analysis_report(
    team_id: UUID,
    analysis_id: UUID,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    analysis_service: Annotated[AnalysisService, Depends(get_analysis_service)],
) -> dict[str, object]:
    await _authorize_team(
        request=request,
        auth_service=auth_service,
        team_id=team_id,
        access="read",
    )
    try:
        report = await analysis_service.get_report(
            team_id=team_id,
            analysis_id=analysis_id,
        )
    except (
        AnalysisNotFoundError,
        ReportNotAvailableError,
        AnalysisUnavailableError,
    ) as error:
        raise analysis_error(error) from None
    response.headers["cache-control"] = "no-store"
    return report


__all__ = [
    "analysis_error",
    "analysis_response",
    "get_analysis_service",
    "router",
]
