from __future__ import annotations

import pytest

from perfpilot_api.engines.states import (
    EngineExecutionState,
    InvalidEngineTransition,
    transition_engine_state,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("pending", "running"),
        ("pending", "failed"),
        ("pending", "canceled"),
        ("running", "awaiting_user"),
        ("running", "completed"),
        ("running", "insufficient_data"),
        ("running", "failed"),
        ("running", "canceled"),
        ("awaiting_user", "running"),
        ("awaiting_user", "failed"),
        ("awaiting_user", "canceled"),
    ],
)
def test_valid_engine_transitions(current: str, target: str) -> None:
    assert transition_engine_state(current, target) is EngineExecutionState(target)


@pytest.mark.parametrize("terminal", ["completed", "insufficient_data", "failed", "canceled"])
def test_terminal_engine_state_cannot_reopen(terminal: str) -> None:
    with pytest.raises(InvalidEngineTransition):
        transition_engine_state(terminal, "running")


@pytest.mark.parametrize(
    ("current", "target"),
    [("pending", "completed"), ("awaiting_user", "completed")],
)
def test_invalid_engine_edges_are_rejected(current: str, target: str) -> None:
    with pytest.raises(InvalidEngineTransition):
        transition_engine_state(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [("unknown", "running"), ("pending", "unknown")],
)
def test_unknown_engine_state_values_raise_stable_transition_error(
    current: str,
    target: str,
) -> None:
    with pytest.raises(InvalidEngineTransition, match="unknown engine state"):
        transition_engine_state(current, target)
