from __future__ import annotations

from collections.abc import Iterable

from perfpilot_api.domain.states import (
    SCENARIO_TERMINAL_STATES,
    AnalysisState,
    ScenarioState,
)


class InvalidTransition(ValueError):
    """The requested state edge is not part of the analysis state machine."""


class InvalidAggregateState(ValueError):
    """Child states cannot be safely reduced to an analysis state."""


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
