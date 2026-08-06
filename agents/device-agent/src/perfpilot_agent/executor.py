from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from perfpilot_agent.control_client import (
    ControlClientError,
    LeaseLost,
    TaskCancellationResponse,
    TaskPollResponse,
    TaskRenewResponse,
    TaskRenewalResponse,
)
from perfpilot_agent.security import TaskSnapshot
from perfpilot_agent.state import AgentRuntimeState, RuntimeStateError

StopReason = Literal["server_cancel", "lease_lost", "agent_shutdown", "execution_failed"]


class TaskExecutionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent task execution failed")


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    manifest: Mapping[str, object]


class RunningTask(Protocol):
    async def wait(self) -> ExecutionOutcome: ...

    async def stop(self, reason: StopReason) -> None: ...


class TaskRunner(Protocol):
    async def start(self, task: TaskSnapshot, *, serial: str) -> RunningTask: ...


class ExecutorControl(Protocol):
    async def poll_task(self, *, wait_seconds: int = 0) -> TaskPollResponse: ...

    async def renew_task(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
    ) -> TaskRenewResponse: ...

    async def acknowledge_cancellation(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
    ) -> object: ...

    async def complete_execution(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        manifest: Mapping[str, object],
    ) -> object: ...


class TaskExecutor:
    def __init__(
        self,
        *,
        control: ExecutorControl,
        runner: TaskRunner,
        state: AgentRuntimeState,
        control_poll_interval_seconds: float = 2.0,
        renewal_interval_seconds: float = 20.0,
        stop_timeout_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not 0 < control_poll_interval_seconds <= 2
            or not 0 < renewal_interval_seconds <= 20
            or not 0 < stop_timeout_seconds <= 2
        ):
            raise ValueError("task supervisor timing is invalid")
        self._control = control
        self._runner = runner
        self._state = state
        self._control_poll_interval_seconds = control_poll_interval_seconds
        self._renewal_interval_seconds = renewal_interval_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._monotonic = monotonic

    @staticmethod
    def _validate_renewal(task: TaskSnapshot, response: TaskRenewalResponse) -> None:
        if (
            response.execution_id != task.execution_id
            or response.lease_version != task.lease_version
        ):
            raise LeaseLost

    @staticmethod
    def _matching_cancellation(
        task: TaskSnapshot,
        response: TaskCancellationResponse,
    ) -> bool:
        return (
            response.execution_id == task.execution_id
            and response.lease_version == task.lease_version
        )

    async def _stop(self, running: RunningTask, reason: StopReason) -> None:
        try:
            async with asyncio.timeout(self._stop_timeout_seconds):
                await running.stop(reason)
        except TimeoutError:
            force_stop = getattr(running, "force_stop", None)
            if force_stop is None:
                raise TaskExecutionError from None
            try:
                async with asyncio.timeout(1.0):
                    await force_stop()
            except TimeoutError:
                raise TaskExecutionError from None

    async def _cancel(
        self,
        task: TaskSnapshot,
        running: RunningTask,
        response: TaskCancellationResponse,
    ) -> None:
        if not self._matching_cancellation(task, response):
            raise LeaseLost
        await self._stop(running, "server_cancel")
        await self._control.acknowledge_cancellation(
            execution_id=task.execution_id,
            lease_version=task.lease_version,
        )

    async def _renew(self, task: TaskSnapshot) -> TaskRenewResponse:
        response = await self._control.renew_task(
            execution_id=task.execution_id,
            lease_version=task.lease_version,
        )
        if isinstance(response, TaskRenewalResponse):
            self._validate_renewal(task, response)
        return response

    async def run(self, task: TaskSnapshot) -> None:
        if self._state.execution_id is not None:
            raise RuntimeStateError
        serial = self._state.serial_for_digest(task.device_digest)
        if serial is None:
            raise RuntimeStateError
        running: RunningTask | None = None
        completion: asyncio.Task[ExecutionOutcome] | None = None
        self._state.set_execution(task.execution_id)
        try:
            running = await self._runner.start(task, serial=serial)
            completion = asyncio.create_task(running.wait())
            next_renewal = self._monotonic() + self._renewal_interval_seconds
            while True:
                now = self._monotonic()
                timeout = min(
                    self._control_poll_interval_seconds,
                    max(0.0, next_renewal - now),
                )
                done, _ = await asyncio.wait(
                    {completion},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if completion in done:
                    outcome = completion.result()
                    final_renewal = await self._renew(task)
                    if isinstance(final_renewal, TaskCancellationResponse):
                        await self._cancel(task, running, final_renewal)
                        return
                    await self._control.complete_execution(
                        execution_id=task.execution_id,
                        lease_version=task.lease_version,
                        manifest=outcome.manifest,
                    )
                    return

                if self._monotonic() >= next_renewal:
                    renewal = await self._renew(task)
                    if isinstance(renewal, TaskCancellationResponse):
                        await self._cancel(task, running, renewal)
                        return
                    next_renewal = self._monotonic() + min(
                        self._renewal_interval_seconds,
                        float(renewal.renew_after_seconds),
                    )

                control_state = await self._control.poll_task(wait_seconds=0)
                if isinstance(control_state, TaskCancellationResponse):
                    await self._cancel(task, running, control_state)
                    return
        except (LeaseLost, ControlClientError):
            if running is not None:
                await self._stop(running, "lease_lost")
            return
        except asyncio.CancelledError:
            if running is not None:
                await self._stop(running, "agent_shutdown")
            raise
        except BaseException:
            if running is not None:
                await self._stop(running, "execution_failed")
            raise
        finally:
            if completion is not None and not completion.done():
                completion.cancel()
            if completion is not None:
                await asyncio.gather(completion, return_exceptions=True)
            self._state.set_execution(None)


__all__ = [
    "ExecutionOutcome",
    "ExecutorControl",
    "RunningTask",
    "StopReason",
    "TaskExecutionError",
    "TaskExecutor",
    "TaskRunner",
]
