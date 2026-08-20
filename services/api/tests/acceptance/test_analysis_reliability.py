from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.local_analysis_lifecycle import (
    AnalysisLifecycleError,
    LifecycleSnapshot,
    apply_transition,
    request_cancel,
)
from perfpilot_api.local_task_supervisor import (
    AI_POLICY,
    SMARTPERFETTO_POLICY,
    SOURCE_POLICY,
    classify_activity,
)


ANALYSIS_ID = UUID("70000000-0000-4000-8000-000000000099")
NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _active(*, generation: int = 1) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        analysis_id=ANALYSIS_ID,
        state="analyzing",
        generation=generation,
        cancel_requested_at=None,
        report_available=False,
    )


@pytest.mark.parametrize("producer", ["agent", "ai"])
def test_cancel_wins_over_late_terminal_producers(producer: str) -> None:
    del producer
    marked = request_cancel(_active(), now=NOW)

    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(
            marked,
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )

    canceled = apply_transition(marked, target="canceled", now=NOW)
    assert canceled.state == "canceled"
    assert canceled.report_available is False


def test_report_response_loss_replays_the_same_terminal_generation() -> None:
    published = apply_transition(
        _active(generation=2),
        target="completed",
        now=NOW,
        result_generation=2,
        publish_report=True,
    )

    replayed = apply_transition(
        published,
        target="completed",
        now=NOW + timedelta(seconds=5),
        result_generation=2,
        publish_report=True,
    )
    assert replayed is published
    with pytest.raises(AnalysisLifecycleError, match="analysis generation rejected"):
        apply_transition(
            _active(generation=2),
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )


@pytest.mark.parametrize(
    ("policy", "waiting_for"),
    [
        (SMARTPERFETTO_POLICY, "smartperfetto"),
        (SOURCE_POLICY, "source_agent"),
        (AI_POLICY, "ai_provider"),
    ],
)
def test_unbounded_upstream_wait_recovers_when_progress_resumes(
    policy, waiting_for: str
) -> None:
    waiting = classify_activity(
        policy=policy,
        started_at=NOW - timedelta(hours=1),
        last_progress_at=NOW - timedelta(minutes=10),
        now=NOW,
        waiting_for=waiting_for,
    )
    active = classify_activity(
        policy=policy,
        started_at=NOW - timedelta(hours=1),
        last_progress_at=NOW + timedelta(seconds=1),
        now=NOW + timedelta(seconds=1),
        waiting_for=waiting_for,
    )

    assert waiting.stage_state == "waiting_for_upstream"
    assert waiting.deadline_exceeded is False
    assert active.stage_state == "running"
    assert active.waiting_for is None
