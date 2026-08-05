from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from typing import Literal

CredentialKind = Literal["registration", "access", "refresh"]

_TOKEN_BYTES = 32
_MINIMUM_SECRET_BYTES = 32
_PREFIXES: dict[CredentialKind, str] = {
    "registration": "ppreg_",
    "access": "ppat_",
    "refresh": "pprt_",
}
_TOKEN_PATTERN = re.compile(r"^(?:ppreg_|ppat_|pprt_)[A-Za-z0-9_-]{43}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AgentCredentialCodec:
    __slots__ = ("_entropy", "_secret")

    def __init__(
        self,
        secret: bytes,
        *,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < _MINIMUM_SECRET_BYTES:
            raise ValueError("Agent credential secret is invalid")
        self._secret = secret
        self._entropy = entropy

    def __repr__(self) -> str:
        return "AgentCredentialCodec()"

    def _issue(self, kind: CredentialKind) -> str:
        random_bytes = self._entropy(_TOKEN_BYTES)
        if not isinstance(random_bytes, bytes) or len(random_bytes) != _TOKEN_BYTES:
            raise RuntimeError("Agent credential entropy source failed")
        suffix = base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")
        return f"{_PREFIXES[kind]}{suffix}"

    def issue_registration_code(self) -> str:
        return self._issue("registration")

    def issue_access_token(self) -> str:
        return self._issue("access")

    def issue_refresh_token(self) -> str:
        return self._issue("refresh")

    def digest(self, token: str) -> str:
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("Agent credential is invalid")
        return hmac.new(self._secret, token.encode("ascii"), hashlib.sha256).hexdigest()

    def matches(self, token: str, expected_digest: str | None) -> bool:
        if (
            not isinstance(token, str)
            or _TOKEN_PATTERN.fullmatch(token) is None
            or not isinstance(expected_digest, str)
            or _DIGEST_PATTERN.fullmatch(expected_digest) is None
        ):
            return False
        actual_digest = hmac.new(
            self._secret,
            token.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(actual_digest, expected_digest)


__all__ = ["AgentCredentialCodec", "CredentialKind"]
