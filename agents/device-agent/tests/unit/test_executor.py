from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import pytest

from perfpilot_agent.control_client import (
    LeaseLost,
    TaskRenewalResponse,
    TaskWaitResponse,
)
from perfpilot_agent.executor import ExecutionOutcome, TaskExecutor
from perfpilot_agent.security import TaskSnapshot
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding


class FakeRunningTask:
    def __init__(self) -> None:
        self._finished = asyncio.Event()
        self.stop_reasons: list[str] = []

    async def wait(self) -> ExecutionOutcome:
        await self._finished.wait()
        return ExecutionOutcome(manifest={"schema_version": "1.0"})

    async def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        self._finished.set()


class FakeTaskRunner:
    def __init__(self) -> None:
        self.running = FakeRunningTask()
        self.serials: list[str] = []

    async def start(self, task: TaskSnapshot, *, serial: str) -> FakeRunningTask:
        self.serials.append(serial)
        return self.running


@dataclass
class FakeControl:
    renew_mode: Literal["ok", "lost"] = "ok"

    def __post_init__(self) -> None:
        self.complete_calls: list[object] = []
        self.cancel_ack_calls: list[object] = []

    async def poll_task(self, *, wait_seconds: int = 0) -> TaskWaitResponse:
        return TaskWaitResponse(
            schema_version="1.0",
            action="wait",
            retry_after_seconds=1,
        )

    async def renew_task(self, *, execution_id, lease_version):
        if self.renew_mode == "lost":
            raise LeaseLost
        return TaskRenewalResponse(
            schema_version="1.0",
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at="2026-08-05T08:01:00Z",
            renew_after_seconds=20,
        )

    async def acknowledge_cancellation(self, *, execution_id, lease_version):
        self.cancel_ack_calls.append(execution_id)

    async def complete_execution(self, *, execution_id, lease_version, manifest):
        self.complete_calls.append((execution_id, lease_version, manifest))


def _state(task: TaskSnapshot, serial: str = "device-under-test") -> AgentRuntimeState:
    state = AgentRuntimeState()
    state.replace_device_bindings(
        (
            DeviceBinding(
                client_ref="74000000-0000-4000-8000-000000000001",
                device_id="72000000-0000-4000-8000-000000000001",
                device_digest=task.device_digest,
                serial=serial,
            ),
        )
    )
    return state


@pytest.mark.asyncio
async def test_lease_loss_terminates_execution_and_blocks_completion(task_claims) -> None:
    task = TaskSnapshot.model_validate(task_claims)
    control = FakeControl(renew_mode="lost")
    runner = FakeTaskRunner()
    state = _state(task)
    executor = TaskExecutor(
        control=control,
        runner=runner,
        state=state,
        control_poll_interval_seconds=0.01,
        renewal_interval_seconds=0.01,
        stop_timeout_seconds=0.05,
    )

    await asyncio.wait_for(executor.run(task), timeout=1)

    assert runner.running.stop_reasons == ["lease_lost"]
    assert control.complete_calls == []
    assert control.cancel_ack_calls == []
    assert state.execution_id is None


@pytest.mark.asyncio
async def test_success_is_fenced_by_a_final_renewal(task_claims) -> None:
    task = TaskSnapshot.model_validate(task_claims)
    control = FakeControl()
    runner = FakeTaskRunner()
    state = _state(task)
    executor = TaskExecutor(
        control=control,
        runner=runner,
        state=state,
        control_poll_interval_seconds=0.01,
        renewal_interval_seconds=20,
        stop_timeout_seconds=0.05,
    )

    run = asyncio.create_task(executor.run(task))
    await asyncio.sleep(0)
    runner.running._finished.set()
    await asyncio.wait_for(run, timeout=1)

    assert len(control.complete_calls) == 1
    assert state.execution_id is None
