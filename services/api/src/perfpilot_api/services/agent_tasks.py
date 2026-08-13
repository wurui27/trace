from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4, uuid5

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import (
    Agent,
    AgentLease,
    Device,
    GlobalJob,
    OutboxEvent,
    ScenarioJob,
    SourceTask,
)
from perfpilot_api.db.tenant.models import Analysis, ApplicationVersion, Artifact, ScenarioResult
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.security.task_snapshots import (
    TaskSnapshotSigner,
    snapshot_digest,
)
from perfpilot_api.services.analyses import trace_analysis_ready_event_id

_LEASE_TTL = timedelta(seconds=60)
_FRESHNESS = timedelta(seconds=30)
_RENEW_AFTER_SECONDS = 20
_SCENARIO_UPLOADS: dict[TaskScenarioType, str]  # Defined after TaskScenarioType.
_COMPLETION_CONTRACT = (
    Path(__file__).resolve().parents[5]
    / "contracts"
    / "v1"
    / "agents"
    / "execution-manifest.schema.json"
)
_EVENT_NAMESPACE = UUID("0e34d746-295e-5d1c-bf2c-98333435f5e7")
_MAXIMUM_EXECUTION_DURATION = timedelta(hours=24)
_MAXIMUM_COMPLETION_SKEW = timedelta(minutes=5)

TaskScenarioType = Literal["startup", "scroll", "memory_cycle"]
TaskInputKind = Literal["apk", "scenario_fixture", "dataset"]
CleanupPolicy = Literal["keep_installed", "uninstall"]
_SCENARIO_UPLOADS = {
    "startup": "startup_trace",
    "scroll": "scroll_trace",
    "memory_cycle": "memory_evidence",
}


class AgentTaskError(RuntimeError):
    pass


class AgentTaskNotFound(AgentTaskError):
    pass


class StaleLeaseVersion(AgentTaskError):
    pass


class AgentTaskUnavailable(AgentTaskError):
    pass


class AgentTaskConflict(AgentTaskError):
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
    schema_version: Literal["1.0", "1.1"] = "1.0"


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
class AgentTaskCancellation:
    execution_id: UUID
    lease_version: int
    requested_at: datetime
    reason_code: Literal["analysis_canceled"] = "analysis_canceled"


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
    allowed_uploads: tuple[str, ...] = ("agent_log",)
    scenario_types: tuple[TaskScenarioType, ...] = ()
    input_artifact_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentExecutionArtifact:
    artifact_id: UUID
    kind: str
    mime: str
    size: int
    sha256_b64: str


@dataclass(frozen=True, slots=True)
class AgentExecutionScenario:
    scenario_type: TaskScenarioType
    state: Literal["completed", "failed", "skipped"]
    started_at: datetime
    completed_at: datetime
    temperature_start_c: int | float | None
    temperature_end_c: int | float | None
    artifact_ids: tuple[UUID, ...]
    diagnostic_code: str | None


@dataclass(frozen=True, slots=True)
class ValidatedAgentExecutionManifest:
    execution_id: UUID
    lease_version: int
    state: Literal["completed", "failed"]
    started_at: datetime
    completed_at: datetime
    agent_version: str
    adb_version: str
    artifacts: tuple[AgentExecutionArtifact, ...]
    scenarios: tuple[AgentExecutionScenario, ...]
    diagnostic_code: str | None
    document_hash: str


@dataclass(frozen=True, slots=True)
class AgentExecutionCompletion:
    execution_id: UUID
    analysis_id: UUID
    lease_version: int
    analysis_state: Literal["analyzing", "failed"]
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class AgentCancellationRequest:
    team_id: UUID
    analysis_id: UUID
    analysis_state: str
    cancel_requested_at: datetime | None
    agent_id: UUID | None
    execution_id: UUID | None
    lease_version: int | None


@dataclass(frozen=True, slots=True)
class AgentCancellationAcknowledgement:
    execution_id: UUID
    analysis_id: UUID
    lease_version: int
    acknowledged_at: datetime


