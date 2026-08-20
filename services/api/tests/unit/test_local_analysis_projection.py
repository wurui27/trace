from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from perfpilot_api.local_analysis_projection import (
    LocalAnalysisProjectionError,
    LocalAnalysisView,
    from_persisted_document,
    to_persisted_document,
    to_public_document,
)


TEAM_ID = UUID("21000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("31000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)


def _runtime_status() -> dict[str, object]:
    return {
        "current_stage": "source_code",
        "stage_state": "running",
        "started_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "last_progress_at": NOW.isoformat(),
        "attempt": 1,
        "max_attempts": 2,
        "generation": 1,
        "waiting_for": None,
        "progress_summary": "正在读取并匹配源码",
        "available_actions": ["cancel"],
    }


def _view(*, source: dict[str, object] | None = None) -> LocalAnalysisView:
    return LocalAnalysisView(
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        schema_version="1.3",
        state="analyzing",
        version=3,
        generation=1,
        created_at=NOW,
        started_at=NOW,
        completed_at=None,
        cancel_requested_at=None,
        report_available=False,
        runtime_status=_runtime_status(),
        payload={
            "analysis_mode": "trace_upload",
            "profile": "startup",
            "question": None,
            "inputs": [],
            "failure": None,
            "stages": {
                "input_validation": "completed",
                "smartperfetto": "completed",
                "perfpilot_ai": "pending",
                "report": "pending",
            },
            "source_code_analysis": source
            or {
                "requested": False,
                "provider_kind": None,
                "agent_id": None,
                "workspace_id": None,
                "snapshot_policy": None,
                "validation_profile_id": None,
                "context_state": "not_requested",
                "match_summary": "none",
                "verification_state": "not_requested",
                "failure_code": None,
            },
            "public_document": {
                "schema_version": "1.3",
                "analysis_id": str(ANALYSIS_ID),
                "team_id": str(TEAM_ID),
                "analysis_mode": "trace_upload",
                "state": "analyzing",
                "version": 3,
                "created_at": NOW.isoformat(),
                "cancel_requested_at": None,
                "report_available": False,
                "failure": None,
                "source_code_analysis": source
                or {
                    "requested": False,
                    "provider_kind": None,
                    "agent_id": None,
                    "workspace_id": None,
                    "snapshot_policy": None,
                    "validation_profile_id": None,
                    "context_state": "not_requested",
                    "match_summary": "none",
                    "verification_state": "not_requested",
                    "failure_code": None,
                },
                "runtime_status": _runtime_status(),
            },
        },
    )


def test_persisted_projection_is_closed_and_round_trips_activity() -> None:
    persisted = to_persisted_document(_view())
    restored = from_persisted_document(persisted)

    assert persisted["schema_version"] == "1.0"
    assert persisted["analysis_id"] == str(ANALYSIS_ID)
    assert persisted["team_id"] == str(TEAM_ID)
    assert persisted["state"] == "analyzing"
    assert persisted["generation"] == 1
    assert restored.runtime_status == _runtime_status()
    assert restored.payload["analysis_mode"] == "trace_upload"


def test_legacy_persisted_document_migrates_missing_activity() -> None:
    persisted = to_persisted_document(_view())
    del persisted["runtime_status"]

    restored = from_persisted_document(persisted)

    assert restored.runtime_status["generation"] == 1
    assert restored.runtime_status["current_stage"] == "perfpilot_ai"


def test_public_projection_requires_runtime_status_for_13() -> None:
    view = _view()
    public = dict(view.payload["public_document"])
    del public["runtime_status"]
    broken = LocalAnalysisView(
        **{
            field: getattr(view, field)
            for field in (
                "analysis_id",
                "team_id",
                "schema_version",
                "state",
                "version",
                "generation",
                "created_at",
                "started_at",
                "completed_at",
                "cancel_requested_at",
                "report_available",
                "runtime_status",
            )
        },
        payload={**view.payload, "public_document": public},
    )

    with pytest.raises(LocalAnalysisProjectionError, match="public analysis rejected"):
        to_public_document(broken)


@pytest.mark.parametrize("private_key", ["relative_path", "symbol", "diff"])
def test_non_strong_source_never_projects_private_locations(private_key: str) -> None:
    source = {
        "requested": True,
        "provider_kind": "agent_workspace",
        "agent_id": "71000000-0000-4000-8000-000000000001",
        "workspace_id": "72000000-0000-4000-8000-000000000001",
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
        "context_state": "mismatch",
        "match_summary": "none",
        "verification_state": "mismatch",
        "failure_code": "source_package_mismatch",
        private_key: "private/value",
    }

    with pytest.raises(LocalAnalysisProjectionError, match="public analysis rejected"):
        to_public_document(_view(source=source))


def test_unknown_persisted_key_is_rejected() -> None:
    persisted = to_persisted_document(_view())
    persisted["private_path"] = "/Users/private/repository"

    with pytest.raises(LocalAnalysisProjectionError, match="persisted analysis rejected"):
        from_persisted_document(persisted)
