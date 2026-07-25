import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from perfpilot_api.api.health import router as health_router
from perfpilot_api.config import Settings, get_settings
from perfpilot_api.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    internal_server_error_handler,
    request_validation_error_handler,
)

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


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


def create_app(testing: bool = False) -> FastAPI:
    settings = (
        Settings(
            app_env="test",
            _env_prefix="PERFPILOT_TEST_ISOLATED_",
            _env_file=None,
            _secrets_dir=None,
        )
        if testing
        else get_settings()
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.testing = testing
    app.state.settings = settings
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        request_validation_error_handler,
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, internal_server_error_handler)
    app.include_router(health_router)
    return app


def run() -> None:
    uvicorn.run("perfpilot_api.main:create_app", factory=True)
