from collections.abc import Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = request.state.request_id
    response = JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "schema_version": "1.0",
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": request_id,
            },
        },
    )
    response.headers["x-request-id"] = request_id
    return response


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
    )


async def request_validation_error_handler(
    request: Request,
    _: RequestValidationError,
) -> JSONResponse:
    return _error_response(
        request,
        status_code=422,
        code="request_validation_failed",
        message="请求参数校验失败",
    )


async def internal_server_error_handler(
    request: Request,
    _: Exception,
) -> JSONResponse:
    return _error_response(
        request,
        status_code=500,
        code="internal_server_error",
        message="服务暂时不可用",
        retryable=True,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return _error_response(
            request,
            status_code=404,
            code="route_not_found",
            message="请求的接口不存在",
            headers=exc.headers,
        )
    return _error_response(
        request,
        status_code=exc.status_code,
        code="http_error",
        message="请求处理失败",
        headers=exc.headers,
    )
