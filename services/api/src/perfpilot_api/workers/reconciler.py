from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.config import get_settings
from perfpilot_api.db.control.models import AgentLease, Device, GlobalJob, ScenarioJob
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.db.tenant.models import Analysis, ScenarioResult
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.runtime.artifacts import build_artifact_runtime
from perfpilot_api.services.agent_tasks import (
    AgentExecutionAccess,
    AgentTaskUnavailable,
    TaskScenarioType,
)
from perfpilot_api.services.agent_uploads import (
    AgentUploadService,
    SQLAlchemyAgentUploadRepository,
)

ReconciliationOutcome = Literal["requeued", "failed", "canceled"]


@dataclass(frozen=True, slots=True)
class LeaseReconciliation:
    access: AgentExecutionAccess
    outcome: ReconciliationOutcome
    reconciled_at: datetime


class ReconciliationRepository(Protocol):
    async def expire_one(self, *, now: datetime) -> LeaseReconciliation | None: ...

    async def project_reconciliation(
        self,
        *,
        reconciliation: LeaseReconciliation,
        now: datetime,
    ) -> None: ...


class ReconciliationArtifactCoordinator(Protocol):
    async def abort_execution(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> None: ...


class SQLAlchemyLeaseReconciliationRepository:
    def __init__(
        self,
        *,
        control_session_factory: Callable[[], AsyncSession],
        tenant_router: TenantRouter,
    ) -> None:
        self._control_sessions = control_session_factory
        self._tenant_router = tenant_router

    async def expire_one(self, *, now: datetime) -> LeaseReconciliation | None:
        _require_aware(now)
        async with self._control_sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentLease, GlobalJob)
                        .join(GlobalJob, GlobalJob.id == AgentLease.global_job_id)
                        .where(
                            or_(
                                (
                                    AgentLease.state.in_(("active", "cancel_requested"))
                                    & (AgentLease.expires_at <= now)
                                ),
                                (
                                    (AgentLease.state == "expired")
                                    & AgentLease.released_at.is_(None)
                                ),
                            ),
                            GlobalJob.state.in_(
                                ("queued", "scheduled", "running", "analyzing")
                            ),
                        )
                        .order_by(AgentLease.expires_at, AgentLease.id)
                        .limit(1)
                        .with_for_update(of=AgentLease, skip_locked=True)
                    )
                ).one_or_none()
                if row is None:
                    return None
                lease, job = row
                scenario_rows = tuple(
                    (
                        await session.scalars(
                            select(ScenarioJob)
                            .where(ScenarioJob.analysis_id == job.id)
                            .with_for_update()
                        )
                    ).all()
                )
                outcome = _outcome(job)
                if lease.state != "expired":
                    lease.state = "expired"
                    lease.updated_at = now
                await session.flush()
                return LeaseReconciliation(
                    access=AgentExecutionAccess(
                        team_id=job.team_id,
                        analysis_id=job.id,
                        agent_id=lease.agent_id,
                        execution_id=lease.execution_id,
                        lease_version=lease.version,
                        lease_expires_at=lease.expires_at,
                        scenario_types=_scenario_types(scenario_rows),
                    ),
                    outcome=outcome,
                    reconciled_at=now,
                )

    async def project_reconciliation(
        self,
        *,
        reconciliation: LeaseReconciliation,
        now: datetime,
    ) -> None:
        _require_aware(now)
        await self._project_tenant(reconciliation=reconciliation, now=now)
        access = reconciliation.access
        async with self._control_sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentLease, GlobalJob, Device)
                        .join(GlobalJob, GlobalJob.id == AgentLease.global_job_id)
                        .join(Device, Device.id == AgentLease.device_id)
                        .where(AgentLease.execution_id == access.execution_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    raise AgentTaskUnavailable("Lease reconciliation is unavailable")
                lease, job, device = row
                if (
                    lease.agent_id != access.agent_id
                    or lease.version != access.lease_version
                    or job.id != access.analysis_id
                    or job.team_id != access.team_id
                    or lease.state != "expired"
                ):
                    raise AgentTaskUnavailable("Lease reconciliation is unavailable")
                if lease.released_at is not None:
                    return
                if _outcome(job) != reconciliation.outcome:
                    raise AgentTaskUnavailable("Lease reconciliation changed")

                scenario_rows = tuple(
                    (
                        await session.scalars(
                            select(ScenarioJob)
                            .where(ScenarioJob.analysis_id == access.analysis_id)
                            .with_for_update()
                        )
                    ).all()
                )
                if reconciliation.outcome == "canceled":
                    job.state = "canceled"
                    job.completed_at = now
                    job.failure_code = None
                    for scenario in scenario_rows:
                        if scenario.state in ("completed", "failed", "canceled"):
                            continue
                        scenario.state = "canceled"
                        scenario.completed_at = now
                        scenario.failure_code = None
                        scenario.version += 1
                        scenario.updated_at = now
                elif reconciliation.outcome == "requeued":
                    job.state = "queued"
                    job.completed_at = None
                    job.failure_code = None
                    job.retry_count += 1
                    for scenario in scenario_rows:
                        if scenario.state not in ("scheduled", "running"):
                            continue
                        scenario.state = "queued"
                        scenario.completed_at = None
                        scenario.failure_code = None
                        scenario.retry_count += 1
                        scenario.version += 1
                        scenario.updated_at = now
                else:
                    job.state = "failed"
                    job.completed_at = now
                    job.failure_code = "agent_lease_expired"
                    for scenario in scenario_rows:
                        if scenario.state in ("completed", "failed", "canceled"):
                            continue
                        scenario.state = "failed"
                        scenario.completed_at = now
                        scenario.failure_code = "agent_lease_expired"
                        scenario.version += 1
                        scenario.updated_at = now
                job.version += 1
                job.updated_at = now
                lease.released_at = now
                lease.updated_at = now
                newer = await session.scalar(
                    select(AgentLease.id)
                    .where(
                        AgentLease.device_id == device.id,
                        AgentLease.id != lease.id,
                        AgentLease.state.in_(("active", "cancel_requested")),
                    )
                    .limit(1)
                )
                if newer is None:
                    device.state = "ready"
                    device.version += 1
                    device.updated_at = now
                await session.flush()

    async def _project_tenant(
        self,
        *,
        reconciliation: LeaseReconciliation,
        now: datetime,
    ) -> None:
        access = reconciliation.access
        async with self._tenant_router.session(access.team_id) as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(
                    Analysis.id == access.analysis_id,
                    Analysis.analysis_mode == "device",
                    Analysis.tombstoned_at.is_(None),
                )
                .with_for_update()
            )
            if analysis is None:
                raise AgentTaskUnavailable("Lease reconciliation is unavailable")
            scenarios = tuple(
                (
                    await session.scalars(
                        select(ScenarioResult)
                        .where(ScenarioResult.analysis_id == access.analysis_id)
                        .with_for_update()
                    )
                ).all()
            )
            if reconciliation.outcome == "canceled":
                if analysis.state not in (
                    "completed",
                    "partially_completed",
                    "failed",
                    "canceled",
                ):
                    analysis.state = "canceled"
                    analysis.completed_at = now
                    analysis.failure_code = None
                    analysis.version += 1
                    analysis.updated_at = now
                for scenario in scenarios:
                    if scenario.state in ("completed", "failed", "canceled"):
                        continue
                    scenario.state = "canceled"
                    scenario.completed_at = now
                    scenario.failure_code = None
                    scenario.version += 1
                    scenario.updated_at = now
            elif reconciliation.outcome == "requeued":
                if analysis.state in ("scheduled", "running"):
                    analysis.state = "queued"
                    analysis.completed_at = None
                    analysis.failure_code = None
                    analysis.version += 1
                    analysis.updated_at = now
                for scenario in scenarios:
                    if scenario.state not in ("scheduled", "running"):
                        continue
                    scenario.state = "queued"
                    scenario.completed_at = None
                    scenario.failure_code = None
                    scenario.version += 1
                    scenario.updated_at = now
            else:
                if analysis.state not in (
                    "completed",
                    "partially_completed",
                    "failed",
                    "canceled",
                ):
                    analysis.state = "failed"
                    analysis.completed_at = now
                    analysis.failure_code = "agent_lease_expired"
                    analysis.version += 1
                    analysis.updated_at = now
                for scenario in scenarios:
                    if scenario.state in ("completed", "failed", "canceled"):
                        continue
                    scenario.state = "failed"
                    scenario.completed_at = now
                    scenario.failure_code = "agent_lease_expired"
                    scenario.version += 1
                    scenario.updated_at = now
            await session.flush()


