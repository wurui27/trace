from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from perfpilot_api.api.auth import get_auth_service, proxy_router_dependencies
from perfpilot_api.errors import ApiError
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.auth import AuthService, InvalidSessionError

router = APIRouter(prefix="/v1", dependencies=proxy_router_dependencies())


@router.get("/me")
async def get_me(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, object]:
    try:
        me = await auth_service.get_me(request.cookies.get(COOKIE_NAME, ""))
    except InvalidSessionError:
        raise ApiError(
            code="unauthenticated",
            message="需要重新登录",
            status_code=401,
            retryable=False,
        ) from None
    response.headers["cache-control"] = "no-store"
    return {
        "schema_version": "1.0",
        "user": {
            "id": str(me.user_id),
            "username": me.username,
            "is_platform_admin": me.is_platform_admin,
        },
        "memberships": [
            {
                "id": str(membership.id),
                "team": {
                    "id": str(membership.team_id),
                    "name": membership.team_name,
                },
                "role": membership.role,
            }
            for membership in me.memberships
        ],
    }
