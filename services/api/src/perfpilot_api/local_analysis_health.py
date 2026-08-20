from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


CapabilityName = Literal[
    "smartperfetto",
    "ai",
    "agent",
    "device",
    "source",
    "storage",
    "supervisor",
]
HealthState = Literal["healthy", "degraded", "unavailable"]
AnalysisMode = Literal["trace_upload", "device"]

_CAPABILITY_NAMES = frozenset(
    {
        "smartperfetto",
        "ai",
        "agent",
        "device",
        "source",
        "storage",
        "supervisor",
    }
)
_HEALTH_STATES = frozenset({"healthy", "degraded", "unavailable"})


@dataclass(frozen=True, slots=True)
class CapabilityHealth:
    name: CapabilityName
    state: HealthState
    message: str
    last_checked_at: datetime

    def __post_init__(self) -> None:
        if (
            self.name not in _CAPABILITY_NAMES
            or self.state not in _HEALTH_STATES
            or not self.message
            or len(self.message) > 120
            or self.last_checked_at.tzinfo is None
        ):
            raise ValueError("capability health rejected")

    def document(self) -> dict[str, str]:
        return {
            "name": self.name,
            "state": self.state,
            "message": self.message,
            "last_checked_at": self.last_checked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AnalysisHealth:
    state: HealthState
    capabilities: tuple[CapabilityHealth, ...]

    def __post_init__(self) -> None:
        names = tuple(item.name for item in self.capabilities)
        if self.state not in _HEALTH_STATES or len(names) != len(set(names)):
            raise ValueError("analysis health rejected")

    def document(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "state": self.state,
            "capabilities": [item.document() for item in self.capabilities],
        }

    def for_mode(
        self,
        mode: AnalysisMode,
        *,
        source_requested: bool = False,
    ) -> AnalysisHealth:
        required = {"storage", "supervisor", "smartperfetto", "ai"}
        if mode == "device":
            required.update({"agent", "device"})
        elif mode != "trace_upload":
            raise ValueError("analysis mode rejected")
        if source_requested:
            required.update({"agent", "source"})
        selected = tuple(item for item in self.capabilities if item.name in required)
        states = {item.state for item in selected}
        state: HealthState = (
            "unavailable"
            if "unavailable" in states or len(selected) != len(required)
            else "degraded"
            if "degraded" in states
            else "healthy"
        )
        return AnalysisHealth(state=state, capabilities=self.capabilities)


class HealthAggregator:
    def readiness(
        self,
        capabilities: tuple[CapabilityHealth, ...],
    ) -> AnalysisHealth:
        indexed = {item.name: item for item in capabilities}
        required = {"storage", "supervisor"}
        required_states = {
            indexed[name].state if name in indexed else "unavailable"
            for name in required
        }
        all_states = {item.state for item in capabilities}
        state: HealthState = (
            "unavailable"
            if "unavailable" in required_states
            else "degraded"
            if all_states != {"healthy"}
            else "healthy"
        )
        return AnalysisHealth(state=state, capabilities=capabilities)


def supervisor_capability(
    *,
    last_tick_at: datetime | None,
    now: datetime,
    stale_after_seconds: float,
) -> CapabilityHealth:
    if (
        now.tzinfo is None
        or isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, (int, float))
        or stale_after_seconds <= 0
    ):
        raise ValueError("supervisor health rejected")
    stale = (
        last_tick_at is None
        or last_tick_at.tzinfo is None
        or (now - last_tick_at).total_seconds() > stale_after_seconds
    )
    return CapabilityHealth(
        name="supervisor",
        state="unavailable" if stale else "healthy",
        message="任务监督器无活动" if stale else "任务监督器正常",
        last_checked_at=now,
    )


__all__ = [
    "AnalysisHealth",
    "CapabilityHealth",
    "HealthAggregator",
    "supervisor_capability",
]
