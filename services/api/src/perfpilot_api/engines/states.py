"""Transitions for the lifecycle of a single external engine execution."""

from __future__ import annotations

from enum import StrEnum


class EngineExecutionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    COMPLETED = "completed"
    INSUFFICIENT_DATA = "insufficient_data"
    FAILED = "failed"
    CANCELED = "canceled"


class InvalidEngineTransition(RuntimeError):
    """The requested state change is outside the engine execution lifecycle."""


_ALLOWED_TRANSITIONS: dict[EngineExecutionState, frozenset[EngineExecutionState]] = {
    EngineExecutionState.PENDING: frozenset(
        {
            EngineExecutionState.RUNNING,
            EngineExecutionState.FAILED,
            EngineExecutionState.CANCELED,
        }
    ),
    EngineExecutionState.RUNNING: frozenset(
        {
            EngineExecutionState.AWAITING_USER,
            EngineExecutionState.COMPLETED,
            EngineExecutionState.INSUFFICIENT_DATA,
            EngineExecutionState.FAILED,
            EngineExecutionState.CANCELED,
        }
    ),
    EngineExecutionState.AWAITING_USER: frozenset(
        {
            EngineExecutionState.RUNNING,
            EngineExecutionState.FAILED,
            EngineExecutionState.CANCELED,
        }
    ),
    EngineExecutionState.COMPLETED: frozenset(),
    EngineExecutionState.INSUFFICIENT_DATA: frozenset(),
    EngineExecutionState.FAILED: frozenset(),
    EngineExecutionState.CANCELED: frozenset(),
}


def transition_engine_state(current: str, target: str) -> EngineExecutionState:
    """Validate and apply one execution-state transition."""

    try:
        source = EngineExecutionState(current)
        destination = EngineExecutionState(target)
    except (TypeError, ValueError):
        raise InvalidEngineTransition("unknown engine state") from None

    if destination not in _ALLOWED_TRANSITIONS[source]:
        raise InvalidEngineTransition(
            f"invalid engine transition: {source.value} -> {destination.value}"
        )
    return destination
