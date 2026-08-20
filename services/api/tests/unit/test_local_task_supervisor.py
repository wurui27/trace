from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from perfpilot_api.local_task_supervisor import (
    AI_POLICY,
    SMARTPERFETTO_POLICY,
    SOURCE_POLICY,
    ActivityState,
    DurableTaskSupervisor,
    StagePolicy,
    SupervisedTask,
    classify_activity,
    run_control_operation,
)


NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "policy",
    [SMARTPERFETTO_POLICY, SOURCE_POLICY, AI_POLICY],
)
def test_unbounded_analysis_stages_warn_without_failing(policy: StagePolicy) -> None:
    active = classify_activity(
        policy=policy,
        started_at=NOW - timedelta(hours=1),
        last_progress_at=NOW - timedelta(minutes=2),
        now=NOW,
        waiting_for="smartperfetto",
    )
    slow = classify_activity(
        policy=policy,
        started_at=NOW - timedelta(hours=1),
        last_progress_at=NOW - timedelta(minutes=3),
        now=NOW,
        waiting_for="smartperfetto",
    )
    waiting = classify_activity(
        policy=policy,
        started_at=NOW - timedelta(days=1),
        last_progress_at=NOW - timedelta(minutes=10),
        now=NOW,
        waiting_for="smartperfetto",
    )

    assert active.stage_state == "running"
    assert slow.stage_state == "slow"
    assert waiting.stage_state == "waiting_for_upstream"
    assert not active.deadline_exceeded
    assert not slow.deadline_exceeded
    assert not waiting.deadline_exceeded


def test_new_progress_clears_activity_warning() -> None:
    state = classify_activity(
        policy=SMARTPERFETTO_POLICY,
        started_at=NOW - timedelta(hours=1),
        last_progress_at=NOW,
        now=NOW,
        waiting_for="smartperfetto",
    )

    assert state == ActivityState(
        stage_state="running",
        waiting_for=None,
        progress_summary="任务仍在持续处理",
        deadline_exceeded=False,
    )


def test_bounded_device_claim_expires() -> None:
    policy = StagePolicy(
        total_timeout_seconds=15,
        idle_warning_seconds=5,
        idle_escalation_seconds=10,
        control_retry_attempts=2,
    )

    state = classify_activity(
        policy=policy,
        started_at=NOW - timedelta(seconds=16),
        last_progress_at=NOW - timedelta(seconds=2),
        now=NOW,
        waiting_for="device",
    )

    assert state.stage_state == "failed"
    assert state.deadline_exceeded
    assert state.waiting_for == "device"


@pytest.mark.asyncio
async def test_control_operation_retries_at_most_twice_with_same_key() -> None:
    calls: list[str] = []

    class RetryableControlError(RuntimeError):
        pass

    async def operation(idempotency_key: str) -> None:
        calls.append(idempotency_key)
        raise RetryableControlError

    with pytest.raises(RetryableControlError):
        await run_control_operation(
            operation,
            idempotency_key="analysis-1:cancel",
            policy=SMARTPERFETTO_POLICY,
            retryable=(RetryableControlError,),
        )

    assert calls == ["analysis-1:cancel", "analysis-1:cancel"]


@pytest.mark.asyncio
async def test_supervisor_reconciles_the_same_upstream_run() -> None:
    task = SupervisedTask(
        key="team-1:analysis-1",
        stage="smartperfetto",
        started_at=NOW - timedelta(hours=1),
        last_progress_at=NOW - timedelta(minutes=10),
        waiting_for="smartperfetto",
        upstream_run_id="run-123",
    )
    observed: list[str] = []
    saved: list[tuple[str, ActivityState]] = []

    async def observe(current: SupervisedTask) -> datetime | None:
        assert current is task
        observed.append(current.upstream_run_id or "")
        return NOW

    async def save(current: SupervisedTask, state: ActivityState) -> None:
        saved.append((current.key, state))

    supervisor = DurableTaskSupervisor(
        load_active=lambda: (task,),
        observe_upstream=observe,
        save_activity=save,
    )

    await supervisor.tick(NOW)

    assert observed == ["run-123"]
    assert saved == [
        (
            task.key,
            ActivityState(
                stage_state="running",
                waiting_for=None,
                progress_summary="任务仍在持续处理",
                deadline_exceeded=False,
            ),
        )
    ]
