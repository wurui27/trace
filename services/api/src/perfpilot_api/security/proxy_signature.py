import asyncio
import base64
import hashlib
import hmac
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol

DEFAULT_FRESHNESS_SECONDS = 60

_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SIGNATURE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_TIMESTAMP_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,19})")
_CLIENT_IDENTITY_PATTERN = re.compile(
    r"(?P<client_id>[A-Za-z0-9_-]{43})\."
    r"(?P<attestation>[A-Za-z0-9_-]{43})"
)
_CLIENT_ID_DOMAIN = b"perfpilot-client-id-v1\n"
_CLIENT_ATTESTATION_DOMAIN = b"perfpilot-client-attestation-v1\n"


class ProxySignatureError(ValueError):
    """A redacted proxy-authentication failure."""


class InvalidProxySignatureError(ProxySignatureError):
    def __init__(self) -> None:
        super().__init__("proxy signature is invalid")


class StaleProxySignatureError(ProxySignatureError):
    def __init__(self) -> None:
        super().__init__("proxy signature timestamp is outside the allowed window")


class ProxyReplayError(ProxySignatureError):
    def __init__(self) -> None:
        super().__init__("proxy request has already been used")


class InvalidProxyClientIdentityError(ProxySignatureError):
    def __init__(self) -> None:
        super().__init__("proxy client identity is invalid")


class ReplayStore(Protocol):
    async def reserve(self, request_id: str, *, ttl_seconds: int) -> bool: ...


class RedisSetClient(Protocol):
    async def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool,
        ex: int,
    ) -> object: ...


@dataclass(frozen=True)
class AuthenticatedProxyRequest:
    request_id: str
    replay_ttl_seconds: int


class InMemoryReplayStore:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._expires_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, request_id: str, *, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("replay TTL must be positive")
        async with self._lock:
            now = self._clock()
            existing_expiry = self._expires_at.get(request_id)
            if existing_expiry is not None and existing_expiry > now:
                return False
            self._expires_at[request_id] = now + ttl_seconds
            return True


class RedisReplayStore:
    def __init__(self, client: RedisSetClient, *, key_prefix: str = "perfpilot:proxy:") -> None:
        self._client = client
        self._key_prefix = key_prefix

    async def reserve(self, request_id: str, *, ttl_seconds: int) -> bool:
        result = await self._client.set(
            f"{self._key_prefix}{request_id}",
            "1",
            nx=True,
            ex=ttl_seconds,
        )
        return bool(result)


