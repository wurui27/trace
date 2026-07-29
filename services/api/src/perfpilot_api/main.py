import asyncio
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
from perfpilot_api.api.admin_teams import router as admin_teams_router
from perfpilot_api.api.analyses import router as analyses_router
from perfpilot_api.api.health import router as health_router
from perfpilot_api.api.me import router as me_router
from perfpilot_api.api.members import router as members_router
from perfpilot_api.api.uploads import router as uploads_router
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
from perfpilot_api.runtime.artifacts import ArtifactRuntime, build_artifact_runtime
from perfpilot_api.services.auth import (
    AuthService,
    RedisLoginRateLimiter,
    RedisPreAuthSessionLimiter,
)
from perfpilot_api.services.analyses import (
    AnalysisService,
    ApkInspector,
    SQLAlchemyAnalysisRepository,
)
from perfpilot_api.services.provisioning import AdminTeamService
from perfpilot_api.services.uploads import UploadService

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


def _prefer_control_flow_error(
    first_error: BaseException | None,
    error: BaseException,
) -> BaseException:
    if first_error is None or (
        isinstance(error, _CONTROL_FLOW_EXCEPTIONS)
        and not isinstance(first_error, _CONTROL_FLOW_EXCEPTIONS)
    ):
        return error
    return first_error


def _transport_client_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _verified_client_identity(request: Request) -> str:
    identity = request.scope.get("state", {}).get(PROXY_CLIENT_IDENTITY_STATE_KEY)
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
            if incoming_request_id and _REQUEST_ID_PATTERN.fullmatch(incoming_request_id)
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
            or path == "/v1/admin"
            or path.startswith("/v1/admin/")
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
    admin_team_service: AdminTeamService | None = None,
    upload_service: UploadService | None = None,
    analysis_service: AnalysisService | None = None,
    apk_inspector: ApkInspector | None = None,
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
    owned_artifact_runtime: ArtifactRuntime | None = None
    control_session_factory = None
    resolved_auth_service = auth_service
    resolved_admin_team_service = admin_team_service
    resolved_upload_service = upload_service
    resolved_analysis_service = analysis_service
    resolved_replay_store = replay_store
    artifact_runtime_required = (
        not testing
        and settings.app_env == "production"
        and (resolved_upload_service is None or resolved_analysis_service is None)
    )
    if not testing and (resolved_auth_service is None or resolved_replay_store is None):
        owned_redis = redis.from_url(settings.redis_url.get_secret_value())
        if resolved_replay_store is None:
            resolved_replay_store = RedisReplayStore(owned_redis)
    if not testing and (
        resolved_auth_service is None
        or resolved_admin_team_service is None
        or artifact_runtime_required
    ):
        owned_engine = create_control_engine(settings.control_database_url.get_secret_value())
        control_session_factory = create_control_session_factory(owned_engine)
        if resolved_auth_service is None:
            if owned_redis is None:
                raise RuntimeError("Redis authentication dependencies are unavailable")
            resolved_auth_service = AuthService(
                session_factory=control_session_factory,
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
        if resolved_admin_team_service is None:
            resolved_admin_team_service = AdminTeamService(
                session_factory=control_session_factory,
            )

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        nonlocal owned_artifact_runtime, resolved_analysis_service, resolved_upload_service
        lifespan_error: BaseException | None = None
        try:
            if settings.app_env == "production" and apk_inspector is None:
                raise RuntimeError("An externally isolated APK inspector is unavailable")
            if artifact_runtime_required:
                if control_session_factory is None:
                    raise RuntimeError("Artifact runtime dependencies are unavailable")
                owned_artifact_runtime = await build_artifact_runtime(
                    settings=settings,
                    control_session_factory=control_session_factory,
                    include_local_apk_inspector=settings.app_env != "production",
                )
                if resolved_upload_service is None:
                    resolved_upload_service = owned_artifact_runtime.upload_service
                lifespan_app.state.upload_service = resolved_upload_service
                if resolved_analysis_service is None:
                    resolved_inspector = apk_inspector
                    if settings.app_env != "production":
                        resolved_inspector = resolved_inspector or owned_artifact_runtime.apk_inspector
                    if resolved_inspector is None:
                        raise RuntimeError("An externally isolated APK inspector is unavailable")
                    resolved_analysis_service = AnalysisService(
                        repository=SQLAlchemyAnalysisRepository(
                            control_session_factory=control_session_factory,
                            tenant_router=owned_artifact_runtime.tenant_router,
                        ),
                        upload_service=resolved_upload_service,
                        apk_inspector=resolved_inspector,
                    )
                    lifespan_app.state.analysis_service = resolved_analysis_service
            yield
        except BaseException as error:
            lifespan_error = error
        finally:
            cleanup_error: BaseException | None = None
            cleanup_steps = (
                owned_artifact_runtime.close if owned_artifact_runtime is not None else None,
                owned_redis.aclose if owned_redis is not None else None,
                owned_engine.dispose if owned_engine is not None else None,
            )
            for cleanup in cleanup_steps:
                if cleanup is None:
                    continue
                try:
                    await cleanup()
                except BaseException as error:
                    cleanup_error = _prefer_control_flow_error(cleanup_error, error)
            failure = lifespan_error
            if cleanup_error is not None:
                failure = _prefer_control_flow_error(failure, cleanup_error)
            if failure is not None:
                if failure is lifespan_error or isinstance(failure, _CONTROL_FLOW_EXCEPTIONS):
                    raise failure
                raise RuntimeError("Application dependency cleanup failed") from None

    app = FastAPI(lifespan=lifespan)
    app.state.testing = testing
    app.state.settings = settings
    app.state.auth_service = resolved_auth_service
    app.state.admin_team_service = resolved_admin_team_service
    app.state.upload_service = resolved_upload_service
    app.state.analysis_service = resolved_analysis_service
    app.state.proxy_replay_store = resolved_replay_store or InMemoryReplayStore(clock=proxy_clock)
    app.state.proxy_clock = proxy_clock
    identity_required = (
        not testing if proxy_client_identity_required is None else proxy_client_identity_required
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
    app.include_router(admin_teams_router)
    app.include_router(me_router)
    app.include_router(members_router)
    app.include_router(analyses_router)
    app.include_router(uploads_router)
    return app


def run() -> None:
    uvicorn.run("perfpilot_api.main:create_app", factory=True)
