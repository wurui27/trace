from __future__ import annotations

import pytest


def test_state_enums_match_the_persisted_analysis_and_scenario_states() -> None:
    from perfpilot_api.domain.states import AnalysisState, ScenarioState

    assert tuple(state.value for state in AnalysisState) == (
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
    )
    assert tuple(state.value for state in ScenarioState) == (
        "queued",
        "scheduled",
        "running",
        "analyzing",
        "completed",
        "failed",
        "canceled",
    )


@pytest.mark.parametrize(
    ("children", "expected"),
    [
        (["queued", "queued", "queued"], "queued"),
        (["scheduled", "queued", "queued"], "scheduled"),
        (["completed", "scheduled", "queued"], "running"),
        (["completed", "analyzing", "completed"], "analyzing"),
        (["completed", "failed", "completed"], "partially_completed"),
        (["completed", "completed", "completed"], "completed"),
        (["failed", "failed", "failed"], "failed"),
        (["canceled", "canceled", "canceled"], "canceled"),
        (["failed", "canceled", "failed"], "failed"),
    ],
)
def test_parent_state_is_derived(children: list[str], expected: str) -> None:
    from perfpilot_api.domain.states import AnalysisState
    from perfpilot_api.domain.transitions import derive_parent_state

    assert derive_parent_state(children) is AnalysisState(expected)


@pytest.mark.parametrize(
    "children",
    [
        [],
        ["queued"],
        ["queued", "queued"],
        ["queued", "queued", "queued", "queued"],
        ["queued", "unknown"],
        ["created"],
        "queued",
    ],
)
def test_parent_derivation_fails_closed_for_missing_or_unknown_children(
    children: object,
) -> None:
    from perfpilot_api.domain.transitions import InvalidAggregateState, derive_parent_state

    with pytest.raises(InvalidAggregateState):
        derive_parent_state(children)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("creating", "created"),
        ("created", "uploading"),
        ("created", "queued"),
        ("uploading", "queued"),
        ("queued", "scheduled"),
        ("scheduled", "running"),
        ("scheduled", "queued"),
        ("uploading", "analyzing"),
        ("running", "analyzing"),
        ("running", "queued"),
        ("analyzing", "running"),
        ("analyzing", "completed"),
        ("analyzing", "partially_completed"),
        ("queued", "canceled"),
        ("running", "failed"),
    ],
)
def test_transition_accepts_legal_compare_and_swap_edges(
    current: str,
    target: str,
) -> None:
    from perfpilot_api.domain.states import AnalysisState
    from perfpilot_api.domain.transitions import transition

    assert transition(current, target) is AnalysisState(target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("creating", "running"),
        ("created", "scheduled"),
        ("uploading", "running"),
        ("queued", "analyzing"),
        ("scheduled", "completed"),
        ("running", "completed"),
        ("queued", "queued"),
    ],
)
def test_transition_rejects_edges_outside_the_compare_and_swap_rules(
    current: str,
    target: str,
) -> None:
    from perfpilot_api.domain.transitions import InvalidTransition, transition

    with pytest.raises(InvalidTransition):
        transition(current, target)


@pytest.mark.parametrize(
    "terminal", ["completed", "partially_completed", "failed", "canceled", "deleted"]
)
@pytest.mark.parametrize("target", ["queued", "scheduled", "running", "analyzing"])
def test_terminal_state_cannot_move_back(terminal: str, target: str) -> None:
    from perfpilot_api.domain.transitions import InvalidTransition, transition

    with pytest.raises(InvalidTransition):
        transition(terminal, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [("unknown", "queued"), ("queued", "unknown")],
)
def test_transition_fails_closed_for_unknown_states(current: str, target: str) -> None:
    from perfpilot_api.domain.transitions import InvalidTransition, transition

    with pytest.raises(InvalidTransition):
        transition(current, target)


def test_synthesis_projection_reduces_core_and_ai_outcomes() -> None:
    from perfpilot_api.domain.states import AnalysisState
    from perfpilot_api.domain.transitions import synthesis_parent_state

    assert (
        synthesis_parent_state(core_state="complete", synthesis_state="completed")
        is AnalysisState.COMPLETED
    )
    assert (
        synthesis_parent_state(core_state="complete", synthesis_state="failed")
        is AnalysisState.PARTIALLY_COMPLETED
    )
    assert (
        synthesis_parent_state(
            core_state="complete",
            synthesis_state="completed",
            credible_core=False,
        )
        is AnalysisState.FAILED
    )


def test_failed_synthesis_report_can_be_remediated_only_by_complete_replacement() -> None:
    from perfpilot_api.domain.states import AnalysisState
    from perfpilot_api.domain.transitions import (
        InvalidSynthesisProjection,
        remediate_failed_synthesis,
    )

    previous = {
        "schema_version": "1.1",
        "state": "partially_completed",
        "synthesis": {"state": "failed"},
        "scenario_reports": [{"result_state": "completed"}],
    }
    replacement = {
        "schema_version": "1.1",
        "state": "completed",
        "synthesis": {"state": "completed"},
        "scenario_reports": [{"result_state": "completed"}],
    }

    assert (
        remediate_failed_synthesis(
            AnalysisState.PARTIALLY_COMPLETED,
            previous_report=previous,
            replacement_report=replacement,
        )
        is AnalysisState.COMPLETED
    )
    with pytest.raises(InvalidSynthesisProjection):
        remediate_failed_synthesis(
            AnalysisState.COMPLETED,
            previous_report=previous,
            replacement_report=replacement,
        )
