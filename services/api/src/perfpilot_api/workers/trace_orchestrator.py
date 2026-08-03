"""Recoverable orchestration entry point for one trace-analysis step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from perfpilot_api.engines.contracts import EngineStepOutcome


class TraceExecutionAdvancer(Protocol):
    async def advance(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome: ...


@dataclass(slots=True)
class TraceOrchestrator:
    """Advance a single durable SmartPerfetto execution."""

    service: TraceExecutionAdvancer

    async def run_once(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome:
        return await self.service.advance(team_id=team_id, analysis_id=analysis_id)


__all__ = ["TraceOrchestrator"]
