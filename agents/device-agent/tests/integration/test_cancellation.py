from __future__ import annotations

import asyncio
from time import monotonic

import pytest

from perfpilot_agent.control_client import TaskCancellationResponse, TaskRenewalResponse
from perfpilot_agent.executor import ExecutionOutcome, TaskExecutor
from perfpilot_agent.security import TaskSnapshot
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding


class BlockingCapture:
    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.stopped = asyncio.Event()

    async def wait(self) -> ExecutionOutcome:
        await self.done.wait()
        return ExecutionOutcome(manifest={"schema_version": "1.0"})

    async def stop(self, reason: str) -> None:
        assert reason == "server_cancel"
        self.stopped.set()
        self.done.set()


class CaptureFactory:
    def __init__(self) -> None:
        self.capture = BlockingCapture()

    async def start(self, task: TaskSnapshot, *, serial: str) -> BlockingCapture:
        return self.capture


class CancelControl:
    def __init__(self, task: TaskSnapshot) -> None:
        self.task = task
        self.polls = 0
        self.acknowledged = []
        self.completed = []

    async def poll_task(self, *, wait_seconds: int = 0):
        self.polls += 1
        return TaskCancellationResponse(
            schema_version="1.0",
            action="cancel",
            execution_id=self.task.execution_id,
            lease_version=self.task.lease_version,
            reason_code="analysis_canceled",
        )

    async def renew_task(self, *, execution_id, lease_version):
        return TaskRenewalResponse(
            schema_version="1.0",
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at="2026-08-05T08:01:00Z",
            renew_after_seconds=20,
        )

    async def acknowledge_cancellation(self, *, execution_id, lease_version):
        self.acknowledged.append(execution_id)

    async def complete_execution(self, *, execution_id, lease_version, manifest):
        self.completed.append(execution_id)


@pytest.mark.asyncio
async def test_server_cancel_stops_capture_within_five_seconds(task_claims) -> None:
    task = TaskSnapshot.model_validate(task_claims)
    state = AgentRuntimeState()
    state.replace_device_bindings(
        (
            DeviceBinding(
                client_ref="74000000-0000-4000-8000-000000000001",
                device_id="72000000-0000-4000-8000-000000000001",
                device_digest=task.device_digest,
                serial="device-under-test",
            ),
        )
    )
    control = CancelControl(task)
    runner = CaptureFactory()
    executor = TaskExecutor(
        control=control,
        runner=runner,
        state=state,
        control_poll_interval_seconds=0.01,
        renewal_interval_seconds=20,
        stop_timeout_seconds=0.05,
    )

    started = monotonic()
    await asyncio.wait_for(executor.run(task), timeout=1)
    elapsed = monotonic() - started

    assert elapsed < 5
    assert runner.capture.stopped.is_set()
    assert control.acknowledged == [task.execution_id]
    assert control.completed == []
