from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID


AnalysisState = Literal[
    "creating",
    "created",
    "uploading",
    "queued",
    "scheduled",
    "running",
    "analyzing",
    "completed",
    "partially_completed",
    "failed",
    "canceled",
    "deleted",
]

_ACTIVE = frozenset(
    {
        "creating",
        "created",
        "uploading",
        "queued",
        "scheduled",
        "running",
        "analyzing",
    }
)
_TERMINAL = frozenset(
    {"completed", "partially_completed", "failed", "canceled", "deleted"}
)
_ALLOWED = {
    "creating": frozenset({"created", "failed", "canceled"}),
    "created": frozenset({"uploading", "queued", "failed", "canceled"}),
    "uploading": frozenset({"queued", "failed", "canceled"}),
    "queued": frozenset(
        {"scheduled", "running", "analyzing", "failed", "canceled"}
    ),
    "scheduled": frozenset({"running", "analyzing", "failed", "canceled"}),
    "running": frozenset(
        {"analyzing", "completed", "partially_completed", "failed", "canceled"}
    ),
    "analyzing": frozenset(
        {"completed", "partially_completed", "failed", "canceled"}
    ),
}


class AnalysisLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    analysis_id: UUID
    state: AnalysisState
    generation: int
    cancel_requested_at: datetime | None
    report_available: bool
    completed_at: datetime | None = None


def apply_transition(
    current: LifecycleSnapshot,
    *,
    target: AnalysisState,
    now: datetime,
    result_generation: int | None = None,
    publish_report: bool = False,
) -> LifecycleSnapshot:
    if result_generation is not None and result_generation != current.generation:
        raise AnalysisLifecycleError("analysis generation rejected")
    if current.state in _TERMINAL:
        if target == current.state and (
            not publish_report or current.report_available
        ):
            return current
        raise AnalysisLifecycleError("analysis transition rejected")
    if current.cancel_requested_at is not None and target != "canceled":
        raise AnalysisLifecycleError("analysis transition rejected")
    if target != current.state and target not in _ALLOWED[current.state]:
        raise AnalysisLifecycleError("analysis transition rejected")
    if publish_report and target not in {"completed", "partially_completed"}:
        raise AnalysisLifecycleError("analysis transition rejected")
    report_available = current.report_available or publish_report
    if report_available and target in _ACTIVE:
        raise AnalysisLifecycleError("analysis transition rejected")
    return replace(
        current,
        state=target,
        report_available=report_available,
        completed_at=now if target in _TERMINAL else current.completed_at,
    )


def request_cancel(
    current: LifecycleSnapshot,
    *,
    now: datetime,
) -> LifecycleSnapshot:
    if current.state in _TERMINAL or current.cancel_requested_at is not None:
        return current
    return replace(current, cancel_requested_at=now)


class AnalysisLifecycleCoordinator:
    def transition(
        self,
        current: LifecycleSnapshot,
        *,
        target: AnalysisState,
        now: datetime,
        result_generation: int | None = None,
        publish_report: bool = False,
    ) -> LifecycleSnapshot:
        return apply_transition(
            current,
            target=target,
            now=now,
            result_generation=result_generation,
            publish_report=publish_report,
        )

    def cancel(
        self,
        current: LifecycleSnapshot,
        *,
        now: datetime,
    ) -> LifecycleSnapshot:
        return request_cancel(current, now=now)


__all__ = [
    "AnalysisLifecycleCoordinator",
    "AnalysisLifecycleError",
    "AnalysisState",
    "LifecycleSnapshot",
    "apply_transition",
    "request_cancel",
]
