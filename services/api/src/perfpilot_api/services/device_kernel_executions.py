"""Advance SmartPerfetto and Android Memory for one device analysis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, select

from perfpilot_api.db.tenant.models import Analysis, ScenarioResult
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.engines.contracts import EngineStepOutcome
from perfpilot_api.services.memory_analyses import CreatedMemoryCapture


_TRACE_SUCCESS_STATES = frozenset({"completed", "insufficient_data"})
_MEMORY_READY_STATES = frozenset({"analyzing", "completed"})
_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


class DeviceKernelExecutionError(RuntimeError):
    """Stable device-kernel orchestration failure without tenant-private data."""


class DeviceKernelExecutionNotFoundError(DeviceKernelExecutionError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("analysis kernel context was not found")


class DeviceKernelExecutionUnavailableError(DeviceKernelExecutionError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("analysis kernel execution is unavailable")


@dataclass(frozen=True, slots=True)
class DeviceKernelContext:
    analysis_id: UUID
    analysis_mode: str
    memory_scenario_state: str | None


class DeviceKernelContextRepository(Protocol):
    async def load_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> DeviceKernelContext: ...


class TraceKernelService(Protocol):
    async def advance(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome: ...


class DeviceMemoryCaptureService(Protocol):
    async def create_device_capture(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> CreatedMemoryCapture: ...


class DeviceMemoryExecutionService(Protocol):
    async def advance(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        capture_id: UUID,
    ) -> EngineStepOutcome: ...


class SQLAlchemyDeviceKernelContextRepository:
    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    async def load_context(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> DeviceKernelContext:
        try:
            async with self._tenant_router.session(team_id) as session:
                row = (
                    await session.execute(
                        select(Analysis.analysis_mode, ScenarioResult.state)
                        .outerjoin(
                            ScenarioResult,
                            and_(
                                ScenarioResult.analysis_id == Analysis.id,
                                ScenarioResult.scenario_type == "memory_cycle",
                            ),
                        )
                        .where(
                            Analysis.id == analysis_id,
                            Analysis.analysis_mode.in_(("trace_upload", "device")),
                            Analysis.tombstoned_at.is_(None),
                            Analysis.state != "deleted",
                        )
                    )
                ).one_or_none()
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except DeviceKernelExecutionError:
            raise
        except Exception:
            raise DeviceKernelExecutionUnavailableError from None
        if row is None:
            raise DeviceKernelExecutionNotFoundError
        analysis_mode, memory_scenario_state = row
        if analysis_mode == "trace_upload":
            memory_scenario_state = None
        return DeviceKernelContext(
            analysis_id=analysis_id,
            analysis_mode=analysis_mode,
            memory_scenario_state=memory_scenario_state,
        )


class DeviceKernelExecutionService:
    """Keep external kernels separate while advancing them in report order."""

    def __init__(
        self,
        *,
        repository: DeviceKernelContextRepository,
        trace_service: TraceKernelService,
        capture_service: DeviceMemoryCaptureService,
        memory_service: DeviceMemoryExecutionService,
    ) -> None:
        self._repository = repository
        self._trace_service = trace_service
        self._capture_service = capture_service
        self._memory_service = memory_service

    async def advance(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> EngineStepOutcome:
        trace_outcome = await self._trace_service.advance(
            team_id=team_id,
            analysis_id=analysis_id,
        )
        if trace_outcome.retry is not None or trace_outcome.state not in _TRACE_SUCCESS_STATES:
            return trace_outcome

        context = await self._repository.load_context(
            team_id=team_id,
            analysis_id=analysis_id,
        )
        if (
            context.analysis_id != analysis_id
            or context.analysis_mode not in {"trace_upload", "device"}
        ):
            raise DeviceKernelExecutionUnavailableError
        if context.analysis_mode == "trace_upload":
            return trace_outcome
        if context.memory_scenario_state not in _MEMORY_READY_STATES:
            return trace_outcome

        capture = await self._capture_service.create_device_capture(
            team_id=team_id,
            analysis_id=analysis_id,
        )
        return await self._memory_service.advance(
            team_id=team_id,
            analysis_id=analysis_id,
            capture_id=capture.manifest.capture_id,
        )


__all__ = [
    "DeviceKernelContext",
    "DeviceKernelContextRepository",
    "DeviceKernelExecutionError",
    "DeviceKernelExecutionNotFoundError",
    "DeviceKernelExecutionService",
    "DeviceKernelExecutionUnavailableError",
    "SQLAlchemyDeviceKernelContextRepository",
]