class Reconciler:
    def __init__(
        self,
        *,
        repository: ReconciliationRepository,
        artifact_coordinator: ReconciliationArtifactCoordinator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._artifact_coordinator = artifact_coordinator
        self._clock = clock

    async def run_once(self) -> LeaseReconciliation | None:
        now = _aware(self._clock())
        reconciliation = await self._repository.expire_one(now=now)
        if reconciliation is None:
            return None
        await self._artifact_coordinator.abort_execution(
            access=reconciliation.access,
            now=now,
        )
        await self._repository.project_reconciliation(
            reconciliation=reconciliation,
            now=now,
        )
        return reconciliation


class ReconcilerWorker:
    def __init__(
        self,
        *,
        reconciler: Reconciler,
        idle_poll_seconds: float = 5.0,
        failure_backoff_seconds: float = 5.0,
    ) -> None:
        if idle_poll_seconds <= 0 or failure_backoff_seconds <= 0:
            raise ValueError("Reconciler intervals must be positive")
        self._reconciler = reconciler
        self._idle_poll_seconds = idle_poll_seconds
        self._failure_backoff_seconds = failure_backoff_seconds

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            try:
                reconciliation = await self._reconciler.run_once()
                delay = 0 if reconciliation is not None else self._idle_poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = self._failure_backoff_seconds
            if delay == 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
            except TimeoutError:
                pass


class _UnusedExecutionAuthorizer:
    async def authorize_execution(self, **_: object) -> AgentExecutionAccess:
        raise AgentTaskUnavailable("Reconciler does not authorize Agent requests")


def _outcome(job: GlobalJob) -> ReconciliationOutcome:
    if job.cancel_requested_at is not None:
        return "canceled"
    return "requeued" if job.retry_count < job.max_retries else "failed"


def _scenario_types(rows: tuple[ScenarioJob, ...]) -> tuple[TaskScenarioType, ...]:
    observed: set[TaskScenarioType] = set()
    for row in rows:
        if row.scenario_type == "cold_start":
            observed.add("startup")
        elif row.scenario_type in ("scroll", "memory_cycle"):
            observed.add(cast(TaskScenarioType, row.scenario_type))
        else:
            raise AgentTaskUnavailable("Lease reconciliation is unavailable")
    return tuple(
        item for item in ("startup", "scroll", "memory_cycle") if item in observed
    )  # type: ignore[return-value]


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Reconciler clock must return an aware datetime")


def _aware(value: datetime) -> datetime:
    _require_aware(value)
    return value.astimezone(UTC)


async def _run() -> None:
    settings = get_settings()
    engine = create_control_engine(settings.control_database_url.get_secret_value())
    artifacts = None
    try:
        control_sessions = create_control_session_factory(engine)
        artifacts = await build_artifact_runtime(
            settings=settings,
            control_session_factory=control_sessions,
            include_local_apk_inspector=False,
        )
        if artifacts.artifact_store is None or artifacts.bucket_resolver is None:
            raise RuntimeError("Reconciler artifact dependencies are unavailable")
        artifact_coordinator = AgentUploadService(
            repository=SQLAlchemyAgentUploadRepository(
                tenant_router=artifacts.tenant_router,
            ),
            artifact_store=artifacts.artifact_store,
            bucket_resolver=artifacts.bucket_resolver,
            execution_authorizer=_UnusedExecutionAuthorizer(),
        )
        reconciler = Reconciler(
            repository=SQLAlchemyLeaseReconciliationRepository(
                control_session_factory=control_sessions,
                tenant_router=artifacts.tenant_router,
            ),
            artifact_coordinator=artifact_coordinator,
        )
        await ReconcilerWorker(reconciler=reconciler).run_forever()
    finally:
        if artifacts is not None:
            await artifacts.close()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reconcile expired Agent leases")
    parser.parse_args(argv)
    asyncio.run(_run())


__all__ = [
    "LeaseReconciliation",
    "Reconciler",
    "ReconcilerWorker",
    "SQLAlchemyLeaseReconciliationRepository",
    "main",
]
