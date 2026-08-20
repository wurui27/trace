from __future__ import annotations

import asyncio

import pytest

from perfpilot_api.local_cancellation import CancellationTarget, cancel_targets


@pytest.mark.asyncio
async def test_cancel_closes_locally_when_remote_never_acknowledges() -> None:
    started = asyncio.Event()

    async def block() -> None:
        started.set()
        await asyncio.Event().wait()

    result = await cancel_targets(
        (CancellationTarget("agent_capture", block),),
        timeout_seconds=0.01,
    )

    assert result.accepted is True
    assert result.failed == ()
    assert result.pending_cleanup == ("agent_capture",)
    assert started.is_set()


@pytest.mark.asyncio
async def test_one_failed_target_does_not_block_other_cleanup() -> None:
    completed = False

    async def fail() -> None:
        raise RuntimeError("private failure")

    async def complete() -> None:
        nonlocal completed
        completed = True

    result = await cancel_targets(
        (
            CancellationTarget("source", fail),
            CancellationTarget("smartperfetto", complete),
        ),
        timeout_seconds=0.1,
    )

    assert completed is True
    assert result.accepted is True
    assert result.failed == ("source",)
    assert result.pending_cleanup == ()


@pytest.mark.asyncio
async def test_repeated_cancellation_is_stably_accepted() -> None:
    calls = 0

    async def complete() -> None:
        nonlocal calls
        calls += 1

    target = CancellationTarget("source", complete)
    first = await cancel_targets((target,), timeout_seconds=0.1)
    second = await cancel_targets((target,), timeout_seconds=0.1)

    assert first.accepted is second.accepted is True
    assert first.failed == second.failed == ()
    assert calls == 2


@pytest.mark.asyncio
async def test_empty_cleanup_set_is_stably_accepted() -> None:
    result = await cancel_targets((), timeout_seconds=0.1)

    assert result.accepted is True
    assert result.failed == ()
    assert result.pending_cleanup == ()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed() -> None:
    started = asyncio.Event()
    released = asyncio.Event()

    async def block() -> None:
        started.set()
        await released.wait()

    task = asyncio.create_task(
        cancel_targets(
            (CancellationTarget("agent_capture", block),),
            timeout_seconds=10,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_invalid_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="cancellation timeout rejected"):
        await cancel_targets((), timeout_seconds=0)
