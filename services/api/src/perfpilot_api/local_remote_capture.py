from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID


T = TypeVar("T")


class RemoteCaptureRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteCaptureContext:
    team_id: UUID
    analysis_id: UUID
    generation: int

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("remote capture context rejected")

    @property
    def key(self) -> tuple[UUID, UUID]:
        return (self.team_id, self.analysis_id)


class RemoteCaptureCoordinator:
    def __init__(self) -> None:
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}

    def _lock(self, context: RemoteCaptureContext) -> asyncio.Lock:
        return self._locks.setdefault(context.key, asyncio.Lock())

    async def _run(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool] | None,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        async with self._lock(context):
            if guard is not None and not guard():
                raise RemoteCaptureRejected("remote capture rejected")
            return await operation()

    async def finalize(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=guard, operation=operation)

    async def reconcile(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=guard, operation=operation)

    async def cancel(
        self,
        context: RemoteCaptureContext,
        *,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=None, operation=operation)

    async def accept_completion(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=guard, operation=operation)

    def discard(self, context: RemoteCaptureContext) -> None:
        lock = self._locks.get(context.key)
        if lock is not None and not lock.locked():
            self._locks.pop(context.key, None)


__all__ = [
    "RemoteCaptureContext",
    "RemoteCaptureCoordinator",
    "RemoteCaptureRejected",
]
