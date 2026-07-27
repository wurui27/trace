from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from perfpilot_api.errors import ApiError
from perfpilot_api.security.csrf import OriginNotAllowedError, require_allowed_origin
from perfpilot_api.security.proxy_signature import (
    ProxySignatureError,
    authenticate_proxy_request,
    reserve_proxy_request,
    verify_proxy_client_identity,
)
from perfpilot_api.security.sessions import (
    COOKIE_NAME,
    clear_session_cookie,
    set_session_cookie,
)
from perfpilot_api.services.auth import (
    AuthService,
    InvalidCsrfError,
    InvalidCredentialsError,
    InvalidSessionError,
    LoginRateLimitedError,
    PreAuthSessionRateLimitedError,
)

_MAX_PROXY_BODY_BYTES = 1024 * 1024
_PROXY_AUTHENTICATED_STATE_KEY = "perfpilot_proxy_authenticated"
PROXY_CLIENT_IDENTITY_STATE_KEY = "perfpilot_proxy_client_identity"
_PROXY_PROTECTED_PATH_PREFIXES = (
    "/v1/admin",
    "/v1/auth",
    "/v1/me",
    "/v1/teams",
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=4096)


def _proxy_failure() -> ApiError:
    return ApiError(
        code="proxy_authentication_failed",
        message="代理请求认证失败",
        status_code=401,
        retryable=False,
    )


def _body_too_large() -> ApiError:
    return ApiError(
        code="request_body_too_large",
        message="请求体过大",
        status_code=413,
        retryable=False,
    )


def _is_proxy_protected_path(path: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _PROXY_PROTECTED_PATH_PREFIXES
    )


async def _send_proxy_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    error: ApiError,
) -> None:
    request_id = str(scope.get("state", {}).get("request_id", "unknown"))
    response = JSONResponse(
        status_code=error.status_code,
        headers={"cache-control": "no-store"},
        content={
            "schema_version": "1.0",
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "request_id": request_id,
            },
        },
    )
    await response(scope, receive, send)


class ProxyAuthenticationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_proxy_protected_path(
            str(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        required_header_names = {
            b"x-perfpilot-proxy-timestamp",
            b"x-perfpilot-proxy-signature",
            b"x-request-id",
        }
        header_values: dict[bytes, list[bytes]] = {
            name: [] for name in required_header_names
        }
        client_identity_values: list[bytes] = []
        content_lengths: list[bytes] = []
        for raw_name, raw_value in scope.get("headers", []):
            normalized_name = raw_name.lower()
            if normalized_name in header_values:
                header_values[normalized_name].append(raw_value)
            elif normalized_name == b"x-perfpilot-client-identity":
                client_identity_values.append(raw_value)
            elif normalized_name == b"content-length":
                content_lengths.append(raw_value)
        if any(len(values) != 1 for values in header_values.values()):
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return
        if len(content_lengths) > 1:
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return
        if content_lengths:
            if not content_lengths[0] or not content_lengths[0].isdigit():
                await _send_proxy_error(scope, receive, send, _proxy_failure())
                return
            significant_length = content_lengths[0].lstrip(b"0") or b"0"
            maximum_length = str(_MAX_PROXY_BODY_BYTES).encode("ascii")
            if len(significant_length) > len(maximum_length) or (
                len(significant_length) == len(maximum_length)
                and significant_length > maximum_length
            ):
                await _send_proxy_error(scope, receive, send, _body_too_large())
                return
            declared_length = int(significant_length)

        try:
            timestamp_header = header_values[
                b"x-perfpilot-proxy-timestamp"
            ][0].decode("ascii")
            request_id_header = header_values[b"x-request-id"][0].decode("ascii")
            signature_header = header_values[
                b"x-perfpilot-proxy-signature"
            ][0].decode("ascii")
        except UnicodeDecodeError:
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return

        app = scope["app"]
        raw_path = scope.get("raw_path")
        raw_query = scope.get("query_string")
        if not isinstance(raw_path, bytes) or not isinstance(raw_query, bytes):
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return

        secret = app.state.settings.proxy_secret.get_secret_value().encode()
        identity_required = app.state.proxy_client_identity_required
        if len(client_identity_values) > 1 or (
            identity_required and len(client_identity_values) != 1
        ):
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return
        verified_client_identity: str | None = None
        if client_identity_values:
            try:
                identity_header = client_identity_values[0].decode("ascii")
                verified_client_identity = verify_proxy_client_identity(
                    secret,
                    identity_header=identity_header,
                    timestamp_header=timestamp_header,
                    request_id_header=request_id_header,
                )
            except (ProxySignatureError, UnicodeDecodeError):
                await _send_proxy_error(scope, receive, send, _proxy_failure())
                return

        body_buffer = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await _send_proxy_error(scope, receive, send, _proxy_failure())
                return
            body_buffer.extend(message.get("body", b""))
            if len(body_buffer) > _MAX_PROXY_BODY_BYTES:
                await _send_proxy_error(scope, receive, send, _body_too_large())
                return
            if not message.get("more_body", False):
                break
        body = bytes(body_buffer)
        if content_lengths and declared_length != len(body):
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return

        try:
            authenticated = authenticate_proxy_request(
                secret,
                timestamp_header=timestamp_header,
                request_id_header=request_id_header,
                signature_header=signature_header,
                method=str(scope["method"]),
                raw_path=raw_path,
                raw_query=raw_query,
                body=body,
                clock=app.state.proxy_clock,
            )
            await reserve_proxy_request(
                authenticated,
                app.state.proxy_replay_store,
            )
        except (ProxySignatureError, UnicodeEncodeError):
            await _send_proxy_error(scope, receive, send, _proxy_failure())
            return
        except ApiError as error:
            await _send_proxy_error(scope, receive, send, error)
            return

        scope.setdefault("state", {})[_PROXY_AUTHENTICATED_STATE_KEY] = True
        if verified_client_identity is not None:
            scope["state"][PROXY_CLIENT_IDENTITY_STATE_KEY] = verified_client_identity
        body_pending = True

        async def replay_body() -> Message:
            nonlocal body_pending
            if body_pending:
                body_pending = False
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send)


async def require_proxy_request(request: Request) -> None:
    if not request.scope.get("state", {}).get(_PROXY_AUTHENTICATED_STATE_KEY):
        raise _proxy_failure()


def proxy_router_dependencies() -> list[Depends]:
    return [Depends(require_proxy_request)]


def get_auth_service(request: Request) -> AuthService:
    service: AuthService | None = request.app.state.auth_service
    if service is None:
        raise ApiError(
            code="service_unavailable",
            message="服务暂时不可用",
            status_code=503,
            retryable=True,
        )
    return service


router = APIRouter(
    prefix="/v1/auth",
    dependencies=proxy_router_dependencies(),
)


@router.get("/csrf")
async def get_csrf(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    try:
        issued = await auth_service.get_or_create_csrf(
            request.cookies.get(COOKIE_NAME),
            client_address=request.app.state.client_address_resolver(request),
        )
    except PreAuthSessionRateLimitedError:
        raise ApiError(
            code="csrf_rate_limited",
            message="会话初始化请求过于频繁",
            status_code=429,
            retryable=True,
        ) from None
    set_session_cookie(response, issued.token, max_age=issued.max_age)
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "csrf_token": issued.csrf_token,
    }


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
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
    try:
        issued = await auth_service.login(
            pre_auth_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=request.headers.get("x-csrf-token", ""),
            username=payload.username,
            password=payload.password,
            client_address=request.app.state.client_address_resolver(request),
        )
    except InvalidCredentialsError:
        raise ApiError(
            code="invalid_credentials",
            message="用户名或密码错误",
            status_code=401,
            retryable=False,
        ) from None
    except LoginRateLimitedError:
        raise ApiError(
            code="login_rate_limited",
            message="登录尝试过于频繁",
            status_code=429,
            retryable=True,
        ) from None
    except InvalidCsrfError:
        raise ApiError(
            code="csrf_validation_failed",
            message="CSRF 校验失败",
            status_code=403,
            retryable=False,
        ) from None
    except InvalidSessionError:
        raise ApiError(
            code="unauthenticated",
            message="需要重新登录",
            status_code=401,
            retryable=False,
        ) from None

    set_session_cookie(response, issued.token, max_age=issued.max_age)
    return {
        "schema_version": "1.0",
        "csrf_token": issued.csrf_token,
    }


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
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
    try:
        await auth_service.logout(
            session_token=request.cookies.get(COOKIE_NAME, ""),
            csrf_token=request.headers.get("x-csrf-token", ""),
        )
    except InvalidCsrfError:
        raise ApiError(
            code="csrf_validation_failed",
            message="CSRF 校验失败",
            status_code=403,
            retryable=False,
        ) from None
    except InvalidSessionError:
        raise ApiError(
            code="unauthenticated",
            message="需要重新登录",
            status_code=401,
            retryable=False,
        ) from None
    clear_session_cookie(response)
