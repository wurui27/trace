import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send
from redis import asyncio as redis

from perfpilot_api.api.auth import PROXY_CLIENT_IDENTITY_STATE_KEY
from perfpilot_api.api.auth import ProxyAuthenticationMiddleware
from perfpilot_api.api.auth import router as auth_router
from perfpilot_api.api.health import router as health_router
from perfpilot_api.api.me import router as me_router
from perfpilot_api.api.members import router as members_router
from perfpilot_api.config import Settings, get_settings
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    internal_server_error_handler,
    request_validation_error_handler,
)
from perfpilot_api.security.proxy_signature import (
    InMemoryReplayStore,
    RedisReplayStore,
    ReplayStore,
)
from perfpilot_api.services.auth import (
    AuthService,
    RedisLoginRateLimiter,
    RedisPreAuthSessionLimiter,
)

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _transport_client_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _verified_client_identity(request: Request) -> str:
    identity = request.scope.get("state", {}).get(
        PROXY_CLIENT_IDENTITY_STATE_KEY
    )
    if not isinstance(identity, str) or not identity:
        raise RuntimeError("verified proxy client identity is missing")
    return identity


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming_request_id = Headers(scope=scope).get("x-request-id")
        request_id = (
            incoming_request_id
            if incoming_request_id
            and _REQUEST_ID_PATTERN.fullmatch(incoming_request_id)
            else uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class AuthNoStoreMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/v1/auth"
            or path.startswith("/v1/auth/")
            or path == "/v1/me"
            or path == "/v1/teams"
            or path.startswith("/v1/teams/")
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_no_store)


def create_app(
    testing: bool = False,
    *,
    settings_override: Settings | None = None,
    auth_service: AuthService | None = None,
    replay_store: ReplayStore | None = None,
    proxy_clock: Callable[[], float] = time.time,
    client_address_resolver: Callable[[Request], str] | None = None,
    proxy_client_identity_required: bool | None = None,
) -> FastAPI:
    settings = settings_override or (
        Settings(
            app_env="test",
            _env_prefix="PERFPILOT_TEST_ISOLATED_",
            _env_file=None,
            _secrets_dir=None,
        )
        if testing
        else get_settings()
    )

    owned_engine = None
    owned_redis = None
    resolved_auth_service = auth_service
    resolved_replay_store = replay_store
    if not testing and (
        resolved_auth_service is None or resolved_replay_store is None
    ):
        owned_redis = redis.from_url(settings.redis_url.get_secret_value())
        if resolved_replay_store is None:
            resolved_replay_store = RedisReplayStore(owned_redis)
        if resolved_auth_service is None:
            owned_engine = create_control_engine(
                settings.control_database_url.get_secret_value()
            )
            session_factory = create_control_session_factory(owned_engine)
            resolved_auth_service = AuthService(
                session_factory=session_factory,
                rate_limiter=RedisLoginRateLimiter(
                    owned_redis,
                    key_secret=settings.session_secret.get_secret_value().encode(),
                    nonce_source=lambda: secrets.token_hex(16),
                ),
                pre_auth_session_limiter=RedisPreAuthSessionLimiter(
                    owned_redis,
                    key_secret=settings.session_secret.get_secret_value().encode(),
                    nonce_source=lambda: secrets.token_hex(16),
                ),
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_redis is not None:
                await owned_redis.aclose()
            if owned_engine is not None:
                await owned_engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.testing = testing
    app.state.settings = settings
    app.state.auth_service = resolved_auth_service
    app.state.proxy_replay_store = resolved_replay_store or InMemoryReplayStore(
        clock=proxy_clock
    )
    app.state.proxy_clock = proxy_clock
    identity_required = (
        not testing
        if proxy_client_identity_required is None
        else proxy_client_identity_required
    )
    app.state.proxy_client_identity_required = identity_required
    app.state.client_address_resolver = client_address_resolver or (
        _verified_client_identity if identity_required else _transport_client_address
    )
    app.add_middleware(ProxyAuthenticationMiddleware)
    app.add_middleware(AuthNoStoreMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        request_validation_error_handler,
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, internal_server_error_handler)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(members_router)
    return app


def run() -> None:
    uvicorn.run("perfpilot_api.main:create_app", factory=True)
