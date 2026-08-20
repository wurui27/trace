from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol, TypeVar
from uuid import UUID

from perfpilot_api.services.agent_tasks import (
    AgentExecutionAccess,
    AgentTaskDefinition,
    TaskInputArtifact,
    TaskScenario,
    ValidatedAgentExecutionManifest,
    validate_agent_execution_manifest,
)


T = TypeVar("T")


class _LocalAnalysis(Protocol):
    pass


class _LocalUpload(Protocol):
    pass


class RemoteCaptureRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteCaptureContext:
    team_id: UUID
    analysis_id: UUID
    generation: int

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("remote capture context rejected")

    @property
    def key(self) -> tuple[UUID, UUID]:
        return (self.team_id, self.analysis_id)


class RemoteCaptureCoordinator:
    def __init__(self) -> None:
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}

    def _lock(self, context: RemoteCaptureContext) -> asyncio.Lock:
        return self._locks.setdefault(context.key, asyncio.Lock())

    async def _run(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool] | None,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        async with self._lock(context):
            if guard is not None and not guard():
                raise RemoteCaptureRejected("remote capture rejected")
            return await operation()

    async def finalize(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=guard, operation=operation)

    async def reconcile(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=guard, operation=operation)

    async def cancel(
        self,
        context: RemoteCaptureContext,
        *,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=None, operation=operation)

    async def accept_completion(
        self,
        context: RemoteCaptureContext,
        *,
        guard: Callable[[], bool],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self._run(context, guard=guard, operation=operation)

    def discard(self, context: RemoteCaptureContext) -> None:
        lock = self._locks.get(context.key)
        if lock is not None and not lock.locked():
            self._locks.pop(context.key, None)


def restore_remote_capture(
    *,
    team_id: UUID,
    analysis_id: UUID,
    agent_id: UUID,
    capture_test_type: object,
    value: Mapping[str, object],
    now: datetime,
) -> tuple[AgentExecutionAccess, ValidatedAgentExecutionManifest]:
    scenario_types = (
        ("scroll",)
        if capture_test_type == "scroll"
        else ("startup",)
        if capture_test_type is not None
        else ("startup", "scroll")
    )
    allowed_uploads = tuple(
        f"{scenario_type}_trace" for scenario_type in scenario_types
    ) + ("agent_log",)
    try:
        execution_id = UUID(str(value["execution_id"]))
        lease_version = int(value["lease_version"])
        manifest_document = {
            key: item for key, item in value.items() if key != "accepted_at"
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("remote capture manifest rejected") from None
    manifest = validate_agent_execution_manifest(
        manifest_document,
        execution_id=execution_id,
        lease_version=lease_version,
        expected_scenarios=scenario_types,
        allowed_uploads=allowed_uploads,
        now=now,
    )
    access = AgentExecutionAccess(
        team_id=team_id,
        analysis_id=analysis_id,
        agent_id=agent_id,
        execution_id=execution_id,
        lease_version=lease_version,
        lease_expires_at=now + timedelta(minutes=1),
        allowed_uploads=allowed_uploads,
        scenario_types=scenario_types,
    )
    return access, manifest


def remote_device_definition(
    analysis: _LocalAnalysis,
    upload: _LocalUpload,
) -> AgentTaskDefinition:
    metadata = analysis.application_metadata
    target = analysis.inputs.get("apk")
    if (
        metadata is None
        or target is None
        or target.artifact_id is None
        or analysis.device_agent_id is None
        or analysis.device_id is None
        or analysis.device_digest is None
    ):
        raise RuntimeError("Remote capture publication is incomplete")
    package_name = metadata.get("package_name")
    launch_activity = metadata.get("launch_activity")
    if not isinstance(package_name, str) or not isinstance(launch_activity, str):
        raise RuntimeError("Remote capture publication is incomplete")
    return AgentTaskDefinition(
        analysis_id=analysis.analysis_id,
        team_id=analysis.team_id,
        agent_id=analysis.device_agent_id,
        device_id=analysis.device_id,
        device_digest=analysis.device_digest,
        package_name=package_name,
        launch_activity=launch_activity,
        cleanup_policy="uninstall",
        input_artifacts=(
            TaskInputArtifact(
                artifact_id=UUID(target.artifact_id),
                kind="apk",
                mime=upload.mime,
                size=upload.size,
                sha256_b64=upload.sha256_b64,
            ),
        ),
        scenarios=(
            TaskScenario(
                scenario_type="startup",
                recipe_version=1,
                recipe_hash=hashlib.sha256(b"startup-v1:15").hexdigest(),
                duration_seconds=15,
                memory_rounds=0,
                swipe_count=0,
            ),
            TaskScenario(
                scenario_type="scroll",
                recipe_version=1,
                recipe_hash=hashlib.sha256(b"scroll-v1:30:3").hexdigest(),
                duration_seconds=30,
                memory_rounds=0,
                swipe_count=3,
            ),
        ),
        schema_version="1.1",
    )

def script_device_definition(
    analysis: _LocalAnalysis,
) -> AgentTaskDefinition:
    configuration = analysis.capture_configuration
    if (
        configuration is None
        or analysis.device_agent_id is None
        or analysis.device_id is None
        or analysis.device_digest is None
    ):
        raise RuntimeError("Script capture publication is incomplete")
    test_type = configuration.get("test_type")
    launch_mode = configuration.get("launch_mode")
    duration = configuration.get("duration_seconds")
    package_name = configuration.get("package_name")
    launch_activity = configuration.get("launch_activity")
    if (
        test_type not in {"cold_start", "hot_start", "scroll"}
        or launch_mode not in {"automatic", "manual"}
        or type(duration) is not int
        or not 1 <= duration <= 300
        or (package_name is not None and not isinstance(package_name, str))
        or (launch_activity is not None and not isinstance(launch_activity, str))
    ):
        raise RuntimeError("Script capture publication is incomplete")
    scenario_type = "scroll" if test_type == "scroll" else "startup"
    return AgentTaskDefinition(
        analysis_id=analysis.analysis_id,
        team_id=analysis.team_id,
        agent_id=analysis.device_agent_id,
        device_id=analysis.device_id,
        device_digest=analysis.device_digest,
        package_name=package_name,
        launch_activity=launch_activity,
        cleanup_policy="keep_installed",
        input_artifacts=(),
        scenarios=(
            TaskScenario(
                scenario_type=scenario_type,
                recipe_version=1,
                recipe_hash=hashlib.sha256(
                    f"script-{test_type}-v1:{duration}".encode("ascii")
                ).hexdigest(),
                duration_seconds=duration,
                memory_rounds=0,
                swipe_count=0,
            ),
        ),
        schema_version="1.2",
        test_type=test_type,
        launch_mode=launch_mode,
    )



__all__ = [
    "remote_device_definition",
    "script_device_definition",
    "RemoteCaptureContext",
    "RemoteCaptureCoordinator",
    "RemoteCaptureRejected",
    "restore_remote_capture",
]
