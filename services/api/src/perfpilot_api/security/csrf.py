import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from urllib.parse import urlsplit

_TOKEN_BYTES = 32
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")


class OriginNotAllowedError(ValueError):
    def __init__(self) -> None:
        super().__init__("request origin is not allowed")


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def digest_csrf_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_csrf_token(token: str, expected_digest: str) -> bool:
    if _SHA256_HEX_PATTERN.fullmatch(expected_digest) is None:
        return False
    return hmac.compare_digest(digest_csrf_token(token), expected_digest)


def _canonical_origin(value: str, *, allow_trailing_slash: bool) -> tuple[str, str, int | None] | None:
    if not value or value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ({"", "/"} if allow_trailing_slash else {""})
    ):
        return None

    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if port == (443 if scheme == "https" else 80):
        port = None
    return scheme, host, port


def is_allowed_origin(origin: str | None, allowed_origins: Iterable[str]) -> bool:
    if origin is None:
        return False
    candidate = _canonical_origin(origin, allow_trailing_slash=False)
    if candidate is None:
        return False
    return any(
        candidate == _canonical_origin(str(allowed), allow_trailing_slash=True)
        for allowed in allowed_origins
    )


def require_allowed_origin(origin: str | None, allowed_origins: Iterable[str]) -> None:
    if not is_allowed_origin(origin, allowed_origins):
        raise OriginNotAllowedError
