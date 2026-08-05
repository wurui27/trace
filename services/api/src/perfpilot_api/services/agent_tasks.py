from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import Agent, AgentLease, Device, GlobalJob, ScenarioJob
from perfpilot_api.db.tenant.models import Analysis, ApplicationVersion, Artifact, ScenarioResult
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.security.task_snapshots import (
    TaskSnapshotSigner,
    snapshot_digest,
)

_LEASE_TTL = timedelta(seconds=60)
_FRESHNESS = timedelta(seconds=30)
_RENEW_AFTER_SECONDS = 20
_ALLOWED_UPLOADS = (
    "startup_trace",
    "scroll_trace",
    "memory_evidence",
    "agent_log",
)

TaskScenarioType = Literal["startup", "scroll", "memory_cycle"]
TaskInputKind = Literal["apk", "scenario_fixture", "dataset"]
CleanupPolicy = Literal["keep_installed", "uninstall"]


class AgentTaskError(RuntimeError):
    pass


class AgentTaskNotFound(AgentTaskError):
    pass


class StaleLeaseVersion(AgentTaskError):
    pass


class AgentTaskUnavailable(AgentTaskError):
    pass


@dataclass(frozen=True, slots=True)
class TaskInputArtifact:
    artifact_id: UUID
    kind: TaskInputKind
    mime: str
    size: int
    sha256_b64: str


@dataclass(frozen=True, slots=True)
class TaskScenario:
    scenario_type: TaskScenarioType
    recipe_version: int
    recipe_hash: str
    duration_seconds: int
    memory_rounds: int
    swipe_count: int


@dataclass(frozen=True, slots=True)
class AgentTaskDefinition:
    analysis_id: UUID
    team_id: UUID
    agent_id: UUID
    device_id: UUID
    device_digest: str
    package_name: str
    launch_activity: str
    cleanup_policy: CleanupPolicy
    input_artifacts: tuple[TaskInputArtifact, ...]
    scenarios: tuple[TaskScenario, ...]


@dataclass(frozen=True, slots=True)
class ScheduledAgentTask:
    lease_id: UUID
    execution_id: UUID
    analysis_id: UUID
    agent_id: UUID
    device_id: UUID
    lease_version: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActiveAgentTask:
    definition: AgentTaskDefinition
    lease_id: UUID
    execution_id: UUID
    lease_version: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AgentTaskDelivery:
    snapshot_jws: str
    lease_expires_at: datetime
    renew_after_seconds: int = _RENEW_AFTER_SECONDS


@dataclass(frozen=True, slots=True)
class LeaseRenewal:
    execution_id: UUID
    lease_version: int
    lease_expires_at: datetime
    renew_after_seconds: int = _RENEW_AFTER_SECONDS


@dataclass(frozen=True, slots=True)
class AgentExecutionAccess:
    team_id: UUID
    analysis_id: UUID
    agent_id: UUID
    execution_id: UUID
    lease_version: int
    lease_expires_at: datetime
    allowed_uploads: tuple[str, ...] = _ALLOWED_UPLOADS


