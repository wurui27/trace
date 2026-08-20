from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeVar


StageState = Literal[
    "running",
    "slow",
    "waiting_for_upstream",
    "failed",
]


@dataclass(frozen=True, slots=True)
class StagePolicy:
    total_timeout_seconds: float | None
    idle_warning_seconds: float
    idle_escalation_seconds: float
    control_retry_attempts: int

    def __post_init__(self) -> None:
        values = (
            self.idle_warning_seconds,
            self.idle_escalation_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            for value in values
        ):
            raise ValueError("stage policy rejected")
        if self.idle_escalation_seconds < self.idle_warning_seconds:
            raise ValueError("stage policy rejected")
        if (
            self.total_timeout_seconds is not None
            and (
                isinstance(self.total_timeout_seconds, bool)
                or not isinstance(self.total_timeout_seconds, (int, float))
                or self.total_timeout_seconds <= 0
            )
        ):
            raise ValueError("stage policy rejected")
        if (
            isinstance(self.control_retry_attempts, bool)
            or not isinstance(self.control_retry_attempts, int)
            or self.control_retry_attempts < 1
        ):
            raise ValueError("stage policy rejected")


SMARTPERFETTO_POLICY = StagePolicy(
    total_timeout_seconds=None,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
SOURCE_POLICY = StagePolicy(
    total_timeout_seconds=None,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
AI_POLICY = StagePolicy(
    total_timeout_seconds=None,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
DEVICE_CLAIM_POLICY = StagePolicy(
    total_timeout_seconds=300,
    idle_warning_seconds=60,
    idle_escalation_seconds=180,
    control_retry_attempts=2,
)
DEVICE_CAPTURE_POLICY = StagePolicy(
    total_timeout_seconds=1800,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
REPORT_POLICY = StagePolicy(
    total_timeout_seconds=300,
    idle_warning_seconds=60,
    idle_escalation_seconds=180,
    control_retry_attempts=2,
)


@dataclass(frozen=True, slots=True)
class ActivityState:
    stage_state: StageState
    waiting_for: str | None
    progress_summary: str
    deadline_exceeded: bool


def _seconds_between(later: datetime, earlier: datetime) -> float:
    if later.tzinfo is None or earlier.tzinfo is None:
        raise ValueError("activity timestamp rejected")
    return max(0.0, (later - earlier).total_seconds())


def classify_activity(
    *,
    policy: StagePolicy,
    started_at: datetime,
    last_progress_at: datetime,
    now: datetime,
    waiting_for: str | None,
) -> ActivityState:
    elapsed = _seconds_between(now, started_at)
    idle = _seconds_between(now, last_progress_at)
    if policy.total_timeout_seconds is not None and elapsed > policy.total_timeout_seconds:
        return ActivityState(
            stage_state="failed",
            waiting_for=waiting_for,
            progress_summary="阶段等待已超过配置截止时间",
            deadline_exceeded=True,
        )
    if idle >= policy.idle_escalation_seconds:
        return ActivityState(
            stage_state="waiting_for_upstream",
            waiting_for=waiting_for,
            progress_summary="上游仍在处理，暂未收到新的进度",
            deadline_exceeded=False,
        )
    if idle >= policy.idle_warning_seconds:
        return ActivityState(
            stage_state="slow",
            waiting_for=waiting_for,
            progress_summary="处理时间较长，任务仍在继续",
            deadline_exceeded=False,
        )
    return ActivityState(
        stage_state="running",
        waiting_for=None,
        progress_summary="任务仍在持续处理",
        deadline_exceeded=False,
    )


T = TypeVar("T")


async def run_control_operation(
    operation: Callable[[str], Awaitable[T]],
    *,
    idempotency_key: str,
    policy: StagePolicy,
    retryable: tuple[type[BaseException], ...],
) -> T:
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("control operation rejected")
    if not retryable:
        raise ValueError("control operation rejected")
    for attempt in range(1, policy.control_retry_attempts + 1):
        try:
            return await operation(idempotency_key)
        except retryable:
            if attempt >= policy.control_retry_attempts:
                raise
    raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class SupervisedTask:
    key: str
    stage: str
    started_at: datetime
    last_progress_at: datetime
    waiting_for: str | None
    upstream_run_id: str | None = None
    cancel_requested: bool = False


LoadActive = Callable[
    [], Iterable[SupervisedTask] | Awaitable[Iterable[SupervisedTask]]
]
ObserveUpstream = Callable[[SupervisedTask], Awaitable[datetime | None]]
SaveActivity = Callable[[SupervisedTask, ActivityState], Awaitable[None]]
CancelTask = Callable[[SupervisedTask], Awaitable[None]]


class DurableTaskSupervisor:
    def __init__(
        self,
        *,
        load_active: LoadActive,
        observe_upstream: ObserveUpstream,
        save_activity: SaveActivity,
        cancel_task: CancelTask | None = None,
        policies: dict[str, StagePolicy] | None = None,
    ) -> None:
        self._load_active = load_active
        self._observe_upstream = observe_upstream
        self._save_activity = save_activity
        self._cancel_task = cancel_task
        self._policies = policies or {
            "device_claim": DEVICE_CLAIM_POLICY,
            "device_capture": DEVICE_CAPTURE_POLICY,
            "smartperfetto": SMARTPERFETTO_POLICY,
            "source_code": SOURCE_POLICY,
            "perfpilot_ai": AI_POLICY,
            "report": REPORT_POLICY,
        }

    async def tick(self, now: datetime) -> None:
        loaded = self._load_active()
        tasks = await loaded if inspect.isawaitable(loaded) else loaded
        for task in tuple(tasks):
            if task.cancel_requested:
                if self._cancel_task is not None:
                    await self._cancel_task(task)
                continue
            policy = self._policies.get(task.stage)
            if policy is None:
                continue
            last_progress_at = task.last_progress_at
            if task.stage in {"smartperfetto", "source_code", "perfpilot_ai"}:
                observed_at = await self._observe_upstream(task)
                if observed_at is not None:
                    last_progress_at = observed_at
            state = classify_activity(
                policy=policy,
                started_at=task.started_at,
                last_progress_at=last_progress_at,
                now=now,
                waiting_for=task.waiting_for,
            )
            await self._save_activity(task, state)


__all__ = [
    "AI_POLICY",
    "DEVICE_CAPTURE_POLICY",
    "DEVICE_CLAIM_POLICY",
    "REPORT_POLICY",
    "SMARTPERFETTO_POLICY",
    "SOURCE_POLICY",
    "ActivityState",
    "DurableTaskSupervisor",
    "StagePolicy",
    "SupervisedTask",
    "classify_activity",
    "run_control_operation",
]
