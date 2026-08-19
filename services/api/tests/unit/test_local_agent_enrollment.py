from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.local_agent_enrollment import (
    LocalAgentEnrollmentBusy,
    LocalAgentEnrollmentBroker,
)


NOW = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
TEAM_A = UUID("81000000-0000-4000-8000-000000000001")
TEAM_B = UUID("81000000-0000-4000-8000-000000000002")
USER_A = UUID("80000000-0000-4000-8000-000000000001")
USER_B = UUID("80000000-0000-4000-8000-000000000002")
ENROLLMENT_ID = UUID("98000000-0000-4000-8000-000000000001")


@pytest.mark.asyncio
async def test_one_user_opens_one_slot_and_the_next_agent_claims_it() -> None:
    broker = LocalAgentEnrollmentBroker(
        clock=lambda: NOW,
        uuid_factory=lambda: ENROLLMENT_ID,
    )

    opened = await broker.open(
        team_id=TEAM_A,
        owner_user_id=USER_A,
        name="测试电脑",
    )

    assert opened.expires_at == NOW + timedelta(minutes=10)
    assert await broker.status(TEAM_A) == opened.public_view()
    assert await broker.status(TEAM_B) is None
    with pytest.raises(LocalAgentEnrollmentBusy, match="unavailable"):
        await broker.open(
            team_id=TEAM_B,
            owner_user_id=USER_B,
            name="另一台电脑",
        )

    claimed = await broker.claim()
    assert claimed == opened
    assert await broker.claim() is None
    await broker.complete(opened.enrollment_id)
    assert await broker.status(TEAM_A) is None


@pytest.mark.asyncio
async def test_failed_claim_can_retry_until_the_slot_expires() -> None:
    now = NOW
    broker = LocalAgentEnrollmentBroker(
        clock=lambda: now,
        uuid_factory=lambda: ENROLLMENT_ID,
    )
    opened = await broker.open(
        team_id=TEAM_A,
        owner_user_id=USER_A,
        name="测试电脑",
    )
    assert await broker.claim() == opened

    await broker.release(opened.enrollment_id)
    assert await broker.claim() == opened
    await broker.release(opened.enrollment_id)
    now = NOW + timedelta(minutes=10)

    assert await broker.claim() is None
    assert await broker.status(TEAM_A) is None