class AgentTaskRepository(Protocol):
    async def schedule(
        self,
        *,
        analysis_id: UUID | None,
        now: datetime,
    ) -> ScheduledAgentTask | None: ...

    async def load_active(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> ActiveAgentTask | None: ...

    async def record_snapshot_digest(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        digest: str,
        now: datetime,
    ) -> None: ...

    async def renew(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> LeaseRenewal: ...

    async def authorize_execution(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess: ...


class AgentTaskWakeup(Protocol):
    async def wake(self, agent_id: UUID) -> None: ...

    async def wait(self, agent_id: UUID, seconds: int) -> None: ...


@dataclass(frozen=True, slots=True)
class _MemoryLease:
    definition: AgentTaskDefinition
    lease_id: UUID
    execution_id: UUID
    lease_version: int
    acquired_at: datetime
    expires_at: datetime
    task_snapshot_digest: str | None = None


class InMemoryAgentTaskRepository:
    __slots__ = (
        "_definitions",
        "_execution_id_source",
        "_lease_id_source",
        "_leases",
    )

    def __init__(
        self,
        definitions: Sequence[AgentTaskDefinition] = (),
        *,
        lease_id_source: Callable[[], UUID] = uuid4,
        execution_id_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._definitions = {item.analysis_id: item for item in definitions}
        self._lease_id_source = lease_id_source
        self._execution_id_source = execution_id_source
        self._leases: dict[UUID, _MemoryLease] = {}

    def __repr__(self) -> str:
        return f"InMemoryAgentTaskRepository(definitions={len(self._definitions)})"

    async def schedule(
        self,
        *,
        analysis_id: UUID | None,
        now: datetime,
    ) -> ScheduledAgentTask | None:
        _require_aware(now)
        candidates = (
            (self._definitions.get(analysis_id),)
            if analysis_id is not None
            else tuple(self._definitions.values())
        )
        for definition in candidates:
            if definition is None:
                continue
            existing = next(
                (
                    lease
                    for lease in self._leases.values()
                    if lease.definition.analysis_id == definition.analysis_id
                    and lease.expires_at > now
                ),
                None,
            )
            if existing is not None:
                return _scheduled(existing)
            if any(
                lease.expires_at > now
                and (
                    lease.definition.device_id == definition.device_id
                    or lease.definition.agent_id == definition.agent_id
                )
                for lease in self._leases.values()
            ):
                continue
            lease = _MemoryLease(
                definition=definition,
                lease_id=self._lease_id_source(),
                execution_id=self._execution_id_source(),
                lease_version=1,
                acquired_at=now,
                expires_at=now + _LEASE_TTL,
            )
            self._leases[lease.execution_id] = lease
            return _scheduled(lease)
        return None

    async def load_active(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> ActiveAgentTask | None:
        _require_aware(now)
        for lease in self._leases.values():
            if lease.definition.agent_id == agent_id and lease.expires_at > now:
                return ActiveAgentTask(
                    definition=lease.definition,
                    lease_id=lease.lease_id,
                    execution_id=lease.execution_id,
                    lease_version=lease.lease_version,
                    lease_expires_at=lease.expires_at,
                )
        return None

    async def record_snapshot_digest(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        digest: str,
        now: datetime,
    ) -> None:
        lease = self._leases.get(execution_id)
        if (
            lease is None
            or lease.definition.agent_id != agent_id
            or lease.lease_version != lease_version
            or lease.expires_at <= now
        ):
            raise AgentTaskNotFound
        self._leases[execution_id] = replace(lease, task_snapshot_digest=digest)

    async def renew(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> LeaseRenewal:
        _require_aware(now)
        lease = self._leases.get(execution_id)
        if lease is None or lease.definition.agent_id != agent_id or lease.expires_at <= now:
            raise AgentTaskNotFound
        if lease.lease_version != lease_version:
            raise StaleLeaseVersion
        expires_at = max(lease.expires_at, now + _LEASE_TTL)
        self._leases[execution_id] = replace(lease, expires_at=expires_at)
        return LeaseRenewal(
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=expires_at,
        )

    async def authorize_execution(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess:
        _require_aware(now)
        lease = self._leases.get(execution_id)
        if lease is None or lease.definition.agent_id != agent_id or lease.expires_at <= now:
            raise AgentTaskNotFound
        if lease.lease_version != lease_version:
            raise StaleLeaseVersion
        return AgentExecutionAccess(
            team_id=lease.definition.team_id,
            analysis_id=lease.definition.analysis_id,
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=lease.expires_at,
        )

    def snapshot_digest(self, execution_id: UUID) -> str | None:
        lease = self._leases.get(execution_id)
        return None if lease is None else lease.task_snapshot_digest


class InMemoryAgentTaskWakeup:
    def __init__(self) -> None:
        self._events: dict[UUID, asyncio.Event] = {}

    async def wake(self, agent_id: UUID) -> None:
        self._events.setdefault(agent_id, asyncio.Event()).set()

    async def wait(self, agent_id: UUID, seconds: int) -> None:
        if seconds <= 0:
            return
        event = self._events.setdefault(agent_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except TimeoutError:
            return
        finally:
            event.clear()


class RedisAgentTaskWakeup:
    __slots__ = ("_redis",)

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def __repr__(self) -> str:
        return "RedisAgentTaskWakeup()"

    @staticmethod
    def _key(agent_id: UUID) -> str:
        return f"perfpilot:agent:{agent_id}:tasks"

    async def wake(self, agent_id: UUID) -> None:
        key = self._key(agent_id)
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.rpush(key, str(agent_id))
            pipeline.expire(key, 30)
            await pipeline.execute()

    async def wait(self, agent_id: UUID, seconds: int) -> None:
        if seconds <= 0:
            return
        await self._redis.blpop((self._key(agent_id),), timeout=seconds)


class AgentTaskService:
    def __init__(
        self,
        *,
        repository: AgentTaskRepository,
        signer: TaskSnapshotSigner,
        wakeup: AgentTaskWakeup,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._signer = signer
        self._wakeup = wakeup
        self._clock = clock

    async def schedule(self, *, analysis_id: UUID | None = None) -> ScheduledAgentTask | None:
        now = _aware_now(self._clock())
        scheduled = await self._repository.schedule(analysis_id=analysis_id, now=now)
        if scheduled is not None:
            await self._wakeup.wake(scheduled.agent_id)
        return scheduled

    async def poll(self, *, agent_id: UUID, wait_seconds: int) -> AgentTaskDelivery | None:
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, int)
            or not 0 <= wait_seconds <= 20
        ):
            raise ValueError("Agent task poll wait is invalid")
        now = _aware_now(self._clock())
        task = await self._repository.load_active(agent_id=agent_id, now=now)
        if task is None and wait_seconds:
            await self._wakeup.wait(agent_id, wait_seconds)
            now = _aware_now(self._clock())
            task = await self._repository.load_active(agent_id=agent_id, now=now)
        if task is None:
            return None
        claims = _task_claims(task, issued_at=now)
        compact = self._signer.sign(claims)
        await self._repository.record_snapshot_digest(
            agent_id=agent_id,
            execution_id=task.execution_id,
            lease_version=task.lease_version,
            digest=snapshot_digest(compact),
            now=now,
        )
        return AgentTaskDelivery(
            snapshot_jws=compact,
            lease_expires_at=task.lease_expires_at,
        )

    async def renew(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
    ) -> LeaseRenewal:
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version < 1
        ):
            raise StaleLeaseVersion
        return await self._repository.renew(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=_aware_now(self._clock()),
        )

    async def authorize_execution(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess:
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version < 1
        ):
            raise StaleLeaseVersion
        return await self._repository.authorize_execution(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=_aware_now(now),
        )


class SQLAlchemyAgentTaskRepository:
    def __init__(
        self,
        *,
        control_session_factory: Callable[[], AsyncSession],
        tenant_router: TenantRouter,
        lease_id_source: Callable[[], UUID] = uuid4,
        execution_id_source: Callable[[], UUID] = uuid4,
        lease_token_source: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    ) -> None:
        self._control_sessions = control_session_factory
        self._tenant_router = tenant_router
        self._lease_id_source = lease_id_source
        self._execution_id_source = execution_id_source
        self._lease_token_source = lease_token_source

    async def schedule(
        self,
        *,
        analysis_id: UUID | None,
        now: datetime,
    ) -> ScheduledAgentTask | None:
        _require_aware(now)
        try:
            async with self._control_sessions() as session:
                async with session.begin():
                    statement = (
                        select(GlobalJob)
                        .join(Device, Device.id == GlobalJob.selected_device_id)
                        .join(Agent, Agent.id == Device.agent_id)
                        .where(
                            GlobalJob.analysis_mode == "device",
                            GlobalJob.state == "queued",
                            GlobalJob.selected_device_id.is_not(None),
                            Device.state == "ready",
                            Device.adb_state == "device",
                            Device.last_seen_at > now - _FRESHNESS,
                            Agent.state == "online",
                            Agent.last_heartbeat_at > now - _FRESHNESS,
                        )
                        .order_by(GlobalJob.created_at, GlobalJob.id)
                        .limit(1)
                        .with_for_update(of=GlobalJob, skip_locked=True)
                    )
                    if analysis_id is not None:
                        statement = statement.where(GlobalJob.id == analysis_id)
                    job = await session.scalar(statement)
                    if job is None or job.selected_device_id is None:
                        return None
                    device = await session.scalar(
                        select(Device).where(Device.id == job.selected_device_id).with_for_update()
                    )
                    if device is None:
                        return None
                    agent = await session.scalar(
                        select(Agent).where(Agent.id == device.agent_id).with_for_update()
                    )
                    if agent is None or not _agent_is_idle(agent):
                        return None
                    active_agent_lease = await session.scalar(
                        select(AgentLease.id)
                        .where(
                            AgentLease.agent_id == agent.id,
                            AgentLease.state == "active",
                            AgentLease.expires_at > now,
                        )
                        .limit(1)
                    )
                    if active_agent_lease is not None:
                        return None
                    lease = AgentLease(
                        id=self._lease_id_source(),
                        device_id=device.id,
                        agent_id=agent.id,
                        global_job_id=job.id,
                        execution_id=self._execution_id_source(),
                        lease_token_digest=hashlib.sha256(self._lease_token_source()).hexdigest(),
                        state="active",
                        acquired_at=now,
                        renewed_at=now,
                        expires_at=now + _LEASE_TTL,
                        task_snapshot_digest=None,
                        version=1,
                    )
                    session.add(lease)
                    job.state = "scheduled"
                    job.started_at = job.started_at or now
                    job.version += 1
                    job.updated_at = now
                    device.state = "busy"
                    device.version += 1
                    device.updated_at = now
                    await session.execute(
                        update(ScenarioJob)
                        .where(
                            ScenarioJob.analysis_id == job.id,
                            ScenarioJob.state == "queued",
                        )
                        .values(
                            state="scheduled",
                            version=ScenarioJob.version + 1,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                    return ScheduledAgentTask(
                        lease_id=lease.id,
                        execution_id=lease.execution_id,
                        analysis_id=job.id,
                        agent_id=agent.id,
                        device_id=device.id,
                        lease_version=lease.version,
                        lease_expires_at=lease.expires_at,
                    )
        except IntegrityError:
            return None

    async def load_active(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> ActiveAgentTask | None:
        _require_aware(now)
        async with self._control_sessions() as session:
            row = (
                await session.execute(
                    select(AgentLease, GlobalJob, Device)
                    .join(GlobalJob, GlobalJob.id == AgentLease.global_job_id)
                    .join(Device, Device.id == AgentLease.device_id)
                    .where(
                        AgentLease.agent_id == agent_id,
                        AgentLease.state == "active",
                        AgentLease.expires_at > now,
                        GlobalJob.state.in_(("scheduled", "running")),
                    )
                    .order_by(AgentLease.acquired_at, AgentLease.id)
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return None
        lease, job, device = row
        definition = await self._load_definition(job=job, device=device)
        return ActiveAgentTask(
            definition=definition,
            lease_id=lease.id,
            execution_id=lease.execution_id,
            lease_version=lease.version,
            lease_expires_at=lease.expires_at,
        )

    async def _load_definition(
        self,
        *,
        job: GlobalJob,
        device: Device,
    ) -> AgentTaskDefinition:
        async with self._tenant_router.session(job.team_id) as session:
            tenant_analysis = await session.get(Analysis, job.id)
            if tenant_analysis is None or tenant_analysis.application_version_id is None:
                raise AgentTaskUnavailable("Task application metadata is unavailable")
            application_version = await session.get(
                ApplicationVersion,
                tenant_analysis.application_version_id,
            )
            artifact = (
                None
                if job.input_artifact_id is None
                else await session.scalar(
                    select(Artifact).where(
                        Artifact.id == job.input_artifact_id,
                        Artifact.analysis_id == job.id,
                        Artifact.artifact_kind == "apk",
                        Artifact.state == "finalized",
                    )
                )
            )
            scenario_rows = list(
                (
                    await session.scalars(
                        select(ScenarioResult).where(
                            ScenarioResult.analysis_id == job.id,
                            ScenarioResult.state.in_(("queued", "scheduled", "running")),
                        )
                    )
                ).all()
            )
        if (
            application_version is None
            or artifact is None
            or application_version.launch_activity is None
            or len(scenario_rows) != 3
        ):
            raise AgentTaskUnavailable("Task projection is unavailable")
        by_type = {item.scenario_type: item for item in scenario_rows}
        if set(by_type) != {"cold_start", "scroll", "memory_cycle"}:
            raise AgentTaskUnavailable("Task scenarios are unavailable")
        scenarios = tuple(
            _scenario_from_result(by_type[scenario_type])
            for scenario_type in ("cold_start", "scroll", "memory_cycle")
        )
        return AgentTaskDefinition(
            analysis_id=job.id,
            team_id=job.team_id,
            agent_id=device.agent_id,
            device_id=device.id,
            device_digest=device.serial_digest,
            package_name=application_version.package_name,
            launch_activity=_launch_component(
                application_version.package_name,
                application_version.launch_activity,
            ),
            cleanup_policy="uninstall",
            input_artifacts=(
                TaskInputArtifact(
                    artifact_id=artifact.id,
                    kind="apk",
                    mime=artifact.mime_type,
                    size=artifact.size_bytes,
                    sha256_b64=artifact.sha256_b64,
                ),
            ),
            scenarios=scenarios,
        )

    async def record_snapshot_digest(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        digest: str,
        now: datetime,
    ) -> None:
        async with self._control_sessions() as session:
            async with session.begin():
                changed = await session.scalar(
                    update(AgentLease)
                    .where(
                        AgentLease.agent_id == agent_id,
                        AgentLease.execution_id == execution_id,
                        AgentLease.state == "active",
                        AgentLease.version == lease_version,
                        AgentLease.expires_at > now,
                    )
                    .values(task_snapshot_digest=digest, updated_at=now)
                    .returning(AgentLease.id)
                )
                if changed is None:
                    raise AgentTaskNotFound

    async def renew(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> LeaseRenewal:
        async with self._control_sessions() as session:
            async with session.begin():
                lease = await session.scalar(
                    select(AgentLease)
                    .where(AgentLease.execution_id == execution_id)
                    .with_for_update()
                )
                if (
                    lease is None
                    or lease.agent_id != agent_id
                    or lease.state != "active"
                    or lease.expires_at <= now
                ):
                    raise AgentTaskNotFound
                if lease.version != lease_version:
                    raise StaleLeaseVersion
                lease.expires_at = max(lease.expires_at, now + _LEASE_TTL)
                lease.renewed_at = now
                lease.updated_at = now
                return LeaseRenewal(
                    execution_id=execution_id,
                    lease_version=lease.version,
                    lease_expires_at=lease.expires_at,
                )

    async def authorize_execution(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess:
        _require_aware(now)
        async with self._control_sessions() as session:
            row = (
                await session.execute(
                    select(AgentLease, GlobalJob)
                    .join(GlobalJob, GlobalJob.id == AgentLease.global_job_id)
                    .where(AgentLease.execution_id == execution_id)
                )
            ).one_or_none()
        if row is None:
            raise AgentTaskNotFound
        lease, job = row
        if (
            lease.agent_id != agent_id
            or lease.state != "active"
            or lease.expires_at <= now
            or job.state not in ("scheduled", "running")
        ):
            raise AgentTaskNotFound
        if lease.version != lease_version:
            raise StaleLeaseVersion
        return AgentExecutionAccess(
            team_id=job.team_id,
            analysis_id=job.id,
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=lease.expires_at,
        )


def _task_claims(task: ActiveAgentTask, *, issued_at: datetime) -> dict[str, object]:
    definition = task.definition
    return {
        "schema_version": "1.0",
        "aud": "perfpilot-agent",
        "agent_id": str(definition.agent_id),
        "device_digest": definition.device_digest,
        "execution_id": str(task.execution_id),
        "lease_version": task.lease_version,
        "analysis_id": str(definition.analysis_id),
        "issued_at": issued_at.astimezone(UTC).isoformat(),
        "expires_at": min(
            task.lease_expires_at.astimezone(UTC),
            issued_at.astimezone(UTC) + timedelta(seconds=90),
        ).isoformat(),
        "package_name": definition.package_name,
        "launch_activity": definition.launch_activity,
        "cleanup_policy": definition.cleanup_policy,
        "input_artifacts": [
            {
                "artifact_id": str(item.artifact_id),
                "kind": item.kind,
                "mime": item.mime,
                "size": item.size,
                "sha256_b64": item.sha256_b64,
            }
            for item in definition.input_artifacts
        ],
        "scenarios": [
            {
                "scenario_type": item.scenario_type,
                "recipe_version": item.recipe_version,
                "recipe_hash": item.recipe_hash,
                "duration_seconds": item.duration_seconds,
                "memory_rounds": item.memory_rounds,
                "swipe_count": item.swipe_count,
            }
            for item in definition.scenarios
        ],
        "allowed_uploads": list(_ALLOWED_UPLOADS),
    }


def _scheduled(lease: _MemoryLease) -> ScheduledAgentTask:
    return ScheduledAgentTask(
        lease_id=lease.lease_id,
        execution_id=lease.execution_id,
        analysis_id=lease.definition.analysis_id,
        agent_id=lease.definition.agent_id,
        device_id=lease.definition.device_id,
        lease_version=lease.lease_version,
        lease_expires_at=lease.expires_at,
    )


def _agent_is_idle(agent: Agent) -> bool:
    slot = agent.capabilities.get("execution_slot")
    return (
        isinstance(slot, dict) and slot.get("state") == "idle" and slot.get("execution_id") is None
    )


def _scenario_from_result(result: ScenarioResult) -> TaskScenario:
    if (
        result.recipe_version is None
        or result.recipe_hash is None
        or result.recipe_snapshot is None
    ):
        raise AgentTaskUnavailable("Task recipe is unavailable")
    recipe = result.recipe_snapshot
    memory_rounds = 0
    swipe_count = 0
    duration_seconds = {"cold_start": 15, "scroll": 30, "memory_cycle": 60}.get(
        result.scenario_type
    )
    if duration_seconds is None:
        raise AgentTaskUnavailable("Task scenario is invalid")
    if result.scenario_type == "scroll":
        scroll = recipe.get("scroll")
        if isinstance(scroll, dict) and type(scroll.get("iterations")) is int:
            swipe_count = cast(int, scroll["iterations"])
    elif result.scenario_type == "memory_cycle":
        memory = recipe.get("memory_cycle")
        if isinstance(memory, dict) and type(memory.get("iterations")) is int:
            memory_rounds = cast(int, memory["iterations"])
    scenario_type: TaskScenarioType = (
        "startup"
        if result.scenario_type == "cold_start"
        else cast(TaskScenarioType, result.scenario_type)
    )
    return TaskScenario(
        scenario_type=scenario_type,
        recipe_version=result.recipe_version,
        recipe_hash=result.recipe_hash,
        duration_seconds=duration_seconds,
        memory_rounds=memory_rounds,
        swipe_count=swipe_count,
    )


def _launch_component(package_name: str, activity: str) -> str:
    if "/" in activity:
        return activity
    component = f"{package_name}{activity}" if activity.startswith(".") else activity
    return f"{package_name}/{component}"


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("Agent task clock must return an aware datetime")


def _aware_now(value: datetime) -> datetime:
    _require_aware(value)
    return value.astimezone(UTC)


__all__ = [
    "ActiveAgentTask",
    "AgentTaskDefinition",
    "AgentTaskDelivery",
    "AgentExecutionAccess",
    "AgentTaskError",
    "AgentTaskNotFound",
    "AgentTaskService",
    "AgentTaskUnavailable",
    "InMemoryAgentTaskRepository",
    "InMemoryAgentTaskWakeup",
    "LeaseRenewal",
    "RedisAgentTaskWakeup",
    "SQLAlchemyAgentTaskRepository",
    "ScheduledAgentTask",
    "StaleLeaseVersion",
    "TaskInputArtifact",
    "TaskScenario",
]
