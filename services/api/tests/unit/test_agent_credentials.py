from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    AgentProofRejected,
    InMemoryAgentNonceStore,
    encode_ed25519_public_key,
    encode_signature,
    refresh_proof_message,
    verify_refresh_proof,
)

AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)


class CountingEntropy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, size: int) -> bytes:
        self.calls += 1
        return bytes([self.calls]) * size


def test_opaque_credentials_have_fixed_prefix_and_256_bit_entropy() -> None:
    codec = AgentCredentialCodec(b"c" * 32, entropy=CountingEntropy())

    registration_code = codec.issue_registration_code()
    access_token = codec.issue_access_token()
    refresh_token = codec.issue_refresh_token()

    assert registration_code.startswith("ppreg_")
    assert access_token.startswith("ppat_")
    assert refresh_token.startswith("pprt_")
    assert len(registration_code) == 49
    assert len(access_token) == 48
    assert len(refresh_token) == 48
    assert len(base64.urlsafe_b64decode(registration_code[6:] + "=")) == 32
    assert len(base64.urlsafe_b64decode(access_token[5:] + "=")) == 32
    assert len(base64.urlsafe_b64decode(refresh_token[5:] + "=")) == 32


def test_credential_digests_are_keyed_and_compared_without_leaking_secret() -> None:
    secret = b"credential-secret-marker-1234567"
    codec = AgentCredentialCodec(secret, entropy=CountingEntropy())
    token = codec.issue_access_token()
    digest = codec.digest(token)

    assert len(digest) == 64
    assert codec.matches(token, digest)
    assert not codec.matches(token[:-1] + "A", digest)
    assert token not in repr(codec)
    assert secret.decode() not in repr(codec)


def test_refresh_proof_accepts_canonical_ed25519_signature() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key_b64 = encode_ed25519_public_key(private_key.public_key())
    nonce = "bm9uY2UtdGhhdC1pcy1sb25nLWVub3VnaA"
    timestamp = int(NOW.timestamp())
    signature_b64 = encode_signature(
        private_key.sign(refresh_proof_message(AGENT_ID, nonce, timestamp))
    )

    verify_refresh_proof(
        agent_id=AGENT_ID,
        public_key_b64=public_key_b64,
        nonce=nonce,
        timestamp=timestamp,
        signature_b64=signature_b64,
        now=NOW,
    )


@pytest.mark.parametrize("mutation", ["stale", "tampered", "noncanonical-key"])
def test_refresh_proof_rejects_invalid_input_without_echoing_it(mutation: str) -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key_b64 = encode_ed25519_public_key(private_key.public_key())
    nonce = "bm9uY2UtdGhhdC1pcy1sb25nLWVub3VnaA"
    timestamp = int(NOW.timestamp())
    signature_b64 = encode_signature(
        private_key.sign(refresh_proof_message(AGENT_ID, nonce, timestamp))
    )
    if mutation == "stale":
        timestamp -= 61
    elif mutation == "tampered":
        signature_b64 = "A" + signature_b64[1:]
    else:
        public_key_b64 = public_key_b64[:-1] + "\n"

    with pytest.raises(AgentProofRejected) as captured:
        verify_refresh_proof(
            agent_id=AGENT_ID,
            public_key_b64=public_key_b64,
            nonce=nonce,
            timestamp=timestamp,
            signature_b64=signature_b64,
            now=NOW,
        )

    rendered = str(captured.value)
    assert nonce not in rendered
    assert signature_b64 not in rendered
    assert public_key_b64 not in rendered


@pytest.mark.asyncio
async def test_nonce_store_accepts_once_and_forgets_after_ttl() -> None:
    current = [NOW.timestamp()]
    store = InMemoryAgentNonceStore(
        key_secret=b"n" * 32,
        clock=lambda: current[0],
        ttl_seconds=120,
    )
    nonce = "bm9uY2UtdGhhdC1pcy1sb25nLWVub3VnaA"

    assert await store.reserve(AGENT_ID, nonce)
    assert not await store.reserve(AGENT_ID, nonce)
    assert nonce not in repr(store)

    current[0] += 121
    assert await store.reserve(AGENT_ID, nonce)
