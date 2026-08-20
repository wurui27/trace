from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class RecoveryAction(str, Enum):
    CLOSE_CANCELED = "close_canceled"
    RECONCILE_PUBLICATION = "reconcile_publication"
    RESUME_REMOTE_ANALYSIS = "resume_remote_analysis"
    RESUME_SYNTHESIS = "resume_synthesis"
    CLOSE_COMPLETED = "close_completed"
    FAIL_INVALID_RECOVERY = "fail_invalid_recovery"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    state: str
    canceled: bool
    smartperfetto_state: str
    evidence_manifest_present: bool
    remote_manifest_present: bool
    report_present: bool
    source_state: str
    remote_publication: Literal["not_requested", "publishing", "published"]
    identity_valid: bool = True
    artifacts_valid: bool = True


_TERMINAL = frozenset(
    {"completed", "partially_completed", "failed", "canceled", "deleted"}
)
_ACTIVE = frozenset(
    {"creating", "created", "uploading", "queued", "scheduled", "running", "analyzing"}
)


def plan_recovery(snapshot: RecoverySnapshot) -> tuple[RecoveryAction, ...]:
    if snapshot.state in _TERMINAL:
        return (RecoveryAction.NOOP,)
    if snapshot.state not in _ACTIVE:
        return (RecoveryAction.FAIL_INVALID_RECOVERY,)
    if not snapshot.identity_valid or not snapshot.artifacts_valid:
        return (RecoveryAction.FAIL_INVALID_RECOVERY,)
    if snapshot.canceled:
        return (RecoveryAction.CLOSE_CANCELED,)
    if snapshot.report_present:
        return (RecoveryAction.CLOSE_COMPLETED,)
    if (
        snapshot.smartperfetto_state == "completed"
        and snapshot.evidence_manifest_present
        and snapshot.source_state in {"not_requested", "available", "unavailable"}
    ):
        return (RecoveryAction.RESUME_SYNTHESIS,)
    if (
        snapshot.remote_publication == "published"
        and snapshot.remote_manifest_present
        and snapshot.smartperfetto_state in {"pending", "running"}
    ):
        return (RecoveryAction.RESUME_REMOTE_ANALYSIS,)
    if snapshot.remote_publication in {"publishing", "published"}:
        return (RecoveryAction.RECONCILE_PUBLICATION,)
    return (RecoveryAction.NOOP,)


__all__ = ["RecoveryAction", "RecoverySnapshot", "plan_recovery"]
