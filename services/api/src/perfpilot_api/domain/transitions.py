from __future__ import annotations

from collections.abc import Iterable
from typing import Mapping

from perfpilot_api.domain.states import (
    SCENARIO_TERMINAL_STATES,
    AnalysisState,
    ScenarioState,
)


class InvalidTransition(ValueError):
    """The requested state edge is not part of the analysis state machine."""


class InvalidAggregateState(ValueError):
    """Child states cannot be safely reduced to an analysis state."""


class InvalidSynthesisProjection(ValueError):
    """A report cannot authorize the requested synthesis parent projection."""


_ALLOWED_ANALYSIS_TRANSITIONS: dict[AnalysisState, frozenset[AnalysisState]] = {
    AnalysisState.CREATING: frozenset(
        {AnalysisState.CREATED, AnalysisState.FAILED, AnalysisState.CANCELED}
    ),
    AnalysisState.CREATED: frozenset(
        {
            AnalysisState.UPLOADING,
            AnalysisState.QUEUED,
            AnalysisState.FAILED,
            AnalysisState.CANCELED,
        }
    ),
    AnalysisState.UPLOADING: frozenset(
        {
            AnalysisState.QUEUED,
            AnalysisState.ANALYZING,
            AnalysisState.FAILED,
            AnalysisState.CANCELED,
        }
    ),
    AnalysisState.QUEUED: frozenset(
        {AnalysisState.SCHEDULED, AnalysisState.FAILED, AnalysisState.CANCELED}
    ),
    AnalysisState.SCHEDULED: frozenset(
        {
            AnalysisState.QUEUED,
            AnalysisState.RUNNING,
            AnalysisState.FAILED,
            AnalysisState.CANCELED,
        }
    ),
    AnalysisState.RUNNING: frozenset(
        {
            AnalysisState.QUEUED,
            AnalysisState.ANALYZING,
            AnalysisState.FAILED,
            AnalysisState.CANCELED,
        }
    ),
    AnalysisState.ANALYZING: frozenset(
        {
            AnalysisState.RUNNING,
            AnalysisState.COMPLETED,
            AnalysisState.PARTIALLY_COMPLETED,
            AnalysisState.FAILED,
            AnalysisState.CANCELED,
        }
    ),
}


def transition(
    current: AnalysisState | str,
    target: AnalysisState | str,
) -> AnalysisState:
    try:
        current_state = AnalysisState(current)
        target_state = AnalysisState(target)
    except (TypeError, ValueError):
        raise InvalidTransition("unknown analysis state") from None

    if target_state not in _ALLOWED_ANALYSIS_TRANSITIONS.get(current_state, frozenset()):
        raise InvalidTransition("analysis state transition is not allowed")
    return target_state


def derive_parent_state(
    children: Iterable[ScenarioState | str],
) -> AnalysisState:
    if isinstance(children, (str, bytes)):
        raise InvalidAggregateState("scenario children must be a collection")

    try:
        child_values = tuple(children)
    except TypeError:
        raise InvalidAggregateState("scenario children must be a collection") from None
    if len(child_values) != 3:
        raise InvalidAggregateState("device analysis requires exactly three children")

    try:
        child_states = tuple(ScenarioState(child) for child in child_values)
    except (TypeError, ValueError):
        raise InvalidAggregateState("unknown scenario child state") from None

    child_set = frozenset(child_states)
    if child_set <= SCENARIO_TERMINAL_STATES:
        if child_set == {ScenarioState.COMPLETED}:
            return AnalysisState.COMPLETED
        if child_set == {ScenarioState.CANCELED}:
            return AnalysisState.CANCELED
        if ScenarioState.COMPLETED in child_set:
            return AnalysisState.PARTIALLY_COMPLETED
        return AnalysisState.FAILED

    if ScenarioState.ANALYZING in child_set:
        return AnalysisState.ANALYZING
    if ScenarioState.RUNNING in child_set or child_set & SCENARIO_TERMINAL_STATES:
        return AnalysisState.RUNNING
    if ScenarioState.SCHEDULED in child_set:
        return AnalysisState.SCHEDULED
    return AnalysisState.QUEUED


def synthesis_parent_state(
    *,
    core_state: str,
    synthesis_state: str,
    credible_core: bool = True,
    canceled: bool = False,
) -> AnalysisState:
    if canceled:
        return AnalysisState.CANCELED
    if not credible_core:
        return AnalysisState.FAILED
    if core_state not in {"complete", "partial"} or synthesis_state not in {
        "completed",
        "failed",
    }:
        raise InvalidSynthesisProjection("unknown synthesis report state")
    if core_state == "complete" and synthesis_state == "completed":
        return AnalysisState.COMPLETED
    return AnalysisState.PARTIALLY_COMPLETED


def remediate_failed_synthesis(
    current: AnalysisState | str,
    *,
    previous_report: Mapping[str, object],
    replacement_report: Mapping[str, object],
) -> AnalysisState:
    try:
        current_state = AnalysisState(current)
    except (TypeError, ValueError):
        raise InvalidSynthesisProjection("unknown analysis state") from None
    if current_state != AnalysisState.PARTIALLY_COMPLETED:
        raise InvalidSynthesisProjection("synthesis remediation is not allowed")
    previous_synthesis = previous_report.get("synthesis")
    replacement_synthesis = replacement_report.get("synthesis")
    previous_scenarios = previous_report.get("scenario_reports")
    replacement_scenarios = replacement_report.get("scenario_reports")
    if (
        previous_report.get("schema_version") != "1.1"
        or previous_report.get("state") != "partially_completed"
        or not isinstance(previous_synthesis, Mapping)
        or previous_synthesis.get("state") != "failed"
        or not isinstance(previous_scenarios, list)
        or not previous_scenarios
        or any(
            not isinstance(item, Mapping) or item.get("result_state") != "completed"
            for item in previous_scenarios
        )
        or replacement_report.get("schema_version") != "1.1"
        or replacement_report.get("state") != "completed"
        or not isinstance(replacement_synthesis, Mapping)
        or replacement_synthesis.get("state") != "completed"
        or not isinstance(replacement_scenarios, list)
        or not replacement_scenarios
        or any(
            not isinstance(item, Mapping) or item.get("result_state") != "completed"
            for item in replacement_scenarios
        )
    ):
        raise InvalidSynthesisProjection("synthesis remediation is not allowed")
    return AnalysisState.COMPLETED
