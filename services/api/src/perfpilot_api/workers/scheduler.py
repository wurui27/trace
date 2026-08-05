from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator, Protocol
from uuid import UUID

from redis import asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.config import get_settings
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.services.agent_tasks import (
    AgentTaskRepository,
    AgentTaskWakeup,
    RedisAgentTaskWakeup,
    SQLAlchemyAgentTaskRepository,
    ScheduledAgentTask,
)


class _TenantRouterUnavailable:
    @asynccontextmanager
    async def session(self, _team_id: UUID) -> AsyncIterator[AsyncSession]:
        raise RuntimeError("Scheduler does not load tenant task projections")
        yield  # pragma: no cover


class Scheduler:
    def __init__(
        self,
        *,
        repository: AgentTaskRepository,
        wakeup: AgentTaskWakeup,
    ) -> None:
        self._repository = repository
        self._wakeup = wakeup

    async def run_once(self) -> ScheduledAgentTask | None:
        scheduled = await self._repository.schedule(
            analysis_id=None,
            now=datetime.now(UTC),
        )
        if scheduled is not None:
            await self._wakeup.wake(scheduled.agent_id)
        return scheduled


class _Scheduler(Protocol):
    async def run_once(self) -> ScheduledAgentTask | None: ...


class SchedulerWorker:
    def __init__(
        self,
        *,
        scheduler: _Scheduler,
        idle_poll_seconds: float = 1.0,
        failure_backoff_seconds: float = 2.0,
    ) -> None:
        if idle_poll_seconds <= 0 or failure_backoff_seconds <= 0:
            raise ValueError("Scheduler intervals must be positive")
        self._scheduler = scheduler
        self._idle_poll_seconds = idle_poll_seconds
        self._failure_backoff_seconds = failure_backoff_seconds

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            try:
                scheduled = await self._scheduler.run_once()
                delay = 0 if scheduled is not None else self._idle_poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = self._failure_backoff_seconds
            if delay == 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
            except TimeoutError:
                pass


async def _run() -> None:
    settings = get_settings()
    engine = create_control_engine(settings.control_database_url.get_secret_value())
    redis_client = redis.from_url(settings.redis_url.get_secret_value())
    try:
        repository = SQLAlchemyAgentTaskRepository(
            control_session_factory=create_control_session_factory(engine),
            tenant_router=_TenantRouterUnavailable(),  # type: ignore[arg-type]
        )
        scheduler = Scheduler(
            repository=repository,
            wakeup=RedisAgentTaskWakeup(redis_client),
        )
        await SchedulerWorker(scheduler=scheduler).run_forever()
    finally:
        await redis_client.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Lease queued device analyses to Agents")
    parser.parse_args(argv)
    asyncio.run(_run())


__all__ = ["Scheduler", "SchedulerWorker", "main"]
