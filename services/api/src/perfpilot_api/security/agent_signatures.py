from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_PUBLIC_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")
_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9+/]{86}==$")
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_MAXIMUM_CLOCK_SKEW_SECONDS = 60
_NONCE_TTL_SECONDS = 120


class AgentProofRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Agent key proof was rejected")


class AgentNonceStore(Protocol):
    async def reserve(self, agent_id: UUID, nonce: str) -> bool: ...


def _decode_canonical_base64(value: str, *, pattern: re.Pattern[str], size: int) -> bytes:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AgentProofRejected
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise AgentProofRejected from None
    if len(decoded) != size or base64.b64encode(decoded).decode("ascii") != value:
        raise AgentProofRejected
    return decoded


def decode_ed25519_public_key(value: str) -> Ed25519PublicKey:
    raw_key = _decode_canonical_base64(value, pattern=_PUBLIC_KEY_PATTERN, size=32)
    try:
        return Ed25519PublicKey.from_public_bytes(raw_key)
    except ValueError:
        raise AgentProofRejected from None


def encode_ed25519_public_key(public_key: Ed25519PublicKey) -> str:
    raw_key = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw_key).decode("ascii")


def encode_signature(signature: bytes) -> str:
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("Ed25519 signature is invalid")
    return base64.b64encode(signature).decode("ascii")


def refresh_proof_message(agent_id: UUID, nonce: str, timestamp: int) -> bytes:
    if (
        not isinstance(agent_id, UUID)
        or not isinstance(nonce, str)
        or _NONCE_PATTERN.fullmatch(nonce) is None
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
    ):
        raise AgentProofRejected
    return f"{agent_id}\n{nonce}\n{timestamp}".encode("ascii")


def verify_refresh_proof(
    *,
    agent_id: UUID,
    public_key_b64: str,
    nonce: str,
    timestamp: int,
    signature_b64: str,
    now: datetime,
    maximum_clock_skew_seconds: int = _MAXIMUM_CLOCK_SKEW_SECONDS,
) -> None:
    try:
        if (
            now.tzinfo is None
            or maximum_clock_skew_seconds < 0
            or abs(int(now.timestamp()) - timestamp) > maximum_clock_skew_seconds
        ):
            raise AgentProofRejected
        public_key = decode_ed25519_public_key(public_key_b64)
        signature = _decode_canonical_base64(
            signature_b64,
            pattern=_SIGNATURE_PATTERN,
            size=64,
        )
        public_key.verify(
            signature,
            refresh_proof_message(agent_id, nonce, timestamp),
        )
    except (AgentProofRejected, InvalidSignature, OverflowError, OSError, ValueError):
        raise AgentProofRejected from None


def _nonce_digest(key_secret: bytes, agent_id: UUID, nonce: str) -> str:
    if not isinstance(key_secret, bytes) or len(key_secret) < 32:
        raise ValueError("Agent nonce secret is invalid")
    if _NONCE_PATTERN.fullmatch(nonce) is None:
        raise AgentProofRejected
    message = f"{agent_id}\n{nonce}".encode("ascii")
    return hmac.new(key_secret, message, hashlib.sha256).hexdigest()


class InMemoryAgentNonceStore:
    __slots__ = ("_clock", "_key_secret", "_reservations", "_ttl_seconds")

    def __init__(
        self,
        *,
        key_secret: bytes,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = _NONCE_TTL_SECONDS,
    ) -> None:
        if len(key_secret) < 32 or ttl_seconds < 1:
            raise ValueError("Agent nonce store configuration is invalid")
        self._key_secret = key_secret
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._reservations: dict[str, float] = {}

    def __repr__(self) -> str:
        return "InMemoryAgentNonceStore()"

    async def reserve(self, agent_id: UUID, nonce: str) -> bool:
        now = self._clock()
        expired = [key for key, expires_at in self._reservations.items() if expires_at <= now]
        for key in expired:
            del self._reservations[key]
        digest = _nonce_digest(self._key_secret, agent_id, nonce)
        if digest in self._reservations:
            return False
        self._reservations[digest] = now + self._ttl_seconds
        return True


class RedisAgentNonceStore:
    __slots__ = ("_key_secret", "_redis", "_ttl_seconds")

    def __init__(
        self,
        redis_client: Any,
        *,
        key_secret: bytes,
        ttl_seconds: int = _NONCE_TTL_SECONDS,
    ) -> None:
        if len(key_secret) < 32 or ttl_seconds < 1:
            raise ValueError("Agent nonce store configuration is invalid")
        self._redis = redis_client
        self._key_secret = key_secret
        self._ttl_seconds = ttl_seconds

    def __repr__(self) -> str:
        return "RedisAgentNonceStore()"

    async def reserve(self, agent_id: UUID, nonce: str) -> bool:
        digest = _nonce_digest(self._key_secret, agent_id, nonce)
        reserved = await self._redis.set(
            f"perfpilot:agent:refresh-nonce:{digest}",
            "1",
            ex=self._ttl_seconds,
            nx=True,
        )
        return bool(reserved)


__all__ = [
    "AgentNonceStore",
    "AgentProofRejected",
    "InMemoryAgentNonceStore",
    "RedisAgentNonceStore",
    "decode_ed25519_public_key",
    "encode_ed25519_public_key",
    "encode_signature",
    "refresh_proof_message",
    "verify_refresh_proof",
]
