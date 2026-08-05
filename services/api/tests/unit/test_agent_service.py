from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    InMemoryAgentNonceStore,
    encode_ed25519_public_key,
    encode_signature,
    refresh_proof_message,
)
from perfpilot_api.services.agents import (
    AgentAuthenticationRejected,
    AgentNameConflictError,
    AgentNotFoundError,
    AgentRegistration,
    AgentRegistrationRejected,
    AgentService,
    InMemoryAgentRepository,
    TaskSigningKey,
)

TEAM_A_ID = UUID("20000000-0000-4000-8000-000000000001")
TEAM_B_ID = UUID("20000000-0000-4000-8000-000000000002")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class CountingEntropy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, size: int) -> bytes:
        self.calls += 1
        return self.calls.to_bytes(4, "big") * (size // 4)


@dataclass(frozen=True)
class ServiceHarness:
    service: AgentService
    clock: MutableClock
    private_key: Ed25519PrivateKey


@pytest.fixture
def harness() -> ServiceHarness:
    clock = MutableClock()
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    task_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    service = AgentService(
        repository=InMemoryAgentRepository(uuid_factory=uuid4),
        credentials=AgentCredentialCodec(
            b"credential-service-secret-123456",
            entropy=CountingEntropy(),
        ),
        nonce_store=InMemoryAgentNonceStore(
            key_secret=b"n" * 32,
            clock=lambda: clock().timestamp(),
        ),
        task_signing_key=TaskSigningKey(
            kid="lan-test-key",
            public_key_b64=encode_ed25519_public_key(task_key.public_key()),
        ),
        clock=clock,
    )
    return ServiceHarness(service=service, clock=clock, private_key=private_key)


def registration(public_key_b64: str, code: str) -> AgentRegistration:
    return AgentRegistration(
        registration_code=code,
        public_key_b64=public_key_b64,
        platform="macos",
        agent_version="1.2.3",
        hostname="Ray Mac",
        os_version="macOS 15.6",
    )


async def register_agent(harness: ServiceHarness, *, name: str = "Ray Mac"):
    issued = await harness.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name=name,
    )
    public_key_b64 = encode_ed25519_public_key(harness.private_key.public_key())
    credentials = await harness.service.register(
        registration(public_key_b64, issued.registration_code)
    )
    return issued, credentials


def refresh_signature(
    harness: ServiceHarness,
    *,
    agent_id: UUID,
    nonce: str,
    timestamp: int,
) -> str:
    return encode_signature(
        harness.private_key.sign(refresh_proof_message(agent_id, nonce, timestamp))
    )


@pytest.mark.asyncio
async def test_registration_code_is_single_use(harness: ServiceHarness) -> None:
    issued = await harness.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name="Ray Mac",
    )
    request = registration(
        encode_ed25519_public_key(harness.private_key.public_key()),
        issued.registration_code,
    )

    registered = await harness.service.register(request)

    assert registered.agent_id == issued.agent_id
    with pytest.raises(AgentRegistrationRejected):
        await harness.service.register(request)


@pytest.mark.asyncio
async def test_registration_code_expires_after_ten_minutes(harness: ServiceHarness) -> None:
    issued = await harness.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name="Ray Mac",
    )
    harness.clock.advance(minutes=10, seconds=1)

    with pytest.raises(AgentRegistrationRejected):
        await harness.service.register(
            registration(
                encode_ed25519_public_key(harness.private_key.public_key()),
                issued.registration_code,
            )
        )


@pytest.mark.asyncio
async def test_refresh_rotates_both_tokens_and_rejects_old_refresh(
    harness: ServiceHarness,
) -> None:
    _, registered = await register_agent(harness)
    nonce = "cmVmcmVzaC1ub25jZS0wMDAwMDAwMDAwMDA"
    timestamp = int(harness.clock().timestamp())
    signature = refresh_signature(
        harness,
        agent_id=registered.agent_id,
        nonce=nonce,
        timestamp=timestamp,
    )

    refreshed = await harness.service.refresh(
        agent_id=registered.agent_id,
        refresh_token=registered.refresh_token,
        nonce=nonce,
        timestamp=timestamp,
        signature_b64=signature,
    )

    assert refreshed.access_token != registered.access_token
    assert refreshed.refresh_token != registered.refresh_token
    second_nonce = "cmVmcmVzaC1ub25jZS0xMTExMTExMTExMTEx"
    with pytest.raises(AgentAuthenticationRejected):
        await harness.service.refresh(
            agent_id=registered.agent_id,
            refresh_token=registered.refresh_token,
            nonce=second_nonce,
            timestamp=timestamp,
            signature_b64=refresh_signature(
                harness,
                agent_id=registered.agent_id,
                nonce=second_nonce,
                timestamp=timestamp,
            ),
        )


@pytest.mark.asyncio
async def test_revoke_invalidates_access_refresh_and_active_identity(
    harness: ServiceHarness,
) -> None:
    _, registered = await register_agent(harness)

    principal = await harness.service.authenticate_access(registered.access_token)
    assert principal.agent_id == registered.agent_id
    revoked = await harness.service.revoke(
        team_id=TEAM_A_ID,
        agent_id=registered.agent_id,
    )
    assert revoked.state == "revoked"

    with pytest.raises(AgentAuthenticationRejected):
        await harness.service.authenticate_access(registered.access_token)
    nonce = "cmV2b2tlZC1ub25jZS0wMDAwMDAwMDAwMDA"
    timestamp = int(harness.clock().timestamp())
    with pytest.raises(AgentAuthenticationRejected):
        await harness.service.refresh(
            agent_id=registered.agent_id,
            refresh_token=registered.refresh_token,
            nonce=nonce,
            timestamp=timestamp,
            signature_b64=refresh_signature(
                harness,
                agent_id=registered.agent_id,
                nonce=nonce,
                timestamp=timestamp,
            ),
        )


@pytest.mark.asyncio
async def test_agent_views_and_repr_redact_credentials_and_public_key(
    harness: ServiceHarness,
) -> None:
    issued, registered = await register_agent(harness)
    public_key_b64 = encode_ed25519_public_key(harness.private_key.public_key())

    views = await harness.service.list_agents(team_id=TEAM_A_ID)
    rendered = repr((issued, registered, views))

    assert len(views) == 1
    assert views[0].name == "Ray Mac"
    assert issued.registration_code not in rendered
    assert registered.access_token not in rendered
    assert registered.refresh_token not in rendered
    assert public_key_b64 not in rendered


@pytest.mark.asyncio
async def test_cross_team_mutation_is_nondisclosing_not_found(
    harness: ServiceHarness,
) -> None:
    _, registered = await register_agent(harness)

    with pytest.raises(AgentNotFoundError):
        await harness.service.rename(
            team_id=TEAM_B_ID,
            agent_id=registered.agent_id,
            name="Other name",
        )
    with pytest.raises(AgentNotFoundError):
        await harness.service.revoke(
            team_id=TEAM_B_ID,
            agent_id=registered.agent_id,
        )


@pytest.mark.asyncio
async def test_agent_names_are_normalized_and_unique_per_team(
    harness: ServiceHarness,
) -> None:
    await harness.service.create_registration_code(
        team_id=TEAM_A_ID,
        owner_user_id=USER_ID,
        name="  Ray Mac  ",
    )

    with pytest.raises(AgentNameConflictError):
        await harness.service.create_registration_code(
            team_id=TEAM_A_ID,
            owner_user_id=USER_ID,
            name="Ray Mac",
        )