class AgentCompletionArtifactValidator(Protocol):
    async def validate_completion(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
    ) -> None: ...

    async def project_completion(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> None: ...


class AgentCancellationArtifactCoordinator(Protocol):
    async def abort_execution(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> None: ...

    async def project_cancellation(
        self,
        *,
        access: AgentExecutionAccess,
        reason_code: str,
        now: datetime,
    ) -> None: ...


@lru_cache(maxsize=1)
def _execution_manifest_validator() -> Draft202012Validator:
    try:
        schema = json.loads(_COMPLETION_CONTRACT.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        raise AgentTaskUnavailable("Execution manifest contract is unavailable") from None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _manifest_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AgentTaskConflict("Execution manifest is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AgentTaskConflict("Execution manifest is invalid") from None
    if parsed.tzinfo is None:
        raise AgentTaskConflict("Execution manifest is invalid")
    return parsed.astimezone(UTC)


def validate_agent_execution_manifest(
    document: Mapping[str, object],
    *,
    execution_id: UUID,
    lease_version: int,
    expected_scenarios: tuple[TaskScenarioType, ...],
    now: datetime,
) -> ValidatedAgentExecutionManifest:
    if not isinstance(document, Mapping):
        raise AgentTaskConflict("Execution manifest is invalid")
    normalized = dict(document)
    try:
        _execution_manifest_validator().validate(normalized)
        canonical = json.dumps(
            normalized,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except ValidationError:
        raise AgentTaskConflict("Execution manifest is invalid") from None
    except (TypeError, ValueError, UnicodeError):
        raise AgentTaskConflict("Execution manifest is invalid") from None

    try:
        manifest_execution_id = UUID(str(normalized["execution_id"]))
        manifest_lease_version = int(normalized["lease_version"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AgentTaskConflict("Execution manifest is invalid") from None
    if manifest_execution_id != execution_id or manifest_lease_version != lease_version:
        raise AgentTaskConflict("Execution manifest does not match the lease")

    started_at = _manifest_time(normalized["started_at"])
    completed_at = _manifest_time(normalized["completed_at"])
    if (
        completed_at < started_at
        or completed_at - started_at > _MAXIMUM_EXECUTION_DURATION
        or completed_at > now + _MAXIMUM_COMPLETION_SKEW
    ):
        raise AgentTaskConflict("Execution manifest timestamps are invalid")

    artifacts: list[AgentExecutionArtifact] = []
    artifact_ids: set[UUID] = set()
    artifact_kinds: set[str] = set()
    for item in cast(list[dict[str, object]], normalized["artifacts"]):
        artifact_id = UUID(cast(str, item["artifact_id"]))
        kind = cast(str, item["kind"])
        if artifact_id in artifact_ids or kind in artifact_kinds:
            raise AgentTaskConflict("Execution artifacts are duplicated")
        artifact_ids.add(artifact_id)
        artifact_kinds.add(kind)
        artifacts.append(
            AgentExecutionArtifact(
                artifact_id=artifact_id,
                kind=kind,
                mime=cast(str, item["mime"]),
                size=cast(int, item["size"]),
                sha256_b64=cast(str, item["sha256_b64"]),
            )
        )

    scenarios: list[AgentExecutionScenario] = []
    scenario_types: set[TaskScenarioType] = set()
    referenced_artifacts: set[UUID] = set()
    by_artifact_id = {item.artifact_id: item for item in artifacts}
    expected_kind = {
        "startup": "startup_trace",
        "scroll": "scroll_trace",
        "memory_cycle": "memory_evidence",
    }
    for item in cast(list[dict[str, object]], normalized["scenarios"]):
        scenario_type = cast(TaskScenarioType, item["scenario_type"])
        if scenario_type in scenario_types:
            raise AgentTaskConflict("Execution scenarios are duplicated")
        scenario_types.add(scenario_type)
        state = cast(Literal["completed", "failed", "skipped"], item["state"])
        scenario_started_at = _manifest_time(item["started_at"])
        scenario_completed_at = _manifest_time(item["completed_at"])
        if (
            scenario_started_at < started_at
            or scenario_completed_at < scenario_started_at
            or scenario_completed_at > completed_at
        ):
            raise AgentTaskConflict("Execution scenario timestamps are invalid")
        ids = tuple(UUID(value) for value in cast(list[str], item["artifact_ids"]))
        if any(artifact_id not in artifact_ids for artifact_id in ids):
            raise AgentTaskConflict("Execution scenario artifacts are invalid")
        diagnostic_code = cast(str | None, item["diagnostic_code"])
        if state == "completed":
            if diagnostic_code is not None or not any(
                by_artifact_id[artifact_id].kind == expected_kind[scenario_type]
                for artifact_id in ids
            ):
                raise AgentTaskConflict("Completed scenario evidence is invalid")
        elif diagnostic_code is None:
            raise AgentTaskConflict("Failed scenario diagnostic is missing")
        referenced_artifacts.update(ids)
        scenarios.append(
            AgentExecutionScenario(
                scenario_type=scenario_type,
                state=state,
                started_at=scenario_started_at,
                completed_at=scenario_completed_at,
                temperature_start_c=cast(int | float | None, item["temperature_start_c"]),
                temperature_end_c=cast(int | float | None, item["temperature_end_c"]),
                artifact_ids=ids,
                diagnostic_code=diagnostic_code,
            )
        )

    if expected_scenarios and scenario_types != set(expected_scenarios):
        raise AgentTaskConflict("Execution scenarios do not match the signed task")
    if any(
        artifact.artifact_id not in referenced_artifacts and artifact.kind != "agent_log"
        for artifact in artifacts
    ):
        raise AgentTaskConflict("Execution artifact is not owned by a scenario")
    manifest_state = cast(Literal["completed", "failed"], normalized["state"])
    diagnostic_code = cast(str | None, normalized["diagnostic_code"])
    if manifest_state == "completed":
        if diagnostic_code is not None or not any(item.state == "completed" for item in scenarios):
            raise AgentTaskConflict("Completed execution state is invalid")
    elif diagnostic_code is None:
        raise AgentTaskConflict("Failed execution diagnostic is missing")

    return ValidatedAgentExecutionManifest(
        execution_id=manifest_execution_id,
        lease_version=manifest_lease_version,
        state=manifest_state,
        started_at=started_at,
        completed_at=completed_at,
        agent_version=cast(str, normalized["agent_version"]),
        adb_version=cast(str, normalized["adb_version"]),
        artifacts=tuple(artifacts),
        scenarios=tuple(scenarios),
        diagnostic_code=diagnostic_code,
        document_hash=hashlib.sha256(canonical).hexdigest(),
    )


class AgentTaskRepository(Protocol):
    async def schedule(
        self,
        *,
        analysis_id: UUID | None,
        now: datetime,
        agent_id: UUID | None = None,
    ) -> ScheduledAgentTask | None: ...

    async def oldest_queued(
        self, *, agent_id: UUID, now: datetime
    ) -> tuple[datetime, UUID] | None: ...

    async def load_active(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> ActiveAgentTask | None: ...

    async def load_cancellation(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> AgentTaskCancellation | None: ...

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
    ) -> LeaseRenewal | AgentTaskCancellation: ...

    async def request_cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> AgentCancellationRequest: ...

    async def project_unleased_cancellation(
        self,
        *,
        cancellation: AgentCancellationRequest,
        now: datetime,
    ) -> None: ...

    async def authorize_cancellation(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess: ...

    async def acknowledge_cancellation(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> AgentCancellationAcknowledgement: ...

    async def authorize_execution(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess: ...

    async def authorize_completion(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        manifest_digest: str,
        now: datetime,
    ) -> AgentExecutionAccess: ...

    async def complete_execution(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> AgentExecutionCompletion: ...


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
    state: Literal["active", "cancel_requested", "released"] = "active"
    cancel_requested_at: datetime | None = None
    cancel_acknowledged_at: datetime | None = None
    completion_manifest_digest: str | None = None
    completion: AgentExecutionCompletion | None = None


def _allowed_uploads(
    scenario_types: Sequence[TaskScenarioType],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*(_SCENARIO_UPLOADS[item] for item in scenario_types), "agent_log")))


class InMemoryAgentTaskRepository:
    __slots__ = (
        "_definitions",
        "_execution_id_source",
        "_lease_id_source",
        "_leases",
        "_lock",
        "_queued_at",
    )

    def __init__(
        self,
        definitions: Sequence[AgentTaskDefinition] = (),
        *,
        lease_id_source: Callable[[], UUID] = uuid4,
        execution_id_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._definitions: dict[UUID, AgentTaskDefinition] = {}
        self._queued_at: dict[UUID, datetime] = {}
        baseline = datetime.min.replace(tzinfo=UTC)
        for position, item in enumerate(definitions):
            existing = self._definitions.get(item.analysis_id)
            if existing is not None and existing != item:
                raise AgentTaskConflict("Task identity is already queued with other content")
            self._definitions[item.analysis_id] = item
            self._queued_at.setdefault(item.analysis_id, baseline + timedelta(microseconds=position))
        self._lease_id_source = lease_id_source
        self._execution_id_source = execution_id_source
        self._leases: dict[UUID, _MemoryLease] = {}
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"InMemoryAgentTaskRepository(definitions={len(self._definitions)})"

    async def enqueue(
        self,
        definition: AgentTaskDefinition,
        *,
        queued_at: datetime,
    ) -> bool:
        _require_aware(queued_at)
        if definition.schema_version != "1.1" or tuple(
            item.scenario_type for item in definition.scenarios
        ) != (
            "startup",
            "scroll",
        ) or tuple(item.kind for item in definition.input_artifacts) != ("apk",):
            raise AgentTaskConflict("Remote device tasks require startup and scroll scenarios")
        async with self._lock:
            existing = self._definitions.get(definition.analysis_id)
            if existing is not None:
                if existing != definition:
                    raise AgentTaskConflict(
                        "Task identity is already queued with other content"
                    )
                return False
            self._definitions[definition.analysis_id] = definition
            self._queued_at[definition.analysis_id] = queued_at.astimezone(UTC)
            return True

    async def schedule(
        self,
        *,
        analysis_id: UUID | None,
        now: datetime,
        agent_id: UUID | None = None,
    ) -> ScheduledAgentTask | None:
        _require_aware(now)
        candidates = (
            (self._definitions.get(analysis_id),)
            if analysis_id is not None
            else tuple(
                self._definitions[item_id]
                for item_id in sorted(
                    self._definitions,
                    key=lambda item_id: (self._queued_at[item_id], item_id),
                )
            )
        )
        for definition in candidates:
            if definition is None:
                continue
            if agent_id is not None and definition.agent_id != agent_id:
                continue
            existing = next(
                (
                    lease
                    for lease in self._leases.values()
                    if lease.definition.analysis_id == definition.analysis_id
                    and lease.state in ("active", "cancel_requested")
                    and lease.expires_at > now
                ),
                None,
            )
            if existing is not None:
                return _scheduled(existing)
            if any(
                lease.expires_at > now
                and lease.state in ("active", "cancel_requested")
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

    async def oldest_queued(
        self, *, agent_id: UUID, now: datetime
    ) -> tuple[datetime, UUID] | None:
        _require_aware(now)
        async with self._lock:
            candidates = (
                (queued_at, definition.analysis_id)
                for definition in self._definitions.values()
                if definition.agent_id == agent_id
                for queued_at in (self._queued_at[definition.analysis_id],)
                if not any(
                    lease.definition.analysis_id == definition.analysis_id
                    and lease.state in ("active", "cancel_requested")
                    and lease.expires_at > now
                    for lease in self._leases.values()
                )
            )
            return min(candidates, default=None)

    async def load_cancellation(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> AgentTaskCancellation | None:
        _require_aware(now)
        for lease in self._leases.values():
            if (
                lease.definition.agent_id == agent_id
                and lease.state == "cancel_requested"
                and lease.expires_at > now
                and lease.cancel_requested_at is not None
            ):
                return AgentTaskCancellation(
                    execution_id=lease.execution_id,
                    lease_version=lease.lease_version,
                    requested_at=lease.cancel_requested_at,
                )
        return None

    async def load_active(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> ActiveAgentTask | None:
        _require_aware(now)
        for lease in self._leases.values():
            if (
                lease.definition.agent_id == agent_id
                and lease.state == "active"
                and lease.expires_at > now
            ):
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
            or lease.state != "active"
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
    ) -> LeaseRenewal | AgentTaskCancellation:
        _require_aware(now)
        lease = self._leases.get(execution_id)
        if lease is None or lease.definition.agent_id != agent_id or lease.expires_at <= now:
            raise AgentTaskNotFound
        if lease.lease_version != lease_version:
            raise StaleLeaseVersion
        if lease.state == "cancel_requested" and lease.cancel_requested_at is not None:
            return AgentTaskCancellation(
                execution_id=execution_id,
                lease_version=lease_version,
                requested_at=lease.cancel_requested_at,
            )
        if lease.state != "active":
            raise AgentTaskNotFound
        expires_at = max(lease.expires_at, now + _LEASE_TTL)
        self._leases[execution_id] = replace(lease, expires_at=expires_at)
        return LeaseRenewal(
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=expires_at,
        )

    async def request_cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> AgentCancellationRequest:
        _require_aware(now)
        definition = self._definitions.get(analysis_id)
        if definition is None or definition.team_id != team_id:
            raise AgentTaskNotFound
        lease = next(
            (
                item
                for item in self._leases.values()
                if item.definition.analysis_id == analysis_id
                and item.state in ("active", "cancel_requested")
            ),
            None,
        )
        if lease is None:
            return AgentCancellationRequest(
                team_id=team_id,
                analysis_id=analysis_id,
                analysis_state="canceled",
                cancel_requested_at=now,
                agent_id=None,
                execution_id=None,
                lease_version=None,
            )
        requested_at = lease.cancel_requested_at or now
        self._leases[lease.execution_id] = replace(
            lease,
            state="cancel_requested",
            cancel_requested_at=requested_at,
        )
        return AgentCancellationRequest(
            team_id=team_id,
            analysis_id=analysis_id,
            analysis_state="scheduled",
            cancel_requested_at=requested_at,
            agent_id=definition.agent_id,
            execution_id=lease.execution_id,
            lease_version=lease.lease_version,
        )

    async def project_unleased_cancellation(
        self,
        *,
        cancellation: AgentCancellationRequest,
        now: datetime,
    ) -> None:
        del cancellation
        _require_aware(now)

    async def authorize_cancellation(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        now: datetime,
    ) -> AgentExecutionAccess:
        _require_aware(now)
        lease = self._leases.get(execution_id)
        if lease is None or lease.definition.agent_id != agent_id:
            raise AgentTaskNotFound
        if lease.lease_version != lease_version:
            raise StaleLeaseVersion
        if lease.state == "cancel_requested" and lease.cancel_requested_at is not None:
            pass
        elif lease.state == "released" and lease.cancel_acknowledged_at is not None:
            pass
        else:
            raise AgentTaskNotFound
        return AgentExecutionAccess(
            team_id=lease.definition.team_id,
            analysis_id=lease.definition.analysis_id,
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=lease.expires_at,
            allowed_uploads=_allowed_uploads(
                tuple(item.scenario_type for item in lease.definition.scenarios)
            ),
            scenario_types=tuple(item.scenario_type for item in lease.definition.scenarios),
            input_artifact_ids=tuple(item.artifact_id for item in lease.definition.input_artifacts),
        )

    async def acknowledge_cancellation(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> AgentCancellationAcknowledgement:
        _require_aware(now)
        lease = self._leases.get(access.execution_id)
        if (
            lease is None
            or lease.definition.agent_id != access.agent_id
            or lease.lease_version != access.lease_version
        ):
            raise AgentTaskNotFound
        acknowledged_at = lease.cancel_acknowledged_at or now
        if lease.state == "released" and lease.cancel_acknowledged_at is not None:
            pass
        elif lease.state == "cancel_requested":
            self._leases[access.execution_id] = replace(
                lease,
                state="released",
                cancel_acknowledged_at=acknowledged_at,
            )
        else:
            raise AgentTaskNotFound
        return AgentCancellationAcknowledgement(
            execution_id=access.execution_id,
            analysis_id=access.analysis_id,
            lease_version=access.lease_version,
            acknowledged_at=acknowledged_at,
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
        if (
            lease is None
            or lease.definition.agent_id != agent_id
            or lease.state != "active"
            or lease.expires_at <= now
        ):
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
            allowed_uploads=_allowed_uploads(
                tuple(item.scenario_type for item in lease.definition.scenarios)
            ),
            scenario_types=tuple(item.scenario_type for item in lease.definition.scenarios),
            input_artifact_ids=tuple(item.artifact_id for item in lease.definition.input_artifacts),
        )

    async def authorize_completion(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        manifest_digest: str,
        now: datetime,
    ) -> AgentExecutionAccess:
        _require_aware(now)
        lease = self._leases.get(execution_id)
        if lease is None or lease.definition.agent_id != agent_id:
            raise AgentTaskNotFound
        if lease.lease_version != lease_version:
            raise StaleLeaseVersion
        if lease.state == "active" and lease.expires_at <= now:
            raise AgentTaskNotFound
        if lease.state == "released" and lease.completion_manifest_digest != manifest_digest:
            raise AgentTaskConflict("Execution was already completed with another manifest")
        return AgentExecutionAccess(
            team_id=lease.definition.team_id,
            analysis_id=lease.definition.analysis_id,
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=lease.expires_at,
            allowed_uploads=_allowed_uploads(
                tuple(item.scenario_type for item in lease.definition.scenarios)
            ),
            scenario_types=tuple(item.scenario_type for item in lease.definition.scenarios),
            input_artifact_ids=tuple(item.artifact_id for item in lease.definition.input_artifacts),
        )

    async def complete_execution(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> AgentExecutionCompletion:
        lease = self._leases.get(access.execution_id)
        if lease is None or lease.definition.agent_id != access.agent_id:
            raise AgentTaskNotFound
        if lease.state == "released":
            if lease.completion_manifest_digest != manifest.document_hash:
                raise AgentTaskConflict("Execution was already completed with another manifest")
            if lease.completion is None:
                raise AgentTaskUnavailable("Execution completion is unavailable")
            return lease.completion
        if (
            lease.state != "active"
            or lease.lease_version != access.lease_version
            or lease.expires_at <= now
        ):
            raise AgentTaskNotFound
        completion = AgentExecutionCompletion(
            execution_id=access.execution_id,
            analysis_id=access.analysis_id,
            lease_version=access.lease_version,
            analysis_state="analyzing" if manifest.state == "completed" else "failed",
            accepted_at=now,
        )
        self._leases[access.execution_id] = replace(
            lease,
            state="released",
            completion_manifest_digest=manifest.document_hash,
            completion=completion,
        )
        return completion

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

    async def schedule(
        self,
        *,
        analysis_id: UUID | None = None,
        agent_id: UUID | None = None,
    ) -> ScheduledAgentTask | None:
        now = _aware_now(self._clock())
        scheduled = await self._repository.schedule(
            analysis_id=analysis_id,
            agent_id=agent_id,
            now=now,
        )
        if scheduled is not None:
            await self._wakeup.wake(scheduled.agent_id)
        return scheduled

    async def oldest_queued(
        self, *, agent_id: UUID
    ) -> tuple[datetime, UUID] | None:
        return await self._repository.oldest_queued(
            agent_id=agent_id,
            now=_aware_now(self._clock()),
        )

    async def poll(
        self,
        *,
        agent_id: UUID,
        wait_seconds: int,
    ) -> AgentTaskDelivery | AgentTaskCancellation | None:
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, int)
            or not 0 <= wait_seconds <= 20
        ):
            raise ValueError("Agent task poll wait is invalid")
        now = _aware_now(self._clock())
        cancellation = await self._repository.load_cancellation(agent_id=agent_id, now=now)
        if cancellation is not None:
            return cancellation
        task = await self._repository.load_active(agent_id=agent_id, now=now)
        if task is None and wait_seconds:
            await self._wakeup.wait(agent_id, wait_seconds)
            now = _aware_now(self._clock())
            cancellation = await self._repository.load_cancellation(agent_id=agent_id, now=now)
            if cancellation is not None:
                return cancellation
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
    ) -> LeaseRenewal | AgentTaskCancellation:
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

    async def request_cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> AgentCancellationRequest:
        cancellation = await self._repository.request_cancel(
            team_id=team_id,
            analysis_id=analysis_id,
            now=_aware_now(self._clock()),
        )
        if (
            cancellation.analysis_state == "canceled"
            and cancellation.execution_id is None
            and cancellation.cancel_requested_at is not None
        ):
            await self._repository.project_unleased_cancellation(
                cancellation=cancellation,
                now=_aware_now(self._clock()),
            )
        if cancellation.agent_id is not None:
            await self._wakeup.wake(cancellation.agent_id)
        return cancellation

    async def acknowledge_cancellation(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        reason_code: str,
        artifact_coordinator: AgentCancellationArtifactCoordinator,
    ) -> AgentCancellationAcknowledgement:
        if reason_code != "analysis_canceled":
            raise ValueError("Cancellation reason is invalid")
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version < 1
        ):
            raise StaleLeaseVersion
        now = _aware_now(self._clock())
        access = await self._repository.authorize_cancellation(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            now=now,
        )
        await artifact_coordinator.abort_execution(access=access, now=now)
        acknowledgement = await self._repository.acknowledge_cancellation(
            access=access,
            now=now,
        )
        await artifact_coordinator.project_cancellation(
            access=access,
            reason_code=reason_code,
            now=now,
        )
        return acknowledgement

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

    async def complete(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        manifest_document: Mapping[str, object],
        artifact_validator: AgentCompletionArtifactValidator,
    ) -> AgentExecutionCompletion:
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version < 1
        ):
            raise StaleLeaseVersion
        now = _aware_now(self._clock())
        preliminary = validate_agent_execution_manifest(
            manifest_document,
            execution_id=execution_id,
            lease_version=lease_version,
            expected_scenarios=(),
            now=now,
        )
        access = await self._repository.authorize_completion(
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            manifest_digest=preliminary.document_hash,
            now=now,
        )
        manifest = validate_agent_execution_manifest(
            manifest_document,
            execution_id=execution_id,
            lease_version=lease_version,
            expected_scenarios=access.scenario_types,
            now=now,
        )
        await artifact_validator.validate_completion(
            access=access,
            manifest=manifest,
        )
        completion = await self._repository.complete_execution(
            access=access,
            manifest=manifest,
            now=now,
        )
        await artifact_validator.project_completion(
            access=access,
            manifest=manifest,
            now=now,
        )
        return completion


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
        agent_id: UUID | None = None,
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
                    if agent_id is not None:
                        statement = statement.where(Agent.id == agent_id)
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
                    oldest_source = (
                        await session.execute(
                            select(SourceTask.created_at, SourceTask.id)
                            .where(
                                SourceTask.agent_id == agent.id,
                                SourceTask.state == "queued",
                            )
                            .order_by(SourceTask.created_at, SourceTask.id)
                            .limit(1)
                        )
                    ).one_or_none()
                    if oldest_source is not None and (
                        oldest_source.created_at,
                        oldest_source.id,
                    ) <= (job.created_at, job.id):
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
                    active_source_task = await session.scalar(
                        select(SourceTask.id)
                        .where(
                            SourceTask.agent_id == agent.id,
                            SourceTask.state.in_(
                                ("leased", "running", "cancel_requested")
                            ),
                            SourceTask.expires_at > now,
                        )
                        .limit(1)
                    )
                    if active_source_task is not None:
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

    async def oldest_queued(
        self, *, agent_id: UUID, now: datetime
    ) -> tuple[datetime, UUID] | None:
        _require_aware(now)
        async with self._control_sessions() as session:
            row = (
                await session.execute(
                    select(GlobalJob.created_at, GlobalJob.id)
                    .join(Device, Device.id == GlobalJob.selected_device_id)
                    .join(Agent, Agent.id == Device.agent_id)
                    .where(
                        Agent.id == agent_id,
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
                )
            ).one_or_none()
        return None if row is None else (row.created_at, row.id)

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

    async def load_cancellation(
        self,
        *,
        agent_id: UUID,
        now: datetime,
    ) -> AgentTaskCancellation | None:
        _require_aware(now)
        async with self._control_sessions() as session:
            row = (
                await session.execute(
                    select(AgentLease, GlobalJob)
                    .join(GlobalJob, GlobalJob.id == AgentLease.global_job_id)
                    .where(
                        AgentLease.agent_id == agent_id,
                        AgentLease.state == "cancel_requested",
                        AgentLease.expires_at > now,
                        GlobalJob.cancel_requested_at.is_not(None),
                    )
                    .order_by(AgentLease.acquired_at, AgentLease.id)
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return None
        lease, job = row
        if job.cancel_requested_at is None:
            raise AgentTaskUnavailable("Cancellation state is unavailable")
        return AgentTaskCancellation(
            execution_id=lease.execution_id,
            lease_version=lease.version,
            requested_at=job.cancel_requested_at,
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
    ) -> LeaseRenewal | AgentTaskCancellation:
        async with self._control_sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentLease, GlobalJob)
                        .join(GlobalJob, GlobalJob.id == AgentLease.global_job_id)
                        .where(AgentLease.execution_id == execution_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    raise AgentTaskNotFound
                lease, job = row
                if lease.agent_id != agent_id or lease.expires_at <= now:
                    raise AgentTaskNotFound
                if lease.version != lease_version:
                    raise StaleLeaseVersion
                if lease.state == "cancel_requested":
                    if job.cancel_requested_at is None:
                        raise AgentTaskUnavailable("Cancellation state is unavailable")
                    return AgentTaskCancellation(
                        execution_id=execution_id,
                        lease_version=lease.version,
                        requested_at=job.cancel_requested_at,
                    )
                if lease.state != "active":
                    raise AgentTaskNotFound
                lease.expires_at = max(lease.expires_at, now + _LEASE_TTL)
                lease.renewed_at = now
                lease.updated_at = now
                return LeaseRenewal(
                    execution_id=execution_id,
                    lease_version=lease.version,
                    lease_expires_at=lease.expires_at,
                )

    async def request_cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> AgentCancellationRequest:
        _require_aware(now)
        async with self._control_sessions() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(GlobalJob.id == analysis_id, GlobalJob.team_id == team_id)
                    .with_for_update()
                )
                if job is None:
                    raise AgentTaskNotFound
                if job.state in ("completed", "partially_completed", "failed", "canceled"):
                    return AgentCancellationRequest(
                        team_id=team_id,
                        analysis_id=analysis_id,
                        analysis_state=job.state,
                        cancel_requested_at=job.cancel_requested_at,
                        agent_id=None,
                        execution_id=None,
                        lease_version=None,
                    )
                requested_at = job.cancel_requested_at or now
                if job.cancel_requested_at is None:
                    job.cancel_requested_at = requested_at
                    job.version += 1
                    job.updated_at = now
                lease = await session.scalar(
                    select(AgentLease)
                    .where(
                        AgentLease.global_job_id == analysis_id,
                        AgentLease.state.in_(("active", "cancel_requested")),
                    )
                    .order_by(AgentLease.acquired_at.desc(), AgentLease.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                if lease is None:
                    job.state = "canceled"
                    job.completed_at = now
                    job.failure_code = None
                    job.version += 1
                    job.updated_at = now
                    await session.execute(
                        update(ScenarioJob)
                        .where(
                            ScenarioJob.analysis_id == analysis_id,
                            ScenarioJob.state.in_(("queued", "scheduled", "running", "analyzing")),
                        )
                        .values(
                            state="canceled",
                            completed_at=now,
                            failure_code=None,
                            version=ScenarioJob.version + 1,
                            updated_at=now,
                        )
                    )
                else:
                    if lease.state == "active":
                        lease.state = "cancel_requested"
                        lease.updated_at = now
                    await session.execute(
                        update(ScenarioJob)
                        .where(
                            ScenarioJob.analysis_id == analysis_id,
                            ScenarioJob.state == "queued",
                        )
                        .values(
                            state="canceled",
                            completed_at=now,
                            failure_code=None,
                            version=ScenarioJob.version + 1,
                            updated_at=now,
                        )
                    )
                await session.flush()
                return AgentCancellationRequest(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    analysis_state=job.state,
                    cancel_requested_at=requested_at,
                    agent_id=None if lease is None else lease.agent_id,
                    execution_id=None if lease is None else lease.execution_id,
                    lease_version=None if lease is None else lease.version,
                )

    async def project_unleased_cancellation(
        self,
        *,
        cancellation: AgentCancellationRequest,
        now: datetime,
    ) -> None:
        _require_aware(now)
        if (
            cancellation.analysis_state != "canceled"
            or cancellation.cancel_requested_at is None
            or cancellation.execution_id is not None
        ):
            raise AgentTaskUnavailable("Cancellation projection is unavailable")
        async with self._tenant_router.session(cancellation.team_id) as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(
                    Analysis.id == cancellation.analysis_id,
                    Analysis.tombstoned_at.is_(None),
                )
                .with_for_update()
            )
            if analysis is None:
                raise AgentTaskNotFound
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
            scenarios = tuple(
                (
                    await session.scalars(
                        select(ScenarioResult)
                        .where(ScenarioResult.analysis_id == cancellation.analysis_id)
                        .with_for_update()
                    )
                ).all()
            )
            for scenario in scenarios:
                if scenario.state in ("completed", "failed", "canceled"):
                    continue
                scenario.state = "canceled"
                scenario.completed_at = now
                scenario.failure_code = None
                scenario.version += 1
                scenario.updated_at = now
            await session.flush()

    async def authorize_cancellation(
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
            scenario_rows = (
                ()
                if row is None
                else tuple(
                    (
                        await session.scalars(
                            select(ScenarioJob).where(ScenarioJob.analysis_id == row[1].id)
                        )
                    ).all()
                )
            )
        if row is None:
            raise AgentTaskNotFound
        lease, job = row
        if lease.agent_id != agent_id:
            raise AgentTaskNotFound
        if lease.version != lease_version:
            raise StaleLeaseVersion
        if lease.state == "cancel_requested":
            if job.cancel_requested_at is None:
                raise AgentTaskNotFound
        elif lease.state == "released":
            if lease.cancel_acknowledged_at is None or job.state != "canceled":
                raise AgentTaskNotFound
        else:
            raise AgentTaskNotFound
        return AgentExecutionAccess(
            team_id=job.team_id,
            analysis_id=job.id,
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=lease.expires_at,
            allowed_uploads=_allowed_uploads(_control_scenario_types(scenario_rows)),
            scenario_types=_control_scenario_types(scenario_rows),
            input_artifact_ids=(() if job.input_artifact_id is None else (job.input_artifact_id,)),
        )

    async def acknowledge_cancellation(
        self,
        *,
        access: AgentExecutionAccess,
        now: datetime,
    ) -> AgentCancellationAcknowledgement:
        _require_aware(now)
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
                    raise AgentTaskNotFound
                lease, job, device = row
                if (
                    lease.agent_id != access.agent_id
                    or lease.version != access.lease_version
                    or job.id != access.analysis_id
                    or job.team_id != access.team_id
                ):
                    raise AgentTaskNotFound
                if lease.state == "released" and lease.cancel_acknowledged_at is not None:
                    if job.state != "canceled":
                        raise AgentTaskUnavailable("Cancellation state is unavailable")
                    return AgentCancellationAcknowledgement(
                        execution_id=access.execution_id,
                        analysis_id=access.analysis_id,
                        lease_version=access.lease_version,
                        acknowledged_at=lease.cancel_acknowledged_at,
                    )
                if lease.state != "cancel_requested" or job.cancel_requested_at is None:
                    raise AgentTaskNotFound

                lease.state = "released"
                lease.released_at = now
                lease.cancel_acknowledged_at = now
                lease.updated_at = now
                newer_lease = await session.scalar(
                    select(AgentLease.id)
                    .where(
                        AgentLease.device_id == device.id,
                        AgentLease.id != lease.id,
                        AgentLease.state.in_(("active", "cancel_requested")),
                    )
                    .limit(1)
                )
                if newer_lease is None:
                    device.state = "ready"
                    device.version += 1
                    device.updated_at = now
                job.state = "canceled"
                job.completed_at = now
                job.failure_code = None
                job.version += 1
                job.updated_at = now
                await session.execute(
                    update(ScenarioJob)
                    .where(
                        ScenarioJob.analysis_id == access.analysis_id,
                        ScenarioJob.state.in_(("queued", "scheduled", "running", "analyzing")),
                    )
                    .values(
                        state="canceled",
                        completed_at=now,
                        failure_code=None,
                        version=ScenarioJob.version + 1,
                        updated_at=now,
                    )
                )
                event_id = agent_execution_canceled_event_id(access.execution_id)
                if await session.get(OutboxEvent, event_id) is None:
                    session.add(
                        OutboxEvent(
                            id=event_id,
                            team_id=access.team_id,
                            global_job_id=access.analysis_id,
                            scenario_job_id=None,
                            event_type="agent_execution_canceled",
                            subject_type="agent_execution",
                            subject_id=access.execution_id,
                            subject_version=access.lease_version,
                            ready_at=now,
                            published_at=None,
                            dead_lettered_at=None,
                            retry_count=0,
                            version=1,
                        )
                    )
                await session.flush()
                return AgentCancellationAcknowledgement(
                    execution_id=access.execution_id,
                    analysis_id=access.analysis_id,
                    lease_version=access.lease_version,
                    acknowledged_at=now,
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
            scenario_rows = (
                ()
                if row is None
                else tuple(
                    (
                        await session.scalars(
                            select(ScenarioJob).where(ScenarioJob.analysis_id == row[1].id)
                        )
                    ).all()
                )
            )
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
            allowed_uploads=_allowed_uploads(_control_scenario_types(scenario_rows)),
            scenario_types=_control_scenario_types(scenario_rows),
            input_artifact_ids=(() if job.input_artifact_id is None else (job.input_artifact_id,)),
        )

    async def authorize_completion(
        self,
        *,
        agent_id: UUID,
        execution_id: UUID,
        lease_version: int,
        manifest_digest: str,
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
            scenario_rows = (
                ()
                if row is None
                else tuple(
                    (
                        await session.scalars(
                            select(ScenarioJob).where(ScenarioJob.analysis_id == row[1].id)
                        )
                    ).all()
                )
            )
        if row is None:
            raise AgentTaskNotFound
        lease, job = row
        if lease.agent_id != agent_id:
            raise AgentTaskNotFound
        if lease.version != lease_version:
            raise StaleLeaseVersion
        if lease.state == "active":
            if lease.expires_at <= now or job.state not in ("scheduled", "running"):
                raise AgentTaskNotFound
        elif lease.state == "released":
            if (
                lease.completion_manifest_digest is None
                or not secrets.compare_digest(
                    lease.completion_manifest_digest,
                    manifest_digest,
                )
                or job.state not in ("analyzing", "failed")
            ):
                raise AgentTaskConflict("Execution was already completed with another manifest")
        else:
            raise AgentTaskNotFound
        return AgentExecutionAccess(
            team_id=job.team_id,
            analysis_id=job.id,
            agent_id=agent_id,
            execution_id=execution_id,
            lease_version=lease_version,
            lease_expires_at=lease.expires_at,
            allowed_uploads=_allowed_uploads(_control_scenario_types(scenario_rows)),
            scenario_types=_control_scenario_types(scenario_rows),
            input_artifact_ids=(() if job.input_artifact_id is None else (job.input_artifact_id,)),
        )

    async def complete_execution(
        self,
        *,
        access: AgentExecutionAccess,
        manifest: ValidatedAgentExecutionManifest,
        now: datetime,
    ) -> AgentExecutionCompletion:
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
                    raise AgentTaskNotFound
                lease, job, device = row
                if (
                    lease.agent_id != access.agent_id
                    or job.id != access.analysis_id
                    or job.team_id != access.team_id
                    or lease.version != access.lease_version
                ):
                    raise AgentTaskNotFound
                if lease.state == "released":
                    if (
                        lease.completion_manifest_digest is None
                        or not secrets.compare_digest(
                            lease.completion_manifest_digest,
                            manifest.document_hash,
                        )
                        or lease.released_at is None
                        or job.state not in ("analyzing", "failed")
                    ):
                        raise AgentTaskConflict(
                            "Execution was already completed with another manifest"
                        )
                    return AgentExecutionCompletion(
                        execution_id=access.execution_id,
                        analysis_id=access.analysis_id,
                        lease_version=access.lease_version,
                        analysis_state=cast(Literal["analyzing", "failed"], job.state),
                        accepted_at=lease.released_at,
                    )
                if lease.state != "active" or lease.expires_at <= now:
                    raise AgentTaskNotFound

                scenario_rows = tuple(
                    (
                        await session.scalars(
                            select(ScenarioJob)
                            .where(ScenarioJob.analysis_id == access.analysis_id)
                            .with_for_update()
                        )
                    ).all()
                )
                if _control_scenario_types(scenario_rows) != access.scenario_types:
                    raise AgentTaskUnavailable("Execution scenarios are unavailable")
                by_scenario = {
                    _agent_scenario_type(row.scenario_type): row for row in scenario_rows
                }
                if set(by_scenario) != {item.scenario_type for item in manifest.scenarios}:
                    raise AgentTaskConflict("Execution scenarios do not match")

                lease.state = "released"
                lease.released_at = now
                lease.completion_manifest_digest = manifest.document_hash
                lease.updated_at = now
                other_lease = await session.scalar(
                    select(AgentLease.id)
                    .where(
                        AgentLease.device_id == device.id,
                        AgentLease.id != lease.id,
                        AgentLease.state.in_(("active", "cancel_requested")),
                    )
                    .limit(1)
                )
                if other_lease is None:
                    device.state = "ready"
                    device.version += 1
                    device.updated_at = now

                if manifest.state == "failed":
                    job.state = "failed"
                    job.completed_at = now
                    job.failure_code = manifest.diagnostic_code or "agent_execution_failed"
                else:
                    job.state = "analyzing"
                    job.completed_at = None
                    job.failure_code = None
                job.version += 1
                job.updated_at = now

                manifest_scenarios = {item.scenario_type: item for item in manifest.scenarios}
                for scenario_type, scenario_job in by_scenario.items():
                    observed = manifest_scenarios[scenario_type]
                    if manifest.state == "completed" and observed.state == "completed":
                        scenario_job.state = "analyzing"
                        scenario_job.failure_code = None
                        scenario_job.completed_at = None
                    else:
                        scenario_job.state = "failed"
                        scenario_job.failure_code = (
                            observed.diagnostic_code
                            or manifest.diagnostic_code
                            or "agent_scenario_failed"
                        )
                        scenario_job.completed_at = now
                    scenario_job.started_at = scenario_job.started_at or observed.started_at
                    scenario_job.version += 1
                    scenario_job.updated_at = now

                completion_event_id = agent_execution_completed_event_id(access.execution_id)
                if await session.get(OutboxEvent, completion_event_id) is None:
                    session.add(
                        OutboxEvent(
                            id=completion_event_id,
                            team_id=access.team_id,
                            global_job_id=access.analysis_id,
                            scenario_job_id=None,
                            event_type="agent_execution_completed",
                            subject_type="agent_execution",
                            subject_id=access.execution_id,
                            subject_version=access.lease_version,
                            ready_at=now,
                            published_at=None,
                            dead_lettered_at=None,
                            retry_count=0,
                            version=1,
                        )
                    )
                if manifest.state == "completed" and any(
                    item.kind in ("startup_trace", "scroll_trace") for item in manifest.artifacts
                ):
                    trace_event_id = trace_analysis_ready_event_id(access.analysis_id)
                    if await session.get(OutboxEvent, trace_event_id) is None:
                        session.add(
                            OutboxEvent(
                                id=trace_event_id,
                                team_id=access.team_id,
                                global_job_id=access.analysis_id,
                                scenario_job_id=None,
                                event_type="trace_analysis_ready",
                                subject_type="analysis",
                                subject_id=access.analysis_id,
                                subject_version=job.version,
                                ready_at=now,
                                published_at=None,
                                dead_lettered_at=None,
                                retry_count=0,
                                version=1,
                            )
                        )
                await session.flush()
                return AgentExecutionCompletion(
                    execution_id=access.execution_id,
                    analysis_id=access.analysis_id,
                    lease_version=access.lease_version,
                    analysis_state="analyzing" if manifest.state == "completed" else "failed",
                    accepted_at=now,
                )


def _task_claims(task: ActiveAgentTask, *, issued_at: datetime) -> dict[str, object]:
    definition = task.definition
    scenario_types = tuple(item.scenario_type for item in definition.scenarios)
    claims: dict[str, object] = {
        "schema_version": definition.schema_version,
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
        "allowed_uploads": list(_allowed_uploads(scenario_types)),
    }
    if definition.schema_version == "1.1":
        claims["team_id"] = str(definition.team_id)
    return claims


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


def agent_execution_completed_event_id(execution_id: UUID) -> UUID:
    return uuid5(_EVENT_NAMESPACE, f"agent_execution_completed:{execution_id}")


def agent_execution_canceled_event_id(execution_id: UUID) -> UUID:
    return uuid5(_EVENT_NAMESPACE, f"agent_execution_canceled:{execution_id}")


def _agent_scenario_type(value: str) -> TaskScenarioType:
    if value == "cold_start":
        return "startup"
    if value in ("scroll", "memory_cycle"):
        return cast(TaskScenarioType, value)
    raise AgentTaskUnavailable("Execution scenarios are unavailable")


def _control_scenario_types(rows: Sequence[ScenarioJob]) -> tuple[TaskScenarioType, ...]:
    observed = {_agent_scenario_type(row.scenario_type) for row in rows}
    return tuple(
        scenario_type
        for scenario_type in ("startup", "scroll", "memory_cycle")
        if scenario_type in observed
    )  # type: ignore[return-value]


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
    "AgentCancellationAcknowledgement",
    "AgentCancellationRequest",
    "AgentExecutionAccess",
    "AgentExecutionArtifact",
    "AgentExecutionCompletion",
    "AgentExecutionScenario",
    "AgentTaskDefinition",
    "AgentTaskCancellation",
    "AgentTaskDelivery",
    "AgentTaskConflict",
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
    "ValidatedAgentExecutionManifest",
    "agent_execution_completed_event_id",
    "agent_execution_canceled_event_id",
    "validate_agent_execution_manifest",
]
