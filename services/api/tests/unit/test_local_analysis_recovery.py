from __future__ import annotations

import asyncio

import pytest

from perfpilot_api.local_analysis_recovery import (
    RecoveryAction,
    RecoverySnapshot,
    plan_recovery,
    schedule_recovery_operation,
)


def _snapshot(**overrides: object) -> RecoverySnapshot:
    values: dict[str, object] = {
        "state": "analyzing",
        "canceled": False,
        "smartperfetto_state": "running",
        "evidence_manifest_present": False,
        "remote_manifest_present": False,
        "report_present": False,
        "source_state": "not_requested",
        "remote_publication": "not_requested",
        "upstream_run_resumable": False,
        "identity_valid": True,
        "artifacts_valid": True,
    }
    values.update(overrides)
    return RecoverySnapshot(**values)


def test_completed_smartperfetto_resumes_synthesis_without_recapture() -> None:
    actions = plan_recovery(
        _snapshot(
            smartperfetto_state="completed",
            evidence_manifest_present=True,
            source_state="available",
            remote_publication="published",
        )
    )

    assert actions == (RecoveryAction.RESUME_SYNTHESIS,)


def test_running_smartperfetto_with_persisted_run_resumes_polling() -> None:
    actions = plan_recovery(
        _snapshot(
            smartperfetto_state="running",
            upstream_run_resumable=True,
        )
    )

    assert actions == (RecoveryAction.RESUME_SMARTPERFETTO,)


def test_completed_smartperfetto_refetches_when_evidence_was_not_committed() -> None:
    actions = plan_recovery(
        _snapshot(
            smartperfetto_state="completed",
            evidence_manifest_present=False,
            upstream_run_resumable=True,
        )
    )

    assert actions == (RecoveryAction.RESUME_SMARTPERFETTO,)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (_snapshot(canceled=True), (RecoveryAction.CLOSE_CANCELED,)),
        (
            _snapshot(remote_publication="publishing"),
            (RecoveryAction.RECONCILE_PUBLICATION,),
        ),
        (
            _snapshot(
                remote_publication="published", remote_manifest_present=True
            ),
            (RecoveryAction.RESUME_REMOTE_ANALYSIS,),
        ),
        (_snapshot(report_present=True), (RecoveryAction.CLOSE_COMPLETED,)),
        (
            _snapshot(identity_valid=False),
            (RecoveryAction.FAIL_INVALID_RECOVERY,),
        ),
        (
            _snapshot(artifacts_valid=False),
            (RecoveryAction.FAIL_INVALID_RECOVERY,),
        ),
        (_snapshot(state="completed"), (RecoveryAction.NOOP,)),
    ],
)
def test_recovery_matrix_is_deterministic(
    snapshot: RecoverySnapshot, expected: tuple[RecoveryAction, ...]
) -> None:
    assert plan_recovery(snapshot) == expected
    assert plan_recovery(snapshot) == expected


def test_cancel_precedes_report_or_remote_recovery() -> None:
    actions = plan_recovery(
        _snapshot(
            canceled=True,
            report_present=True,
            remote_publication="published",
            remote_manifest_present=True,
        )
    )

    assert actions == (RecoveryAction.CLOSE_CANCELED,)


@pytest.mark.asyncio
async def test_scheduled_recovery_waits_for_gate_and_unregisters() -> None:
    gate = asyncio.Event()
    tasks: set[asyncio.Task[None]] = set()
    calls: list[str] = []

    async def operation() -> None:
        calls.append("resumed")

    task = schedule_recovery_operation(
        gate=gate,
        operation=operation,
        tasks=tasks,
    )
    await asyncio.sleep(0)
    assert calls == []
    assert task in tasks

    gate.set()
    await task
    await asyncio.sleep(0)

    assert calls == ["resumed"]
    assert task not in tasks
