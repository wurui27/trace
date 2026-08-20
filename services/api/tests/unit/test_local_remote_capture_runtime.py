from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from perfpilot_api.local_remote_capture import (
    RemoteCaptureContext,
    RemoteCaptureCoordinator,
    RemoteCaptureRejected,
)


TEAM_ID = UUID("21000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("31000000-0000-4000-8000-000000000001")


def _context(offset: int = 0) -> RemoteCaptureContext:
    return RemoteCaptureContext(
        team_id=TEAM_ID,
        analysis_id=UUID(int=ANALYSIS_ID.int + offset),
        generation=1,
    )


@pytest.mark.asyncio
async def test_concurrent_exact_finalize_enters_inspection_once() -> None:
    coordinator = RemoteCaptureCoordinator()
    phase = "not_requested"
    inspections = 0

    async def finalize() -> str:
        nonlocal phase, inspections
        if phase == "not_requested":
            inspections += 1
            await asyncio.sleep(0)
            phase = "published"
        return phase

    results = await asyncio.gather(
        coordinator.finalize(_context(), guard=lambda: True, operation=finalize),
        coordinator.finalize(_context(), guard=lambda: True, operation=finalize),
    )

    assert results == ["published", "published"]
    assert inspections == 1


@pytest.mark.asyncio
async def test_guard_rechecks_cancel_and_generation_inside_lock() -> None:
    coordinator = RemoteCaptureCoordinator()
    allowed = False
    called = False

    async def operation() -> None:
        nonlocal called
        called = True

    with pytest.raises(RemoteCaptureRejected, match="remote capture rejected"):
        await coordinator.finalize(
            _context(), guard=lambda: allowed, operation=operation
        )

    assert called is False


@pytest.mark.asyncio
async def test_different_analyses_are_not_globally_serialized() -> None:
    coordinator = RemoteCaptureCoordinator()
    entered: set[UUID] = set()
    both = asyncio.Event()

    async def operation(context: RemoteCaptureContext) -> None:
        entered.add(context.analysis_id)
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)

    await asyncio.gather(
        coordinator.finalize(
            _context(), guard=lambda: True, operation=lambda: operation(_context())
        ),
        coordinator.finalize(
            _context(1),
            guard=lambda: True,
            operation=lambda: operation(_context(1)),
        ),
    )

    assert entered == {ANALYSIS_ID, UUID(int=ANALYSIS_ID.int + 1)}


@pytest.mark.asyncio
async def test_reconcile_cancel_and_completion_share_exact_analysis_lock() -> None:
    coordinator = RemoteCaptureCoordinator()
    order: list[str] = []

    async def record(name: str) -> str:
        order.append(name)
        await asyncio.sleep(0)
        return name

    assert await coordinator.reconcile(
        _context(), guard=lambda: True, operation=lambda: record("reconcile")
    ) == "reconcile"
    assert await coordinator.cancel(
        _context(), operation=lambda: record("cancel")
    ) == "cancel"
    assert await coordinator.accept_completion(
        _context(), guard=lambda: True, operation=lambda: record("completion")
    ) == "completion"
    coordinator.discard(_context())

    assert order == ["reconcile", "cancel", "completion"]
