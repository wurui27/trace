from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CancellationTarget:
    name: str
    cancel: Callable[[], Awaitable[None]]

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 64 or not callable(self.cancel):
            raise ValueError("cancellation target rejected")


@dataclass(frozen=True, slots=True)
class CancellationResult:
    accepted: bool
    failed: tuple[str, ...]
    pending_cleanup: tuple[str, ...]


async def cancel_targets(
    targets: tuple[CancellationTarget, ...],
    *,
    timeout_seconds: float = 10.0,
) -> CancellationResult:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("cancellation timeout rejected")
    names = tuple(item.name for item in targets)
    if len(names) != len(set(names)):
        raise ValueError("cancellation target rejected")
    tasks = {
        asyncio.create_task(item.cancel(), name=f"cancel-{item.name}"): item.name
        for item in targets
    }
    if not tasks:
        return CancellationResult(accepted=True, failed=(), pending_cleanup=())
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failed = tuple(
        sorted(
            tasks[task]
            for task in done
            if not task.cancelled() and task.exception() is not None
        )
    )
    pending_cleanup = tuple(sorted(tasks[task] for task in pending))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return CancellationResult(
        accepted=True,
        failed=failed,
        pending_cleanup=pending_cleanup,
    )


__all__ = ["CancellationResult", "CancellationTarget", "cancel_targets"]
