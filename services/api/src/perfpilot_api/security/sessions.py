import hashlib
import hmac
import re
import secrets

from starlette.responses import Response

COOKIE_NAME = "perfpilot_session"
_TOKEN_BYTES = 32
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")


def generate_session_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def digest_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_session_token(token: str, expected_digest: str) -> bool:
    if _SHA256_HEX_PATTERN.fullmatch(expected_digest) is None:
        return False
    return hmac.compare_digest(digest_session_token(token), expected_digest)


def set_session_cookie(response: Response, token: str, *, max_age: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
