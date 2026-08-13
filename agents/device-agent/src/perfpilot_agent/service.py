from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from perfpilot_agent.control_client import (
    ControlClientError,
    DeviceTaskExecuteResponse,
    HeartbeatResponse,
    LeaseLost,
    TaskCancellationResponse,
    TaskExecuteResponse,
    TaskPollResponse,
    TaskWaitResponse,
    SourceTaskExecuteResponse,
)
from perfpilot_agent.credentials import AgentCredentials, CredentialStoreError
from perfpilot_agent.executor import TaskExecutionError
from perfpilot_agent.security import TaskRejected, TaskVerifier, VerifiedSourceTask
from perfpilot_agent.state import AgentRuntimeState, RuntimeStateError

_LOGGER = logging.getLogger("perfpilot-agent.service")


class TaskLoopControl(Protocol):
    @property
    def credentials(self) -> AgentCredentials: ...

    async def poll_task(self, *, wait_seconds: int = 20) -> TaskPollResponse: ...

    async def acknowledge_cancellation(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
    ) -> object: ...


class TaskLoopExecutor(Protocol):
    async def run(self, task: object) -> None: ...


class SourceTaskLoopExecutor(Protocol):
    async def run(self, task: VerifiedSourceTask, *, lease_token: str) -> None: ...


class SourceTaskCompletionControl(Protocol):
    async def complete_source_task(
        self,
        *,
        execution_id: UUID,
        lease_version: int,
        lease_token: str,
        completion: dict[str, object],
    ) -> object: ...


class SourceTaskExecutor:
    """Compatibility shell for callers that have not wired a source registry yet.

    The production CLI uses :class:`SourceTaskRunner`; retaining this bounded
    shell keeps older embedded integrations from silently touching source paths.
    """

    def __init__(self, *, control: SourceTaskCompletionControl) -> None:
        self._control = control

    async def run(self, task: VerifiedSourceTask, *, lease_token: str) -> None:
        result: dict[str, object]
        if task.task_type == "patch_verification":
            patch = getattr(task, "patch", "")
            result = {
                "verification_state": "unavailable",
                "exit_code": None,
                "duration_ms": None,
                "profile_id": str(task.validation_profile_id),
                "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                "log_summary": None,
            }
        else:
            result = {
                "failure_code": "source_runner_unavailable",
                "retryable": False,
            }
        await self._control.complete_source_task(
            execution_id=task.execution_id,
            lease_version=task.lease_version,
            lease_token=lease_token,
            completion={
                "schema_version": "1.0",
                "task_type": task.task_type,
                "execution_id": str(task.execution_id),
                "analysis_id": str(task.analysis_id),
                "workspace_id": str(task.workspace_id),
                "lease_version": task.lease_version,
                "state": "failed",
                "result": result,
            },
        )


class TaskLoop:
    def __init__(
        self,
        *,
        control: TaskLoopControl,
        executor: TaskLoopExecutor,
        source_executor: SourceTaskLoopExecutor | None = None,
        state: AgentRuntimeState,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._control = control
        self._executor = executor
        self._source_executor = source_executor
        self._state = state
        self._clock = clock
        self._sleep = sleep

    async def poll_once(self) -> bool:
        response = await self._control.poll_task(wait_seconds=20)
        if isinstance(response, TaskWaitResponse):
            await self._sleep(response.retry_after_seconds)
            return False
        if isinstance(response, TaskCancellationResponse):
            if self._state.execution_id not in {None, response.execution_id}:
                raise LeaseLost
            await self._control.acknowledge_cancellation(
                execution_id=response.execution_id,
                lease_version=response.lease_version,
            )
            return True
        if not isinstance(response, (TaskExecuteResponse, DeviceTaskExecuteResponse)):
            if not isinstance(response, SourceTaskExecuteResponse):
                raise ControlClientError
            credentials = self._control.credentials
            if credentials.team_id is None:
                raise TaskRejected
            snapshot = response.snapshot
            task = TaskVerifier(
                public_key_b64=credentials.task_signing_key.public_key_b64,
                kid=credentials.task_signing_key.kid,
                clock=self._clock,
            ).verify_source(
                snapshot,
                response.signature_b64,
                expected_agent_id=credentials.agent_id,
                expected_team_id=credentials.team_id,
            )
            if not isinstance(task, VerifiedSourceTask) or self._source_executor is None:
                raise TaskRejected
            if self._state.execution_id is not None:
                raise RuntimeStateError
            self._state.set_execution(task.execution_id)
            try:
                await self._source_executor.run(task, lease_token=response.lease_token)
            finally:
                self._state.set_execution(None)
            return True
        credentials = self._control.credentials
        verifier = TaskVerifier(
            public_key_b64=credentials.task_signing_key.public_key_b64,
            kid=credentials.task_signing_key.kid,
            clock=self._clock,
        )
        if isinstance(response, DeviceTaskExecuteResponse):
            if credentials.team_id is None:
                raise TaskRejected
            task = verifier.verify_device(
                response.snapshot,
                response.signature_b64,
                expected_agent_id=credentials.agent_id,
                expected_team_id=credentials.team_id,
                known_device_digests=self._state.known_device_digests(),
            )
        else:
            task = verifier.verify(
                response.snapshot_jws,
                expected_agent_id=credentials.agent_id,
                expected_team_id=credentials.team_id,
                expected_lease_version=None,
                known_device_digests=self._state.known_device_digests(),
            )
            if task.expires_at != response.lease_expires_at.astimezone(UTC):
                raise TaskRejected
        await self._executor.run(task)
        return True


class HeartbeatLoopPublisher(Protocol):
    async def publish(self) -> HeartbeatResponse: ...


class CredentialRefreshControl(Protocol):
    async def refresh_credentials(self, *, force: bool = False) -> AgentCredentials: ...


class AgentService:
    def __init__(
        self,
        *,
        heartbeat: HeartbeatLoopPublisher,
        tasks: TaskLoop,
        credentials: CredentialRefreshControl,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._heartbeat = heartbeat
        self._tasks = tasks
        self._credentials = credentials
        self._stop_event = stop_event or asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def _pause(self, seconds: float) -> None:
        try:
            async with asyncio.timeout(seconds):
                await self._stop_event.wait()
        except TimeoutError:
            pass

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                response = await self._heartbeat.publish()
                delay = response.next_heartbeat_seconds
            except (ControlClientError, RuntimeStateError, OSError, ValueError):
                _LOGGER.warning("Agent heartbeat failed")
                delay = 1
            await self._pause(delay)

    async def _task_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tasks.poll_once()
            except (
                ControlClientError,
                CredentialStoreError,
                LeaseLost,
                RuntimeStateError,
                TaskExecutionError,
                TaskRejected,
                OSError,
                ValueError,
            ):
                _LOGGER.warning("Agent task cycle failed")
                await self._pause(1)

    async def _credential_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._credentials.refresh_credentials()
                delay = 30
            except (ControlClientError, CredentialStoreError, OSError, ValueError):
                _LOGGER.warning("Agent credential refresh failed")
                delay = 1
            await self._pause(delay)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            workers = (
                group.create_task(self._heartbeat_loop()),
                group.create_task(self._task_loop()),
                group.create_task(self._credential_loop()),
            )
            await self._stop_event.wait()
            for worker in workers:
                worker.cancel()


__all__ = ["AgentService", "SourceTaskExecutor", "TaskLoop"]