def _canonical_request(
    *,
    timestamp: int,
    request_id: str,
    method: str,
    raw_path: bytes,
    raw_query: bytes,
    body: bytes,
) -> bytes:
    path_and_query = raw_path + (b"?" + raw_query if raw_query else b"")
    return b"\n".join(
        (
            str(timestamp).encode("ascii"),
            request_id.encode("ascii"),
            method.upper().encode("ascii"),
            path_and_query,
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )


def _urlsafe_hmac(secret: bytes, payload: bytes) -> str:
    digest = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _client_attestation(
    secret: bytes,
    *,
    timestamp: str,
    request_id: str,
    client_id: str,
) -> str:
    payload = _CLIENT_ATTESTATION_DOMAIN + b"\n".join(
        (
            timestamp.encode("ascii"),
            request_id.encode("ascii"),
            client_id.encode("ascii"),
        )
    )
    return _urlsafe_hmac(secret, payload)


def sign_proxy_client_identity(
    secret: bytes,
    *,
    client_address: str,
    timestamp: int,
    request_id: str,
) -> str:
    if (
        "%" in client_address
        or _TIMESTAMP_PATTERN.fullmatch(str(timestamp)) is None
        or _REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise ValueError("invalid client address or identity inputs")
    try:
        canonical_address = str(ip_address(client_address))
    except ValueError:
        raise ValueError("invalid client address") from None
    client_id = _urlsafe_hmac(
        secret,
        _CLIENT_ID_DOMAIN + canonical_address.encode("ascii"),
    )
    attestation = _client_attestation(
        secret,
        timestamp=str(timestamp),
        request_id=request_id,
        client_id=client_id,
    )
    return f"{client_id}.{attestation}"


def verify_proxy_client_identity(
    secret: bytes,
    *,
    identity_header: str | None,
    timestamp_header: str | None,
    request_id_header: str | None,
) -> str:
    identity_match = (
        _CLIENT_IDENTITY_PATTERN.fullmatch(identity_header)
        if identity_header is not None
        else None
    )
    if (
        identity_match is None
        or timestamp_header is None
        or _TIMESTAMP_PATTERN.fullmatch(timestamp_header) is None
        or request_id_header is None
        or _REQUEST_ID_PATTERN.fullmatch(request_id_header) is None
    ):
        raise InvalidProxyClientIdentityError
    client_id = identity_match.group("client_id")
    expected_attestation = _client_attestation(
        secret,
        timestamp=timestamp_header,
        request_id=request_id_header,
        client_id=client_id,
    )
    if not hmac.compare_digest(
        expected_attestation,
        identity_match.group("attestation"),
    ):
        raise InvalidProxyClientIdentityError
    return client_id


def sign_proxy_request(
    secret: bytes,
    *,
    timestamp: int,
    request_id: str,
    method: str,
    raw_path: bytes,
    raw_query: bytes = b"",
    body: bytes = b"",
) -> str:
    canonical_request = _canonical_request(
        timestamp=timestamp,
        request_id=request_id,
        method=method,
        raw_path=raw_path,
        raw_query=raw_query,
        body=body,
    )
    signature = hmac.new(secret, canonical_request, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def authenticate_proxy_request(
    secret: bytes,
    *,
    timestamp_header: str | None,
    request_id_header: str | None,
    signature_header: str | None,
    method: str,
    raw_path: bytes,
    raw_query: bytes = b"",
    body: bytes = b"",
    clock: Callable[[], float] = time.time,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> AuthenticatedProxyRequest:
    if (
        timestamp_header is None
        or _TIMESTAMP_PATTERN.fullmatch(timestamp_header) is None
        or request_id_header is None
        or _REQUEST_ID_PATTERN.fullmatch(request_id_header) is None
        or signature_header is None
        or _SIGNATURE_PATTERN.fullmatch(signature_header) is None
        or re.fullmatch(r"[A-Za-z]+", method) is None
        or not raw_path.startswith(b"/")
    ):
        raise InvalidProxySignatureError

    timestamp = int(timestamp_header)
    if freshness_seconds < 0 or abs(clock() - timestamp) > freshness_seconds:
        raise StaleProxySignatureError

    expected_signature = sign_proxy_request(
        secret,
        timestamp=timestamp,
        request_id=request_id_header,
        method=method,
        raw_path=raw_path,
        raw_query=raw_query,
        body=body,
    )
    if not hmac.compare_digest(expected_signature, signature_header):
        raise InvalidProxySignatureError

    return AuthenticatedProxyRequest(
        request_id=request_id_header,
        replay_ttl_seconds=freshness_seconds * 2 + 1,
    )


async def reserve_proxy_request(
    authenticated: AuthenticatedProxyRequest,
    replay_store: ReplayStore,
) -> None:
    if not await replay_store.reserve(
        authenticated.request_id,
        ttl_seconds=authenticated.replay_ttl_seconds,
    ):
        raise ProxyReplayError


async def verify_proxy_request(
    secret: bytes,
    *,
    timestamp_header: str | None,
    request_id_header: str | None,
    signature_header: str | None,
    method: str,
    raw_path: bytes,
    replay_store: ReplayStore,
    raw_query: bytes = b"",
    body: bytes = b"",
    clock: Callable[[], float] = time.time,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> None:
    authenticated = authenticate_proxy_request(
        secret,
        timestamp_header=timestamp_header,
        request_id_header=request_id_header,
        signature_header=signature_header,
        method=method,
        raw_path=raw_path,
        raw_query=raw_query,
        body=body,
        clock=clock,
        freshness_seconds=freshness_seconds,
    )
    await reserve_proxy_request(authenticated, replay_store)
