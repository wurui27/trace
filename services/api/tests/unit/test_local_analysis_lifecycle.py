from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from perfpilot_api.local_analysis_lifecycle import (
    AnalysisLifecycleCoordinator,
    AnalysisLifecycleError,
    AnalysisState,
    LifecycleSnapshot,
    apply_transition,
    request_cancel,
)


ANALYSIS_ID = UUID("70000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 20, tzinfo=UTC)


def snapshot(
    state: AnalysisState = "analyzing",
    *,
    generation: int = 1,
    canceled: bool = False,
    report_available: bool = False,
) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        analysis_id=ANALYSIS_ID,
        state=state,
        generation=generation,
        cancel_requested_at=NOW if canceled else None,
        report_available=report_available,
    )


def test_terminal_analysis_cannot_return_to_active_state() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(
            snapshot("completed", report_available=True),
            target="analyzing",
            now=NOW,
        )


def test_cancel_marker_rejects_new_work_and_late_report() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(
            snapshot(canceled=True),
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )


def test_old_generation_cannot_publish() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis generation rejected"):
        apply_transition(
            snapshot(generation=2),
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )


def test_report_and_terminal_state_close_together() -> None:
    result = apply_transition(
        snapshot(),
        target="completed",
        now=NOW,
        result_generation=1,
        publish_report=True,
    )

    assert result.state == "completed"
    assert result.report_available is True
    assert result.completed_at == NOW


def test_repeated_completed_transition_is_idempotent() -> None:
    completed = snapshot("completed", report_available=True)

    assert (
        apply_transition(
            completed,
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )
        is completed
    )


def test_repeated_cancel_request_and_canceled_transition_are_idempotent() -> None:
    marked = request_cancel(snapshot(), now=NOW)
    assert request_cancel(marked, now=NOW) is marked

    canceled = apply_transition(marked, target="canceled", now=NOW)
    assert apply_transition(canceled, target="canceled", now=NOW) is canceled


def test_creating_cannot_skip_directly_to_analyzing() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(snapshot("creating"), target="analyzing", now=NOW)


def test_active_state_cannot_claim_report_is_available() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(
            snapshot(report_available=True),
            target="analyzing",
            now=NOW,
        )


def test_coordinator_is_the_runtime_transition_and_cancel_boundary() -> None:
    coordinator = AnalysisLifecycleCoordinator()
    marked = coordinator.cancel(snapshot(), now=NOW)

    assert marked.cancel_requested_at == NOW
    assert coordinator.transition(marked, target="canceled", now=NOW).state == "canceled"
