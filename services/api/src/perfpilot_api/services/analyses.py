from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, TypeVar
from uuid import UUID, uuid4, uuid5

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from perfpilot_api.db.control.models import (
    Device,
    AgentLease,
    EngineExecution,
    GlobalJob,
    IdempotencyKey,
    OutboxEvent,
    ScenarioJob,
    SourceTask,
    SynthesisExecution,
    Team,
    TenantQuota,
)
from perfpilot_api.db.tenant.models import (
    Analysis,
    Application,
    ApplicationVersion,
    Artifact,
    ReportVersion,
    SampleAttempt,
    ScenarioRecipe,
    ScenarioResult,
)
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.domain.states import ANALYSIS_TERMINAL_STATES, AnalysisState
from perfpilot_api.domain.transitions import (
    InvalidAggregateState,
    InvalidTransition,
    derive_parent_state,
    transition,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
    SynthesisExecutionNotFoundError,
    SynthesisIdempotencyConflictError,
    SynthesisRequest,
)
from perfpilot_api.services.uploads import (
    UploadError,
    UploadIdempotencyConflictError,
    UploadInvalidRequestError,
    UploadNotFoundError,
    UploadService,
    UploadSlot,
)
from perfpilot_api.services.source_workspaces import SourceBinding

_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_APK_MIME = "application/vnd.android.package-archive"
_SCENARIOS = ("cold_start", "scroll", "memory_cycle")
_QUEUE_RESERVATION_STATES = ("creating", "created", "uploading", "queued")
_ACTIVE_ANALYSIS_STATES = (
    "creating",
    "created",
    "uploading",
    "queued",
    "scheduled",
    "running",
    "analyzing",
)
_CREATE_OPERATION = "create_analysis"
_CREATE_IDEMPOTENCY_TTL = timedelta(days=30)
_APK_INSPECTION_CLAIM_TTL = timedelta(minutes=5)
_CHILD_NAMESPACE = UUID("0b24a12a-8f5d-4bb8-a8bc-71d6ddb750ca")
_EVENT_NAMESPACE = UUID("d80deae5-50f5-4a35-a92b-f75f5c54e832")
_APPLICATION_NAMESPACE = UUID("115a38fd-705d-4aa8-9674-d6af5b19aa0d")
_APPLICATION_VERSION_NAMESPACE = UUID("d057f159-35a6-42fb-b319-b3415bfb1b63")
_RECIPE_NAMESPACE = UUID("6af10575-b7ae-443e-a485-4477573236a3")
_PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_COMPONENT_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z")
_MANIFEST_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_SUPPORTED_ABIS = frozenset(("armeabi-v7a", "arm64-v8a", "x86", "x86_64"))
_TRACE_INPUT_ORDER = (
    "trace",
    "memory_evidence",
    "apk",
    "source_archive",
    "mapping",
    "native_symbols",
    "log",
)
_TRACE_INPUT_KINDS = frozenset(_TRACE_INPUT_ORDER)
_MIME_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_REPORT_CONTRACT_ROOT = Path(__file__).resolve().parents[5] / "contracts" / "v1" / "reports"
# RFC 6750 permits very short b64tokens; eight characters distinguishes credentials from
# ordinary prose such as "the bearer of bad news" while remaining fail-closed for real tokens.
_BEARER_CREDENTIAL_MIN_LENGTH = 8
_PRIVATE_REPORT_VALUE = re.compile(
    r"(?i)(?:\b(?:postgres(?:ql)?|mysql|redis|rediss|mongodb(?:\+srv)?|s3|gs|file)://|"
    r"x-amz-(?:signature|credential|security-token)=|"
    r"awsaccesskeyid=|"
    rf"\bbearer[ \t]+[-A-Za-z0-9._~+/]{{{_BEARER_CREDENTIAL_MIN_LENGTH},}}=*"
    r"(?=[^-A-Za-z0-9._~+/=]|\Z)|"
    r"(?:authorization|access[_-]?token|password|secret|credential|signature)=)"
)
_T = TypeVar("_T")


class AnalysisError(RuntimeError):
    def __init__(self, message: str = "analysis operation failed") -> None:
        super().__init__(message)


class AnalysisInvalidRequestError(AnalysisError):
    pass


class AnalysisNotFoundError(AnalysisError):
    pass


class AnalysisIdempotencyConflictError(AnalysisError):
    pass


class AnalysisQueueLimitError(AnalysisError):
    pass


class AnalysisDeviceUnavailableError(AnalysisError):
    pass


class StaleTaskVersionError(AnalysisError):
    pass


class ReportNotAvailableError(AnalysisError):
    pass


class AnalysisUnavailableError(AnalysisError):
    pass


class ApkInspectionError(AnalysisError):
    def __init__(
        self,
        message: str = "APK metadata is invalid",
        *,
        code: str = "apk_invalid",
    ) -> None:
        super().__init__(message)
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) else "apk_invalid"


class ApkInspectionUnavailableError(AnalysisError):
    pass


@dataclass(frozen=True, slots=True)
class ApplicationMetadataView:
    package_name: str
    version_name: str | None
    version_code: int
    launch_activity: str | None
    min_sdk: int | None
    target_sdk: int | None
    supported_abis: tuple[str, ...]
    has_native_libraries: bool


@dataclass(frozen=True, slots=True)
class InspectedApkMetadata:
    package_name: str
    version_name: str | None
    version_code: int
    launch_activity: str | None
    min_sdk: int | None
    target_sdk: int | None
    supported_abis: tuple[str, ...]
    has_native_libraries: bool
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedScenario:
    scenario_job_id: UUID
    scenario_type: Literal["cold_start", "scroll", "memory_cycle"]
    scenario_recipe_id: UUID
    recipe_version: int
    recipe_hash: str
    recipe_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class SchedulingRequirements:
    min_api_level: int | None
    supported_abis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalizationPreparation:
    requirements: SchedulingRequirements | None
    inspection_token: UUID | None


@dataclass(frozen=True, slots=True)
class ScenarioView:
    scenario_job_id: UUID | None
    scenario_type: Literal["cold_start", "scroll", "memory_cycle"]
    state: str
    version: int | None
    device_group_id: UUID | None
    sample_verdict_counts: SampleVerdictCounts
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class SampleVerdictCounts:
    valid: int
    invalid: int
    pending: int
    validation_error: int
    total: int


@dataclass(frozen=True, slots=True)
class ActiveLeaseView:
    lease_id: UUID
    device_id: UUID
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class InputUploadView:
    state: Literal["awaiting_upload", "pending", "finalized"]
    artifact_kind: str
    mime: str
    size: int
    sha256_b64: str
    upload_id: UUID | None
    artifact_id: UUID | None
    expires_at: datetime | None
    finalized_at: datetime | None


AnalysisStageName = Literal["input_validation", "smartperfetto", "perfpilot_ai", "report"]
AnalysisStageState = Literal[
    "pending", "running", "completed", "failed", "canceled", "not_requested"
]


@dataclass(frozen=True, slots=True)
class AnalysisStageView:
    stage: AnalysisStageName
    state: AnalysisStageState
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCodeAnalysisView:
    requested: bool
    provider_kind: Literal["agent_workspace"] | None
    agent_id: UUID | None
    workspace_id: UUID | None
    snapshot_policy: Literal["tracked_worktree"] | None
    validation_profile_id: UUID | None
    context_state: Literal[
        "not_requested", "waiting_for_agent", "extracting", "available", "unavailable"
    ]
    match_summary: Literal["strong", "weak", "none"]
    verification_state: Literal[
        "not_requested",
        "pending",
        "validating",
        "verified",
        "apply_failed",
        "validation_failed",
        "source_changed",
        "not_configured",
        "timeout",
        "canceled",
        "unavailable",
    ]
    failure_code: str | None


def source_code_analysis_view(
    binding: SourceBinding | None,
    task: SourceTask | None = None,
) -> SourceCodeAnalysisView:
    if binding is None:
        return SourceCodeAnalysisView(
            requested=False,
            provider_kind=None,
            agent_id=None,
            workspace_id=None,
            snapshot_policy=None,
            validation_profile_id=None,
            context_state="not_requested",
            match_summary="none",
            verification_state="not_requested",
            failure_code=None,
        )
    context_state: Literal[
        "waiting_for_agent", "extracting", "available", "unavailable"
    ] = "waiting_for_agent"
    failure_code = None
    if task is not None:
        if task.state in {"leased", "running", "cancel_requested"}:
            context_state = "extracting"
        elif task.state == "completed" and task.completion_artifact_id is not None:
            context_state = "available"
        elif task.state in {"failed", "canceled", "expired"}:
            context_state = "unavailable"
            failure_code = task.failure_code or "source_agent_unavailable"
    return SourceCodeAnalysisView(
        requested=True,
        provider_kind=binding.provider_kind,
        agent_id=binding.agent_id,
        workspace_id=binding.workspace_id,
        snapshot_policy=binding.snapshot_policy,
        validation_profile_id=binding.validation_profile_id,
        context_state=context_state,
        match_summary="none",
        verification_state="not_requested",
        failure_code=failure_code,
    )


@dataclass(frozen=True, slots=True)
class AnalysisView:
    analysis_id: UUID
    team_id: UUID
    analysis_mode: Literal["device", "trace_upload", "memory_upload"]
    state: str
    version: int
    application_version_id: UUID | None
    application_metadata: ApplicationMetadataView | None
    apk_upload: UploadSlot | None
    scenarios: tuple[ScenarioView, ...]
    sample_verdict_counts: SampleVerdictCounts
    active_lease: ActiveLeaseView | None
    report_available: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    cancel_requested_at: datetime | None = None
    device_id: UUID | None = None
    question: str | None = None
    analysis_profile: Literal["auto", "startup", "scroll"] | None = None
    input_uploads: tuple[InputUploadView, ...] = ()
    stages: tuple[AnalysisStageView, ...] = ()
    source_binding: SourceBinding | None = None
    source_code_analysis: SourceCodeAnalysisView = source_code_analysis_view(None)


MemoryAnalysisView = AnalysisView


@dataclass(frozen=True, slots=True)
class SynthesisRunConfiguration:
    normalizer_version: str
    prompt_template_version: str
    prompt_template_sha256_b64: str
    report_worker_image_digest: str
    provider_name: str
    model: str
    inference_config_hash: str


@dataclass(frozen=True, slots=True)
class SynthesisRunView:
    analysis_id: UUID
    generation: int
    state: Literal["queued"] = "queued"


@dataclass(frozen=True, slots=True)
class CreationReservation:
    analysis_id: UUID
    state: str
    version: int


class ApkInspector(Protocol):
    async def inspect(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        apk_sha256_b64: str,
    ) -> InspectedApkMetadata: ...


class AnalysisCancellationCoordinator(Protocol):
    async def request_cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> object: ...


class AnalysisRepository(Protocol):
    async def create_trace_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        analysis_profile: Literal["auto", "startup", "scroll"],
        question: str | None,
        inputs: tuple[dict[str, object], ...],
        source_binding: SourceBinding | None = None,
        now: datetime,
    ) -> AnalysisView: ...

    async def create_memory_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        application_version_id: UUID,
        question: str | None,
        now: datetime,
    ) -> MemoryAnalysisView: ...

    async def reserve_creation(
        self,
        *,
        team_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        selected_device_id: UUID,
        source_binding: SourceBinding | None = None,
        now: datetime,
    ) -> CreationReservation: ...

    async def ensure_tenant_parent(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        requested_by_user_id: UUID,
    ) -> str: ...

    async def mark_tenant_created(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> None: ...

    async def complete_creation(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        idempotency_key: str,
        request_hash: str,
        expected_version: int,
        now: datetime,
    ) -> None: ...

    async def load_view(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> AnalysisView: ...

    async def list_report_analysis_ids(
        self,
        *,
        team_id: UUID,
        limit: int,
    ) -> tuple[UUID, ...]: ...

    async def list_active_analysis_ids(
        self,
        *,
        team_id: UUID,
        limit: int,
    ) -> tuple[UUID, ...]: ...

    async def mark_trace_uploading(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> None: ...

    async def require_finalizable(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
        sha256_b64: str,
        size: int,
        now: datetime,
    ) -> FinalizationPreparation: ...

    async def release_apk_inspection(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        inspection_token: UUID,
        now: datetime,
    ) -> None: ...

    async def classify_upload(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
    ) -> Literal["initial_apk", "trace_input", "generic"]: ...

    async def trace_required_input_ready(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> bool: ...

    async def queue_trace_execution(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> None: ...

    async def stage_tenant_scenarios(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> tuple[PreparedScenario, ...]: ...

    async def persist_apk_metadata(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        apk_sha256_b64: str,
        metadata: InspectedApkMetadata,
        inspection_token: UUID,
        now: datetime,
    ) -> UUID: ...

    async def fail_apk_inspection(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        failure_code: str,
        inspection_token: UUID,
        now: datetime,
    ) -> None: ...

    async def queue_control_scenarios(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        scenarios: tuple[PreparedScenario, ...],
        requirements: SchedulingRequirements,
        now: datetime,
    ) -> None: ...

    async def load_report(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> dict[str, object]: ...


def scenario_job_id(analysis_id: UUID, scenario_type: str) -> UUID:
    if scenario_type not in _SCENARIOS:
        raise AnalysisInvalidRequestError("unknown scenario type")
    return uuid5(_CHILD_NAMESPACE, f"{analysis_id}:{scenario_type}")


def analysis_queued_event_id(analysis_id: UUID) -> UUID:
    return uuid5(_EVENT_NAMESPACE, f"analysis_queued:{analysis_id}")


def trace_analysis_ready_event_id(analysis_id: UUID) -> UUID:
    return uuid5(_EVENT_NAMESPACE, f"trace_analysis_ready:{analysis_id}")


def _prepared_scenarios_from_results(
    analysis_id: UUID,
    children: list[ScenarioResult],
) -> tuple[PreparedScenario, ...]:
    by_type = {child.scenario_type: child for child in children}
    if len(children) != 3 or len(by_type) != 3:
        raise AnalysisUnavailableError("tenant scenario projection is unavailable")
    prepared: list[PreparedScenario] = []
    for scenario_type in _SCENARIOS:
        child = by_type.get(scenario_type)
        if (
            child is None
            or child.id != scenario_job_id(analysis_id, scenario_type)
            or child.state
            not in (
                "queued",
                "scheduled",
                "running",
                "analyzing",
                "completed",
                "failed",
                "canceled",
            )
            or child.scenario_recipe_id is None
            or child.recipe_version is None
            or child.recipe_version < 1
            or child.recipe_hash is None
            or not isinstance(child.recipe_snapshot, dict)
        ):
            raise AnalysisUnavailableError("tenant scenario projection is unavailable")
        _validate_recipe(child.recipe_snapshot, scenario_type)
        if _recipe_hash(child.recipe_snapshot) != child.recipe_hash:
            raise AnalysisUnavailableError("tenant scenario projection is unavailable")
        prepared.append(
            PreparedScenario(
                scenario_job_id=child.id,
                scenario_type=scenario_type,  # type: ignore[arg-type]
                scenario_recipe_id=child.scenario_recipe_id,
                recipe_version=child.recipe_version,
                recipe_hash=child.recipe_hash,
                recipe_snapshot=_copy_recipe(child.recipe_snapshot),
            )
        )
    return tuple(prepared)


def canonical_analysis_request_hash(
    *,
    schema_version: Literal["1.0", "1.1"] = "1.0",
    source_binding: SourceBinding | None = None,
    device_id: UUID,
    scenarios: tuple[str, ...],
    apk_mime: str,
    apk_size: int,
    apk_sha256_b64: str,
) -> str:
    if schema_version == "1.0" and source_binding is not None:
        raise AnalysisInvalidRequestError("analysis request is invalid")
    document: dict[str, object] = {
        "analysis_mode": "device",
        "device_id": str(device_id),
        "apk": {
            "artifact_kind": "apk",
            "mime": apk_mime,
            "sha256_b64": apk_sha256_b64,
            "size": apk_size,
        },
        "scenarios": list(scenarios),
        "schema_version": schema_version,
    }
    if source_binding is not None:
        document["source_binding"] = _source_binding_payload(source_binding)
    payload = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def canonical_memory_analysis_request_hash(
    *,
    application_version_id: UUID,
    question: str | None,
) -> str:
    payload = json.dumps(
        {
            "analysis_mode": "memory_upload",
            "application_version_id": str(application_version_id),
            "question": question,
            "schema_version": "1.0",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _canonical_trace_inputs(
    inputs: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    if not 1 <= len(inputs) <= len(_TRACE_INPUT_ORDER):
        raise AnalysisInvalidRequestError("analysis request is invalid")
    by_kind: dict[str, dict[str, object]] = {}
    for item in inputs:
        if set(item) != {"kind", "mime", "size", "sha256_b64"}:
            raise AnalysisInvalidRequestError("analysis request is invalid")
        kind = item.get("kind")
        mime = item.get("mime")
        size = item.get("size")
        checksum = item.get("sha256_b64")
        checksum_valid = False
        if isinstance(checksum, str):
            try:
                decoded = base64.b64decode(checksum, validate=True)
                checksum_valid = (
                    len(decoded) == 32 and base64.b64encode(decoded).decode("ascii") == checksum
                )
            except (binascii.Error, ValueError):
                pass
        if (
            not isinstance(kind, str)
            or kind not in _TRACE_INPUT_KINDS
            or kind in by_kind
            or not isinstance(mime, str)
            or _MIME_TYPE.fullmatch(mime) is None
            or type(size) is not int
            or not 1 <= size <= 5 * 1024 * 1024 * 1024
            or not checksum_valid
        ):
            raise AnalysisInvalidRequestError("analysis request is invalid")
        by_kind[kind] = {
            "kind": kind,
            "mime": mime,
            "size": size,
            "sha256_b64": checksum,
        }
    if "trace" not in by_kind:
        raise AnalysisInvalidRequestError("analysis request is invalid")
    return tuple(by_kind[kind] for kind in _TRACE_INPUT_ORDER if kind in by_kind)


def canonical_trace_analysis_request_hash(
    *,
    schema_version: Literal["1.0", "1.1"] = "1.0",
    source_binding: SourceBinding | None = None,
    analysis_profile: Literal["auto", "startup", "scroll"],
    question: str | None,
    inputs: tuple[dict[str, object], ...],
) -> str:
    if schema_version == "1.0" and source_binding is not None:
        raise AnalysisInvalidRequestError("analysis request is invalid")
    document: dict[str, object] = {
        "analysis_mode": "trace_upload",
        "analysis_profile": analysis_profile,
        "inputs": list(inputs),
        "question": question,
        "schema_version": schema_version,
    }
    if source_binding is not None:
        document["source_binding"] = _source_binding_payload(source_binding)
    payload = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _source_binding_payload(binding: SourceBinding) -> dict[str, object]:
    return {
        "provider_kind": binding.provider_kind,
        "agent_id": str(binding.agent_id),
        "workspace_id": str(binding.workspace_id),
        "snapshot_policy": binding.snapshot_policy,
        "validation_profile_id": (
            None
            if binding.validation_profile_id is None
            else str(binding.validation_profile_id)
        ),
    }


def _source_binding_columns(binding: SourceBinding | None) -> dict[str, object]:
    if binding is None:
        return {
            "source_provider_kind": None,
            "source_agent_id": None,
            "source_workspace_id": None,
            "source_snapshot_policy": None,
            "source_validation_profile_id": None,
        }
    return {
        "source_provider_kind": binding.provider_kind,
        "source_agent_id": binding.agent_id,
        "source_workspace_id": binding.workspace_id,
        "source_snapshot_policy": binding.snapshot_policy,
        "source_validation_profile_id": binding.validation_profile_id,
    }


def _stored_source_binding(job: GlobalJob) -> SourceBinding | None:
    core = (
        job.source_provider_kind,
        job.source_agent_id,
        job.source_workspace_id,
        job.source_snapshot_policy,
    )
    if all(value is None for value in core):
        if job.source_validation_profile_id is not None:
            raise AnalysisUnavailableError("source binding state is unavailable")
        return None
    if (
        job.source_provider_kind != "agent_workspace"
        or job.source_agent_id is None
        or job.source_workspace_id is None
        or job.source_snapshot_policy != "tracked_worktree"
    ):
        raise AnalysisUnavailableError("source binding state is unavailable")
    return SourceBinding(
        provider_kind="agent_workspace",
        agent_id=job.source_agent_id,
        workspace_id=job.source_workspace_id,
        snapshot_policy="tracked_worktree",
        validation_profile_id=job.source_validation_profile_id,
    )


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise AnalysisUnavailableError("stored JSON is invalid") from None


def _recipe_hash(recipe: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(recipe)).hexdigest()


def _copy_recipe(recipe: dict[str, object]) -> dict[str, object]:
    copied = json.loads(_canonical_json_bytes(recipe))
    if not isinstance(copied, dict):
        raise AnalysisUnavailableError("scenario recipe is invalid")
    return copied


def _default_recipe(scenario_type: str) -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": "1.0",
        "scenario_type": scenario_type,
        "actions": [{"action": "launch"}],
    }
    if scenario_type == "scroll":
        common["scroll"] = {"direction": "up", "iterations": 5}
    elif scenario_type == "memory_cycle":
        common["memory_cycle"] = {"iterations": 10}
    return common


def _validate_recipe(recipe: dict[str, object], scenario_type: str) -> None:
    allowed_top = {"schema_version", "scenario_type", "actions"}
    if scenario_type == "scroll":
        allowed_top.add("scroll")
    elif scenario_type == "memory_cycle":
        allowed_top.add("memory_cycle")
    if (
        set(recipe) != allowed_top
        or recipe.get("schema_version") != "1.0"
        or recipe.get("scenario_type") != scenario_type
        or not isinstance(recipe.get("actions"), list)
        or not recipe["actions"]
        or len(recipe["actions"]) > 64
    ):
        raise AnalysisUnavailableError("scenario recipe is invalid")
    for action in recipe["actions"]:
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise AnalysisUnavailableError("scenario recipe is invalid")
        kind = action["action"]
        expected_keys: set[str]
        if kind == "launch":
            expected_keys = {"action"}
        elif kind in ("tap_text", "wait_text"):
            expected_keys = {"action", "text"}
            text_value = action.get("text")
            if not isinstance(text_value, str) or not 1 <= len(text_value) <= 200:
                raise AnalysisUnavailableError("scenario recipe is invalid")
        elif kind == "tap_ratio":
            expected_keys = {"action", "x", "y"}
            if any(
                type(action.get(axis)) not in (int, float) or not 0 <= action[axis] <= 1
                for axis in ("x", "y")
            ):
                raise AnalysisUnavailableError("scenario recipe is invalid")
        elif kind == "keyevent":
            expected_keys = {"action", "keycode"}
            if action.get("keycode") not in ("BACK", "HOME", "ENTER", "ESCAPE"):
                raise AnalysisUnavailableError("scenario recipe is invalid")
        else:
            raise AnalysisUnavailableError("scenario recipe is invalid")
        if set(action) != expected_keys:
            raise AnalysisUnavailableError("scenario recipe is invalid")
    if scenario_type == "scroll":
        scroll = recipe.get("scroll")
        if (
            not isinstance(scroll, dict)
            or set(scroll) != {"direction", "iterations"}
            or scroll.get("direction") not in ("up", "down", "left", "right")
            or type(scroll.get("iterations")) is not int
            or not 1 <= scroll["iterations"] <= 20
        ):
            raise AnalysisUnavailableError("scenario recipe is invalid")
    if scenario_type == "memory_cycle":
        cycle = recipe.get("memory_cycle")
        if (
            not isinstance(cycle, dict)
            or set(cycle) != {"iterations"}
            or type(cycle.get("iterations")) is not int
            or not 1 <= cycle["iterations"] <= 20
        ):
            raise AnalysisUnavailableError("scenario recipe is invalid")


def _bundle_sha256_b64(bundle: dict[str, object]) -> str:
    return base64.b64encode(hashlib.sha256(_canonical_json_bytes(bundle)).digest()).decode("ascii")


@lru_cache(maxsize=2)
def _report_contract_validator(schema_name: str) -> Draft202012Validator:
    if schema_name not in ("analysis-bundle.schema.json", "analysis-report.schema.json"):
        raise AnalysisUnavailableError("analysis report contract is unavailable")
    try:
        schema = json.loads((_REPORT_CONTRACT_ROOT / schema_name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        raise AnalysisUnavailableError("analysis report contract is unavailable") from None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_report_contract(schema_name: str, value: object) -> None:
    try:
        _report_contract_validator(schema_name).validate(value)
    except ValidationError:
        raise AnalysisUnavailableError("analysis report contract is invalid") from None


def _load_trace_report_from_versions(
    *,
    analysis_id: UUID,
    versions: list[ReportVersion],
    analysis_mode: Literal["trace_upload", "device"] = "trace_upload",
) -> dict[str, object] | None:
    if analysis_mode not in {"trace_upload", "device"}:
        raise AnalysisUnavailableError("analysis report identity is invalid")
    for version in sorted(
        versions,
        key=lambda item: item.report_version,
        reverse=True,
    ):
        if version.analysis_id != analysis_id or version.scenario_result_id is not None:
            raise AnalysisUnavailableError("analysis report identity is invalid")
        if version.report is None:
            if (
                version.report_sha256_b64 is not None
                or version.bundle is not None
                or version.bundle_sha256_b64 is not None
            ):
                raise AnalysisUnavailableError("analysis report metadata is invalid")
            continue
        if (
            version.report_sha256_b64 is None
            or version.bundle is not None
            or version.bundle_sha256_b64 is not None
            or not hmac.compare_digest(
                _bundle_sha256_b64(version.report),
                version.report_sha256_b64,
            )
        ):
            raise AnalysisUnavailableError("analysis report checksum is invalid")
        candidate = _copy_public_json(version.report)
        if not isinstance(candidate, dict):
            raise AnalysisUnavailableError("analysis report is invalid")
        _validate_report_contract("analysis-report.schema.json", candidate)
        expected_row_state = {
            "completed": "complete",
            "partially_completed": "partial",
            "failed": "failed",
        }.get(candidate.get("state"))
        if (
            candidate.get("schema_version") != "1.1"
            or candidate.get("analysis_id") != str(analysis_id)
            or candidate.get("analysis_mode") != analysis_mode
            or candidate.get("report_version") != version.report_version
            or expected_row_state is None
            or version.state != expected_row_state
        ):
            raise AnalysisUnavailableError("analysis report identity is invalid")
        return candidate
    return None


def _stage(
    name: AnalysisStageName,
    state: AnalysisStageState,
    failure_code: str | None = None,
) -> AnalysisStageView:
    if state == "failed":
        code = failure_code or f"{name}_failed"
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) is None:
            code = f"{name}_failed"
        return AnalysisStageView(name, state, code)
    return AnalysisStageView(name, state, None)


def _trace_stages(
    *,
    job: GlobalJob,
    engine: EngineExecution | None,
    synthesis: SynthesisExecution | None,
    report_available: bool,
) -> tuple[AnalysisStageView, ...]:
    if engine is not None or job.state in {
        "queued",
        "scheduled",
        "running",
        "analyzing",
        "completed",
        "partially_completed",
    }:
        input_stage = _stage("input_validation", "completed")
    elif job.state == "uploading":
        input_stage = _stage("input_validation", "running")
    elif job.state == "failed":
        input_stage = _stage("input_validation", "failed", job.failure_code)
    elif job.state == "canceled":
        input_stage = _stage("input_validation", "canceled")
    else:
        input_stage = _stage("input_validation", "pending")

    if engine is None:
        if job.state == "failed" and input_stage.state == "completed":
            engine_stage = _stage("smartperfetto", "failed", job.failure_code)
        elif job.state == "canceled" and input_stage.state == "completed":
            engine_stage = _stage("smartperfetto", "canceled")
        else:
            engine_stage = _stage("smartperfetto", "pending")
    elif engine.state in {"completed", "insufficient_data"}:
        engine_stage = _stage("smartperfetto", "completed")
    elif engine.state in {"running", "awaiting_user"}:
        engine_stage = _stage("smartperfetto", "running")
    elif engine.state == "failed":
        engine_stage = _stage("smartperfetto", "failed", engine.stable_error_code)
    elif engine.state == "canceled":
        engine_stage = _stage("smartperfetto", "canceled")
    else:
        engine_stage = _stage("smartperfetto", "pending")

    if synthesis is None:
        if engine is not None and engine.state in {"completed", "insufficient_data"}:
            ai_state: AnalysisStageState = (
                "pending" if job.state == "analyzing" else "not_requested"
            )
        elif engine_stage.state in {"failed", "canceled"} or job.state in {
            "completed",
            "partially_completed",
            "failed",
            "canceled",
        }:
            ai_state = "not_requested"
        else:
            ai_state = "pending"
        ai_stage = _stage("perfpilot_ai", ai_state)
    elif synthesis.state == "succeeded":
        ai_stage = _stage("perfpilot_ai", "completed")
    elif synthesis.state == "failed":
        ai_stage = _stage("perfpilot_ai", "failed", synthesis.stable_error_code)
    elif synthesis.state == "canceled":
        ai_stage = _stage("perfpilot_ai", "canceled")
    elif synthesis.state == "running":
        ai_stage = _stage("perfpilot_ai", "running")
    else:
        ai_stage = _stage("perfpilot_ai", "pending")

    if report_available:
        report_stage = _stage("report", "completed")
    elif synthesis is None and ai_stage.state == "not_requested":
        report_stage = _stage("report", "not_requested")
    elif synthesis is not None and synthesis.state == "failed":
        report_stage = _stage("report", "failed", synthesis.stable_error_code)
    elif job.state == "failed":
        report_stage = _stage("report", "failed", job.failure_code)
    elif job.state == "canceled":
        report_stage = _stage("report", "canceled")
    else:
        report_stage = _stage("report", "pending")
    return input_stage, engine_stage, ai_stage, report_stage


def _validate_idempotency_key(idempotency_key: str) -> None:
    if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise AnalysisInvalidRequestError("analysis request is invalid")


def _validate_create_request(
    *,
    idempotency_key: str,
    scenarios: tuple[str, ...],
    apk_mime: str,
    apk_size: int,
    apk_sha256_b64: str,
) -> None:
    _validate_idempotency_key(idempotency_key)
    checksum_valid = False
    try:
        decoded = base64.b64decode(apk_sha256_b64, validate=True)
        checksum_valid = (
            len(decoded) == 32 and base64.b64encode(decoded).decode("ascii") == apk_sha256_b64
        )
    except (binascii.Error, ValueError):
        pass
    if (
        scenarios != _SCENARIOS
        or apk_mime != _APK_MIME
        or type(apk_size) is not int
        or not 1 <= apk_size <= 5 * 1024 * 1024 * 1024
        or not checksum_valid
    ):
        raise AnalysisInvalidRequestError("analysis request is invalid")


def _validate_inspected_apk(metadata: InspectedApkMetadata) -> None:
    if (
        _PACKAGE_NAME.fullmatch(metadata.package_name) is None
        or len(metadata.package_name) > 200
        or metadata.version_name is not None
        and len(metadata.version_name) > 255
        or type(metadata.version_code) is not int
        or not 0 <= metadata.version_code <= 2_147_483_647
        or metadata.launch_activity is not None
        and not 1 <= len(metadata.launch_activity) <= 512
        or metadata.min_sdk is not None
        and (type(metadata.min_sdk) is not int or not 1 <= metadata.min_sdk <= 2_147_483_647)
        or metadata.target_sdk is not None
        and (type(metadata.target_sdk) is not int or not 1 <= metadata.target_sdk <= 2_147_483_647)
        or len(set(metadata.supported_abis)) != len(metadata.supported_abis)
        or not set(metadata.supported_abis) <= _SUPPORTED_ABIS
        or metadata.has_native_libraries
        and not metadata.supported_abis
        or metadata.min_sdk is not None
        and metadata.target_sdk is not None
        and metadata.min_sdk > metadata.target_sdk
        or type(metadata.has_native_libraries) is not bool
        or _MANIFEST_SHA256.fullmatch(metadata.manifest_sha256) is None
    ):
        raise ApkInspectionError("APK metadata is invalid")
    if metadata.launch_activity is not None:
        activity = metadata.launch_activity
        if activity.startswith("."):
            activity = f"{metadata.package_name}{activity}"
        if len(activity) > 512 or _COMPONENT_NAME.fullmatch(activity) is None:
            raise ApkInspectionError("APK launch activity is invalid")


async def _repository_call(operation: Callable[[], Awaitable[_T]]) -> _T:
    try:
        return await operation()
    except AnalysisError:
        raise
    except Exception:
        raise AnalysisUnavailableError("analysis service is unavailable") from None


class AnalysisService:
    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        upload_service: UploadService,
        apk_inspector: ApkInspector | None = None,
        cancellation_coordinator: AnalysisCancellationCoordinator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._upload_service = upload_service
        self._apk_inspector = apk_inspector
        self._cancellation_coordinator = cancellation_coordinator
        self._clock = clock
        self._uuid_source = uuid_source

    async def create_memory_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        application_version_id: UUID,
        question: str | None,
    ) -> MemoryAnalysisView:
        _validate_idempotency_key(idempotency_key)
        normalized_question = question.strip() if question is not None else None
        if normalized_question == "":
            normalized_question = None
        if normalized_question is not None and len(normalized_question) > 2_000:
            raise AnalysisInvalidRequestError("analysis request is invalid")
        request_hash = canonical_memory_analysis_request_hash(
            application_version_id=application_version_id,
            question=normalized_question,
        )
        return await _repository_call(
            lambda: self._repository.create_memory_analysis(
                team_id=team_id,
                requested_by_user_id=requested_by_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                candidate_analysis_id=self._uuid_source(),
                application_version_id=application_version_id,
                question=normalized_question,
                now=self._clock(),
            )
        )

    async def create_trace_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        analysis_profile: str,
        question: str | None,
        inputs: tuple[dict[str, object], ...],
        schema_version: Literal["1.0", "1.1"] = "1.0",
        source_binding: SourceBinding | None = None,
    ) -> AnalysisView:
        _validate_idempotency_key(idempotency_key)
        if analysis_profile not in ("auto", "startup", "scroll"):
            raise AnalysisInvalidRequestError("analysis request is invalid")
        normalized_question = question.strip() if question is not None else None
        if normalized_question == "":
            normalized_question = None
        if normalized_question is not None and len(normalized_question) > 2_000:
            raise AnalysisInvalidRequestError("analysis request is invalid")
        canonical_inputs = _canonical_trace_inputs(inputs)
        typed_profile: Literal["auto", "startup", "scroll"] = analysis_profile  # type: ignore[assignment]
        request_hash = canonical_trace_analysis_request_hash(
            schema_version=schema_version,
            source_binding=source_binding,
            analysis_profile=typed_profile,
            question=normalized_question,
            inputs=canonical_inputs,
        )
        return await _repository_call(
            lambda: self._repository.create_trace_analysis(
                team_id=team_id,
                requested_by_user_id=requested_by_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                candidate_analysis_id=self._uuid_source(),
                analysis_profile=typed_profile,
                question=normalized_question,
                inputs=canonical_inputs,
                source_binding=source_binding,
                now=self._clock(),
            )
        )

    async def create_upload_slot(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        idempotency_key: str,
        artifact_kind: str,
        mime: str,
        size: int,
        sha256_b64: str,
    ) -> UploadSlot:
        slot = await self._upload_service.create_slot(
            team_id=team_id,
            analysis_id=analysis_id,
            idempotency_key=idempotency_key,
            artifact_kind=artifact_kind,
            mime=mime,
            size=size,
            sha256_b64=sha256_b64,
        )
        await _repository_call(
            lambda: self._repository.mark_trace_uploading(
                team_id=team_id,
                analysis_id=analysis_id,
                now=self._clock(),
            )
        )
        return slot

    async def create_device_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        device_id: UUID,
        scenarios: tuple[str, ...],
        apk_mime: str,
        apk_size: int,
        apk_sha256_b64: str,
        schema_version: Literal["1.0", "1.1"] = "1.0",
        source_binding: SourceBinding | None = None,
    ) -> AnalysisView:
        _validate_create_request(
            idempotency_key=idempotency_key,
            scenarios=scenarios,
            apk_mime=apk_mime,
            apk_size=apk_size,
            apk_sha256_b64=apk_sha256_b64,
        )
        request_hash = canonical_analysis_request_hash(
            schema_version=schema_version,
            source_binding=source_binding,
            device_id=device_id,
            scenarios=scenarios,
            apk_mime=apk_mime,
            apk_size=apk_size,
            apk_sha256_b64=apk_sha256_b64,
        )
        now = self._clock()
        reservation = await _repository_call(
            lambda: self._repository.reserve_creation(
                team_id=team_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                candidate_analysis_id=self._uuid_source(),
                selected_device_id=device_id,
                source_binding=source_binding,
                now=now,
            )
        )
        if reservation.state not in ("creating", "created", "uploading"):
            return await _repository_call(
                lambda: self._repository.load_view(
                    team_id=team_id,
                    analysis_id=reservation.analysis_id,
                    now=now,
                )
            )

        tenant_state = await _repository_call(
            lambda: self._repository.ensure_tenant_parent(
                team_id=team_id,
                analysis_id=reservation.analysis_id,
                requested_by_user_id=requested_by_user_id,
            )
        )
        if tenant_state not in ("creating", "created", "uploading"):
            return await _repository_call(
                lambda: self._repository.load_view(
                    team_id=team_id,
                    analysis_id=reservation.analysis_id,
                    now=now,
                )
            )

        try:
            slot = await self._upload_service.create_slot(
                team_id=team_id,
                analysis_id=reservation.analysis_id,
                idempotency_key="initial-apk",
                artifact_kind="apk",
                mime=apk_mime,
                size=apk_size,
                sha256_b64=apk_sha256_b64,
            )
        except UploadIdempotencyConflictError:
            raise AnalysisIdempotencyConflictError(
                "analysis upload does not match the original request"
            ) from None
        except UploadInvalidRequestError:
            raise AnalysisInvalidRequestError("analysis request is invalid") from None
        except UploadError:
            raise AnalysisUnavailableError("analysis service is unavailable") from None

        await _repository_call(
            lambda: self._repository.mark_tenant_created(
                team_id=team_id,
                analysis_id=reservation.analysis_id,
                now=now,
            )
        )
        await _repository_call(
            lambda: self._repository.complete_creation(
                team_id=team_id,
                analysis_id=reservation.analysis_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                expected_version=reservation.version,
                now=now,
            )
        )
        view = await _repository_call(
            lambda: self._repository.load_view(
                team_id=team_id,
                analysis_id=reservation.analysis_id,
                now=now,
            )
        )
        return replace(view, apk_upload=slot)

    async def get_analysis(self, *, team_id: UUID, analysis_id: UUID) -> AnalysisView:
        return await _repository_call(
            lambda: self._repository.load_view(
                team_id=team_id,
                analysis_id=analysis_id,
                now=self._clock(),
            )
        )

    async def request_cancel(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        requested_by_user_id: UUID,
    ) -> AnalysisView:
        del requested_by_user_id
        if self._cancellation_coordinator is None:
            raise AnalysisUnavailableError("analysis cancellation is unavailable")
        try:
            await self._cancellation_coordinator.request_cancel(
                team_id=team_id,
                analysis_id=analysis_id,
            )
        except AnalysisError:
            raise
        except Exception as error:
            from perfpilot_api.services.agent_tasks import AgentTaskNotFound

            if isinstance(error, AgentTaskNotFound):
                raise AnalysisNotFoundError("analysis was not found") from None
            raise AnalysisUnavailableError("analysis cancellation is unavailable") from None
        return await _repository_call(
            lambda: self._repository.load_view(
                team_id=team_id,
                analysis_id=analysis_id,
                now=self._clock(),
            )
        )

    async def list_report_analyses(
        self,
        *,
        team_id: UUID,
        limit: int,
    ) -> tuple[AnalysisView, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise AnalysisInvalidRequestError("analysis list limit is invalid")
        analysis_ids = await _repository_call(
            lambda: self._repository.list_report_analysis_ids(
                team_id=team_id,
                limit=limit,
            )
        )
        now = self._clock()
        views = tuple(
            [
                await _repository_call(
                    lambda analysis_id=analysis_id: self._repository.load_view(
                        team_id=team_id,
                        analysis_id=analysis_id,
                        now=now,
                    )
                )
                for analysis_id in analysis_ids
            ]
        )
        if any(view.team_id != team_id or not view.report_available for view in views):
            raise AnalysisUnavailableError("analysis report list is unavailable")
        return views

    async def list_active_analyses(
        self,
        *,
        team_id: UUID,
        limit: int,
    ) -> tuple[AnalysisView, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise AnalysisInvalidRequestError("active analysis list limit is invalid")
        analysis_ids = await _repository_call(
            lambda: self._repository.list_active_analysis_ids(
                team_id=team_id,
                limit=limit,
            )
        )
        now = self._clock()
        views = tuple(
            [
                await _repository_call(
                    lambda analysis_id=analysis_id: self._repository.load_view(
                        team_id=team_id,
                        analysis_id=analysis_id,
                        now=now,
                    )
                )
                for analysis_id in analysis_ids
            ]
        )
        if any(
            view.team_id != team_id or view.state not in _ACTIVE_ANALYSIS_STATES
            for view in views
        ):
            raise AnalysisUnavailableError("active analysis list is unavailable")
        return views

    async def finalize_device_upload(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
        caller_sha256_b64: str,
        caller_size: int,
    ) -> UploadSlot:
        now = self._clock()
        preparation = await _repository_call(
            lambda: self._repository.require_finalizable(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=upload_id,
                sha256_b64=caller_sha256_b64,
                size=caller_size,
                now=now,
            )
        )
        try:
            slot = await self._upload_service.finalize(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=upload_id,
                caller_sha256_b64=caller_sha256_b64,
                caller_size=caller_size,
            )
        except UploadNotFoundError:
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise AnalysisNotFoundError("analysis was not found") from None
        except UploadError:
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise
        if slot.artifact_kind != "apk" or slot.state != "finalized":
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise AnalysisInvalidRequestError("the required APK was not finalized")
        if preparation.requirements is not None:
            scenarios = await _repository_call(
                lambda: self._repository.stage_tenant_scenarios(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    artifact_id=slot.artifact_id,
                    now=now,
                )
            )
            await _repository_call(
                lambda: self._repository.queue_control_scenarios(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    artifact_id=slot.artifact_id,
                    scenarios=scenarios,
                    requirements=preparation.requirements,
                    now=now,
                )
            )
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
            )
            return slot
        if preparation.inspection_token is None:
            raise AnalysisUnavailableError("APK inspection claim is unavailable")
        if self._apk_inspector is None:
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise ApkInspectionUnavailableError("APK inspection is unavailable")
        try:
            metadata = await self._apk_inspector.inspect(
                team_id=team_id,
                analysis_id=analysis_id,
                artifact_id=slot.artifact_id,
                apk_sha256_b64=slot.sha256_b64,
            )
            _validate_inspected_apk(metadata)
        except ApkInspectionError as error:
            failure_code = error.code
            await _repository_call(
                lambda: self._repository.fail_apk_inspection(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    failure_code=failure_code,
                    inspection_token=preparation.inspection_token,
                    now=now,
                )
            )
            raise
        except ApkInspectionUnavailableError:
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise
        except Exception:
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise ApkInspectionUnavailableError("APK inspection is unavailable") from None
        try:
            await _repository_call(
                lambda: self._repository.persist_apk_metadata(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    artifact_id=slot.artifact_id,
                    apk_sha256_b64=slot.sha256_b64,
                    metadata=metadata,
                    inspection_token=preparation.inspection_token,
                    now=now,
                )
            )
        except AnalysisError:
            await self._release_apk_inspection_claim(
                team_id=team_id,
                analysis_id=analysis_id,
                inspection_token=preparation.inspection_token,
                now=now,
                best_effort=True,
            )
            raise
        scenarios = await _repository_call(
            lambda: self._repository.stage_tenant_scenarios(
                team_id=team_id,
                analysis_id=analysis_id,
                artifact_id=slot.artifact_id,
                now=now,
            )
        )
        requirements = SchedulingRequirements(
            min_api_level=metadata.min_sdk,
            supported_abis=metadata.supported_abis,
        )
        await _repository_call(
            lambda: self._repository.queue_control_scenarios(
                team_id=team_id,
                analysis_id=analysis_id,
                artifact_id=slot.artifact_id,
                scenarios=scenarios,
                requirements=requirements,
                now=now,
            )
        )
        await self._release_apk_inspection_claim(
            team_id=team_id,
            analysis_id=analysis_id,
            inspection_token=preparation.inspection_token,
            now=now,
        )
        return slot

    async def _release_apk_inspection_claim(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        inspection_token: UUID | None,
        now: datetime,
        best_effort: bool = False,
    ) -> None:
        if inspection_token is None:
            return
        try:
            await _repository_call(
                lambda: self._repository.release_apk_inspection(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    inspection_token=inspection_token,
                    now=now,
                )
            )
        except AnalysisError:
            if not best_effort:
                raise

    async def finalize_upload(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
        caller_sha256_b64: str,
        caller_size: int,
    ) -> UploadSlot:
        upload_class = await _repository_call(
            lambda: self._repository.classify_upload(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=upload_id,
            )
        )
        if upload_class == "initial_apk":
            return await self.finalize_device_upload(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=upload_id,
                caller_sha256_b64=caller_sha256_b64,
                caller_size=caller_size,
            )
        try:
            slot = await self._upload_service.finalize(
                team_id=team_id,
                analysis_id=analysis_id,
                upload_id=upload_id,
                caller_sha256_b64=caller_sha256_b64,
                caller_size=caller_size,
            )
        except UploadNotFoundError:
            raise AnalysisNotFoundError("analysis upload was not found") from None
        if upload_class == "trace_input":
            required_ready = await _repository_call(
                lambda: self._repository.trace_required_input_ready(
                    team_id=team_id,
                    analysis_id=analysis_id,
                )
            )
            if required_ready:
                await _repository_call(
                    lambda: self._repository.queue_trace_execution(
                        team_id=team_id,
                        analysis_id=analysis_id,
                        now=self._clock(),
                    )
                )
        return slot

    async def get_report(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> dict[str, object]:
        return await _repository_call(
            lambda: self._repository.load_report(
                team_id=team_id,
                analysis_id=analysis_id,
            )
        )


class SynthesisRunService:
    """Reserve an AI-only generation without changing the current public report."""

    def __init__(
        self,
        *,
        control_session_factory: Callable[[], AsyncSession],
        tenant_router: TenantRouter,
        execution_repository: SQLAlchemySynthesisExecutionRepository,
        configuration: SynthesisRunConfiguration,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._control_sessions = control_session_factory
        self._tenant_router = tenant_router
        self._executions = execution_repository
        self._configuration = configuration
        self._clock = clock

    async def create(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        idempotency_key: str,
    ) -> SynthesisRunView:
        _validate_idempotency_key(idempotency_key)
        now = self._clock()
        async with self._control_sessions() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                )
            )
            if job is None:
                raise AnalysisNotFoundError("analysis was not found")
            if job.analysis_mode not in {"trace_upload", "device"}:
                raise AnalysisInvalidRequestError("analysis does not support AI reruns")
            if AnalysisState(job.state) not in ANALYSIS_TERMINAL_STATES:
                raise AnalysisIdempotencyConflictError("analysis is not terminal")
            source = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.team_id == team_id,
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
                .order_by(EngineExecution.attempt_number.desc())
                .limit(1)
            )
            if (
                source is None
                or source.state not in {"completed", "insufficient_data"}
                or source.raw_result_artifact_id is None
                or source.normalized_report_version_id is None
            ):
                raise AnalysisIdempotencyConflictError("authoritative core report is unavailable")
            existing_key = await session.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.operation == "create_synthesis_run",
                    IdempotencyKey.scope_type == "team",
                    IdempotencyKey.scope_id == team_id,
                    IdempotencyKey.key == idempotency_key,
                )
            )
            replay: SynthesisExecution | None = None
            if existing_key is not None:
                replay = (
                    await session.get(
                        SynthesisExecution,
                        existing_key.response_resource_id,
                    )
                    if existing_key.response_resource_id is not None
                    else None
                )
                if (
                    existing_key.team_id != team_id
                    or existing_key.state != "completed"
                    or replay is None
                    or replay.team_id != team_id
                    or replay.analysis_id != analysis_id
                    or replay.source_execution_id != source.id
                ):
                    raise AnalysisIdempotencyConflictError("synthesis idempotency key changed")
                generation = replay.generation
            else:
                latest = await session.scalar(
                    select(SynthesisExecution)
                    .where(
                        SynthesisExecution.team_id == team_id,
                        SynthesisExecution.analysis_id == analysis_id,
                        SynthesisExecution.source_execution_id == source.id,
                    )
                    .order_by(SynthesisExecution.generation.desc())
                    .limit(1)
                )
                if latest is None or latest.report_version_id is None:
                    raise AnalysisIdempotencyConflictError("authoritative AI report is unavailable")
                generation = latest.generation + 1

        async with self._tenant_router.session(team_id) as session:
            if session.info.get("tenant_resource_version") != source.tenant_resource_version:
                raise AnalysisUnavailableError("tenant resource changed")
            analysis = await session.get(Analysis, analysis_id)
            artifact = await session.get(Artifact, source.raw_result_artifact_id)
            report_rows = list(
                (
                    await session.scalars(
                        select(ReportVersion)
                        .where(
                            ReportVersion.analysis_id == analysis_id,
                            ReportVersion.scenario_result_id.is_(None),
                        )
                        .order_by(ReportVersion.report_version.desc())
                    )
                ).all()
            )
            if (
                analysis is None
                or analysis.analysis_mode != job.analysis_mode
                or analysis.tombstoned_at is not None
                or artifact is None
                or artifact.analysis_id != analysis_id
                or artifact.artifact_kind != "engine_result"
                or artifact.state != "finalized"
                or artifact.version_id is None
                or artifact.deleted_at is not None
                or not isinstance(artifact.sha256_b64, str)
            ):
                raise AnalysisIdempotencyConflictError("authoritative core report is unavailable")
            report = _load_trace_report_from_versions(
                analysis_id=analysis_id,
                versions=report_rows,
                analysis_mode=job.analysis_mode,  # type: ignore[arg-type]
            )
            latest_content = next(
                (row for row in report_rows if row.report is not None),
                None,
            )
            if (
                report is None
                or latest_content is None
                or latest_content.id != source.normalized_report_version_id
            ):
                raise AnalysisIdempotencyConflictError("authoritative core report is unavailable")
            question = analysis.question
            canonical_checksum = artifact.sha256_b64

        config = self._configuration
        request = SynthesisRequest(
            canonical_sha256_b64=canonical_checksum,
            tenant_resource_version=source.tenant_resource_version,
            question=question,
            normalizer_version=config.normalizer_version,
            prompt_template_version=config.prompt_template_version,
            prompt_template_sha256_b64=config.prompt_template_sha256_b64,
            report_worker_image_digest=config.report_worker_image_digest,
            provider_name=config.provider_name,
            model=config.model,
            inference_config_hash=config.inference_config_hash,
            # The worker binds the content-derived projection checksum before invocation.
            projection_sha256_b64=canonical_checksum,
            generation=generation,
        )
        try:
            record = await self._executions.allocate(
                team_id=team_id,
                analysis_id=analysis_id,
                source_execution_id=source.id,
                request=request,
                now=now,
                mode="manual",
                idempotency_key=idempotency_key,
            )
        except SynthesisIdempotencyConflictError:
            raise AnalysisIdempotencyConflictError("synthesis idempotency key changed") from None
        except SynthesisExecutionNotFoundError:
            raise AnalysisIdempotencyConflictError(
                "authoritative core report is unavailable"
            ) from None

        from perfpilot_api.workers.synthesis_orchestrator import (
            analysis_synthesis_requested_event_id,
        )

        event_id = analysis_synthesis_requested_event_id(record.id)
        async with self._control_sessions() as session:
            async with session.begin():
                event = await session.get(OutboxEvent, event_id)
                if event is None:
                    session.add(
                        OutboxEvent(
                            id=event_id,
                            team_id=team_id,
                            global_job_id=analysis_id,
                            scenario_job_id=None,
                            event_type="analysis_synthesis_requested",
                            subject_type="synthesis_execution",
                            subject_id=record.id,
                            subject_version=record.version,
                            ready_at=now,
                            published_at=None,
                            dead_lettered_at=None,
                            retry_count=0,
                            version=1,
                        )
                    )
                elif (
                    event.team_id != team_id
                    or event.global_job_id != analysis_id
                    or event.scenario_job_id is not None
                    or event.event_type != "analysis_synthesis_requested"
                    or event.subject_type != "synthesis_execution"
                    or event.subject_id != record.id
                    or event.subject_version is None
                    or event.subject_version > record.version
                    or event.dead_lettered_at is not None
                ):
                    raise AnalysisUnavailableError("synthesis event is unavailable")
        return SynthesisRunView(
            analysis_id=analysis_id,
            generation=record.generation,
        )


class SQLAlchemyAnalysisRepository:
    def __init__(
        self,
        *,
        control_session_factory: Callable[[], AsyncSession],
        tenant_router: TenantRouter,
    ) -> None:
        self._control_session_factory = control_session_factory
        self._tenant_router = tenant_router

    async def list_report_analysis_ids(
        self,
        *,
        team_id: UUID,
        limit: int,
    ) -> tuple[UUID, ...]:
        async with self._tenant_router.session(team_id) as session:
            analysis_ids = await session.scalars(
                select(Analysis.id)
                .join(ReportVersion, ReportVersion.analysis_id == Analysis.id)
                .where(
                    Analysis.tombstoned_at.is_(None),
                    Analysis.state != "deleted",
                    Analysis.analysis_mode.in_(("device", "trace_upload")),
                    ReportVersion.scenario_result_id.is_(None),
                    ReportVersion.report.is_not(None),
                )
                .group_by(Analysis.id, Analysis.created_at)
                .order_by(Analysis.created_at.desc(), Analysis.id.desc())
                .limit(limit)
            )
            return tuple(analysis_ids.all())

    async def list_active_analysis_ids(
        self,
        *,
        team_id: UUID,
        limit: int,
    ) -> tuple[UUID, ...]:
        async with self._control_session_factory() as session:
            analysis_ids = await session.scalars(
                select(GlobalJob.id)
                .where(
                    GlobalJob.team_id == team_id,
                    GlobalJob.state.in_(_ACTIVE_ANALYSIS_STATES),
                )
                .order_by(GlobalJob.created_at.desc(), GlobalJob.id.desc())
                .limit(limit)
            )
            return tuple(analysis_ids.all())

    @staticmethod
    def _reservation(
        key: IdempotencyKey,
        job: GlobalJob | None,
        *,
        team_id: UUID,
        idempotency_key: str,
        request_hash: str,
        analysis_mode: Literal["device", "trace_upload", "memory_upload"],
    ) -> CreationReservation:
        if not hmac.compare_digest(key.request_hash, request_hash):
            raise AnalysisIdempotencyConflictError(
                "idempotency key was reused with another analysis request"
            )
        if (
            job is None
            or key.response_resource_id != job.id
            or job.team_id != team_id
            or job.idempotency_key != idempotency_key
            or job.analysis_mode != analysis_mode
        ):
            raise AnalysisUnavailableError("analysis creation state is unavailable")
        return CreationReservation(
            analysis_id=job.id,
            state=job.state,
            version=job.version,
        )

    async def _find_creation(
        self,
        session: AsyncSession,
        *,
        team_id: UUID,
        idempotency_key: str,
        request_hash: str,
        analysis_mode: Literal["device", "trace_upload", "memory_upload"],
    ) -> CreationReservation | None:
        existing = await session.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.operation == _CREATE_OPERATION,
                IdempotencyKey.scope_type == "team",
                IdempotencyKey.scope_id == team_id,
                IdempotencyKey.key == idempotency_key,
            )
            .with_for_update()
        )
        if existing is None:
            return None
        job = (
            await session.get(GlobalJob, existing.response_resource_id)
            if existing.response_resource_id is not None
            else None
        )
        return self._reservation(
            existing,
            job,
            team_id=team_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            analysis_mode=analysis_mode,
        )

    async def _lock_creation_scope_and_recheck(
        self,
        session: AsyncSession,
        *,
        team_id: UUID,
        idempotency_key: str,
        request_hash: str,
        analysis_mode: Literal["device", "trace_upload", "memory_upload"],
    ) -> tuple[CreationReservation | None, TenantQuota | None]:
        locked_team_id = await session.scalar(
            select(Team.id).where(Team.id == team_id).with_for_update()
        )
        if locked_team_id is None:
            raise AnalysisUnavailableError("team state is unavailable")
        quota = None
        if analysis_mode == "device":
            quota = await session.scalar(
                select(TenantQuota).where(TenantQuota.team_id == team_id).with_for_update()
            )
            if quota is None:
                raise AnalysisUnavailableError("team quota is unavailable")
        existing = await self._find_creation(
            session,
            team_id=team_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            analysis_mode=analysis_mode,
        )
        return existing, quota

    async def _reserve_direct_creation(
        self,
        *,
        team_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        analysis_mode: Literal["trace_upload", "memory_upload"],
        source_binding: SourceBinding | None = None,
        now: datetime,
    ) -> CreationReservation:
        async with self._control_session_factory() as session:
            async with session.begin():
                existing = await self._find_creation(
                    session,
                    team_id=team_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    analysis_mode=analysis_mode,
                )
                if existing is not None:
                    return existing

                existing, _ = await self._lock_creation_scope_and_recheck(
                    session,
                    team_id=team_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    analysis_mode=analysis_mode,
                )
                if existing is not None:
                    return existing

                job = GlobalJob(
                    id=candidate_analysis_id,
                    team_id=team_id,
                    idempotency_key=idempotency_key,
                    analysis_mode=analysis_mode,
                    state="creating",
                    input_artifact_id=None,
                    required_abi=None,
                    min_api_level=None,
                    attempt_count=0,
                    valid_sample_count=0,
                    invalid_sample_count=0,
                    retry_count=0,
                    max_retries=3,
                    device_migration_allowed=False,
                    version=1,
                    **_source_binding_columns(source_binding),
                )
                key = IdempotencyKey(
                    team_id=team_id,
                    key=idempotency_key,
                    operation=_CREATE_OPERATION,
                    scope_type="team",
                    scope_id=team_id,
                    request_hash=request_hash,
                    state="pending",
                    response_resource_id=candidate_analysis_id,
                    expires_at=now + _CREATE_IDEMPOTENCY_TTL,
                    version=1,
                )
                session.add_all((job, key))
                await session.flush()
                return CreationReservation(
                    analysis_id=job.id,
                    state=job.state,
                    version=job.version,
                )

    async def _complete_direct_creation(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        idempotency_key: str,
        request_hash: str,
        expected_version: int,
        analysis_mode: Literal["trace_upload", "memory_upload"],
        now: datetime,
    ) -> None:
        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                    )
                    .with_for_update()
                )
                if (
                    job is None
                    or job.analysis_mode != analysis_mode
                    or job.idempotency_key != idempotency_key
                ):
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                if job.state == "creating":
                    if job.version != expected_version:
                        raise StaleTaskVersionError("analysis version is stale")
                    changed = await session.scalar(
                        update(GlobalJob)
                        .where(
                            GlobalJob.id == analysis_id,
                            GlobalJob.team_id == team_id,
                            GlobalJob.version == expected_version,
                            GlobalJob.state.in_(("creating",)),
                        )
                        .values(
                            state="created",
                            version=GlobalJob.version + 1,
                            updated_at=now,
                        )
                        .returning(GlobalJob.id)
                    )
                    if changed is None:
                        raise StaleTaskVersionError("analysis version is stale")

                key = await session.scalar(
                    select(IdempotencyKey)
                    .where(
                        IdempotencyKey.operation == _CREATE_OPERATION,
                        IdempotencyKey.scope_type == "team",
                        IdempotencyKey.scope_id == team_id,
                        IdempotencyKey.key == idempotency_key,
                    )
                    .with_for_update()
                )
                if (
                    key is None
                    or key.response_resource_id != analysis_id
                    or not hmac.compare_digest(key.request_hash, request_hash)
                ):
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                if key.state == "completed":
                    return
                if key.state != "pending":
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                key_changed = await session.scalar(
                    update(IdempotencyKey)
                    .where(
                        IdempotencyKey.id == key.id,
                        IdempotencyKey.version == key.version,
                        IdempotencyKey.state.in_(("pending",)),
                    )
                    .values(
                        state="completed",
                        version=IdempotencyKey.version + 1,
                        updated_at=now,
                    )
                    .returning(IdempotencyKey.id)
                )
                if key_changed is None:
                    raise StaleTaskVersionError("idempotency version is stale")

    async def create_memory_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        application_version_id: UUID,
        question: str | None,
        now: datetime,
    ) -> MemoryAnalysisView:
        async with self._tenant_router.session(team_id) as session:
            application_version = await session.scalar(
                select(ApplicationVersion)
                .join(Application, Application.id == ApplicationVersion.application_id)
                .where(ApplicationVersion.id == application_version_id)
                .with_for_update()
            )
            if application_version is None:
                raise AnalysisNotFoundError("application version was not found")

            reservation = await self._reserve_direct_creation(
                team_id=team_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                candidate_analysis_id=candidate_analysis_id,
                analysis_mode="memory_upload",
                now=now,
            )
            await session.execute(
                postgresql_insert(Analysis)
                .values(
                    id=reservation.analysis_id,
                    application_version_id=application_version_id,
                    requested_by_user_id=requested_by_user_id,
                    analysis_mode="memory_upload",
                    question=question,
                    state="created",
                    version=1,
                )
                .on_conflict_do_nothing(index_elements=(Analysis.id,))
            )
            tenant_analysis = await session.get(Analysis, reservation.analysis_id)
            if (
                tenant_analysis is None
                or tenant_analysis.analysis_mode != "memory_upload"
                or tenant_analysis.application_version_id != application_version_id
                or tenant_analysis.question != question
                or tenant_analysis.tombstoned_at is not None
                or tenant_analysis.state == "deleted"
            ):
                raise AnalysisUnavailableError("tenant analysis state is unavailable")

        await self._complete_direct_creation(
            team_id=team_id,
            analysis_id=reservation.analysis_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expected_version=reservation.version,
            analysis_mode="memory_upload",
            now=now,
        )
        return await self.load_view(
            team_id=team_id,
            analysis_id=reservation.analysis_id,
            now=now,
        )

    async def create_trace_analysis(
        self,
        *,
        team_id: UUID,
        requested_by_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        analysis_profile: Literal["auto", "startup", "scroll"],
        question: str | None,
        inputs: tuple[dict[str, object], ...],
        source_binding: SourceBinding | None = None,
        now: datetime,
    ) -> AnalysisView:
        reservation = await self._reserve_direct_creation(
            team_id=team_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            candidate_analysis_id=candidate_analysis_id,
            analysis_mode="trace_upload",
            source_binding=source_binding,
            now=now,
        )
        manifest = [dict(item) for item in inputs]
        async with self._tenant_router.session(team_id) as session:
            await session.execute(
                postgresql_insert(Analysis)
                .values(
                    id=reservation.analysis_id,
                    application_version_id=None,
                    requested_by_user_id=requested_by_user_id,
                    analysis_mode="trace_upload",
                    question=question,
                    analysis_profile=analysis_profile,
                    input_manifest=manifest,
                    state="created",
                    version=1,
                )
                .on_conflict_do_nothing(index_elements=(Analysis.id,))
            )
            tenant_analysis = await session.get(Analysis, reservation.analysis_id)
            if (
                tenant_analysis is None
                or tenant_analysis.analysis_mode != "trace_upload"
                or tenant_analysis.application_version_id is not None
                or tenant_analysis.question != question
                or tenant_analysis.analysis_profile != analysis_profile
                or tenant_analysis.input_manifest != manifest
                or tenant_analysis.tombstoned_at is not None
                or tenant_analysis.state == "deleted"
            ):
                raise AnalysisUnavailableError("tenant analysis state is unavailable")

        await self._complete_direct_creation(
            team_id=team_id,
            analysis_id=reservation.analysis_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expected_version=reservation.version,
            analysis_mode="trace_upload",
            now=now,
        )
        return await self.load_view(
            team_id=team_id,
            analysis_id=reservation.analysis_id,
            now=now,
        )

    async def mark_trace_uploading(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> None:
        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                    )
                    .with_for_update()
                )
                if job is None:
                    raise AnalysisNotFoundError("analysis was not found")
                if job.analysis_mode != "trace_upload":
                    return
                if job.state == "uploading":
                    return
                if job.state != "created":
                    raise AnalysisUnavailableError("analysis upload state is unavailable")
                changed = await session.scalar(
                    update(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                        GlobalJob.analysis_mode == "trace_upload",
                        GlobalJob.state == "created",
                        GlobalJob.version == job.version,
                    )
                    .values(
                        state="uploading",
                        version=GlobalJob.version + 1,
                        updated_at=now,
                    )
                    .returning(GlobalJob.id)
                )
                if changed is None:
                    raise StaleTaskVersionError("analysis version is stale")

    async def reserve_creation(
        self,
        *,
        team_id: UUID,
        idempotency_key: str,
        request_hash: str,
        candidate_analysis_id: UUID,
        selected_device_id: UUID,
        source_binding: SourceBinding | None = None,
        now: datetime,
    ) -> CreationReservation:
        async with self._control_session_factory() as session:
            async with session.begin():
                existing = await self._find_creation(
                    session,
                    team_id=team_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    analysis_mode="device",
                )
                if existing is not None:
                    return existing

                device = await session.scalar(
                    select(Device).where(Device.id == selected_device_id).with_for_update()
                )
                if device is None or device.team_id != team_id:
                    raise AnalysisNotFoundError("device was not found")
                if device.state != "ready":
                    raise AnalysisDeviceUnavailableError("device is unavailable")

                existing, quota = await self._lock_creation_scope_and_recheck(
                    session,
                    team_id=team_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    analysis_mode="device",
                )
                if existing is not None:
                    return existing
                if quota is None:
                    raise AnalysisUnavailableError("team quota is unavailable")

                reserved = await session.scalar(
                    select(func.count(GlobalJob.id)).where(
                        GlobalJob.team_id == team_id,
                        GlobalJob.analysis_mode == "device",
                        GlobalJob.state.in_(_QUEUE_RESERVATION_STATES),
                    )
                )
                if reserved is None or reserved >= quota.queued_device_limit:
                    raise AnalysisQueueLimitError("team queue limit reached")

                job = GlobalJob(
                    id=candidate_analysis_id,
                    team_id=team_id,
                    idempotency_key=idempotency_key,
                    analysis_mode="device",
                    state="creating",
                    selected_device_id=selected_device_id,
                    input_artifact_id=None,
                    required_abi=None,
                    min_api_level=None,
                    attempt_count=0,
                    valid_sample_count=0,
                    invalid_sample_count=0,
                    retry_count=0,
                    max_retries=3,
                    device_migration_allowed=True,
                    version=1,
                    **_source_binding_columns(source_binding),
                )
                key = IdempotencyKey(
                    team_id=team_id,
                    key=idempotency_key,
                    operation=_CREATE_OPERATION,
                    scope_type="team",
                    scope_id=team_id,
                    request_hash=request_hash,
                    state="pending",
                    response_resource_id=candidate_analysis_id,
                    expires_at=now + _CREATE_IDEMPOTENCY_TTL,
                    version=1,
                )
                session.add_all((job, key))
                await session.flush()
                return CreationReservation(
                    analysis_id=job.id,
                    state=job.state,
                    version=job.version,
                )

    async def ensure_tenant_parent(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        requested_by_user_id: UUID,
    ) -> str:
        async with self._tenant_router.session(team_id) as session:
            await session.execute(
                postgresql_insert(Analysis)
                .values(
                    id=analysis_id,
                    application_version_id=None,
                    requested_by_user_id=requested_by_user_id,
                    analysis_mode="device",
                    state="creating",
                    version=1,
                )
                .on_conflict_do_nothing(index_elements=(Analysis.id,))
            )
            row = await session.get(Analysis, analysis_id)
            if (
                row is None
                or row.analysis_mode != "device"
                or row.tombstoned_at is not None
                or row.state == "deleted"
            ):
                raise AnalysisUnavailableError("tenant analysis state is unavailable")
            return row.state

    async def mark_tenant_created(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> None:
        async with self._tenant_router.session(team_id) as session:
            row = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if row is None:
                raise AnalysisUnavailableError("tenant analysis state is unavailable")
            if row.state != "creating":
                if row.state in (
                    "created",
                    "uploading",
                    "queued",
                    "scheduled",
                    "running",
                    "analyzing",
                    "completed",
                    "partially_completed",
                ):
                    return
                raise AnalysisUnavailableError("tenant analysis state is unavailable")
            changed = await session.scalar(
                update(Analysis)
                .where(
                    Analysis.id == analysis_id,
                    Analysis.version == row.version,
                    Analysis.state.in_(("creating",)),
                )
                .values(
                    state="created",
                    version=Analysis.version + 1,
                    updated_at=now,
                )
                .returning(Analysis.id)
            )
            if changed is None:
                raise StaleTaskVersionError("tenant analysis version is stale")

    async def complete_creation(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        idempotency_key: str,
        request_hash: str,
        expected_version: int,
        now: datetime,
    ) -> None:
        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                    )
                    .with_for_update()
                )
                if job is None:
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                if job.analysis_mode != "device" or job.idempotency_key != idempotency_key:
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                if job.state == "creating":
                    if job.version != expected_version:
                        raise StaleTaskVersionError("analysis version is stale")
                    changed = await session.scalar(
                        update(GlobalJob)
                        .where(
                            GlobalJob.id == analysis_id,
                            GlobalJob.team_id == team_id,
                            GlobalJob.version == expected_version,
                            GlobalJob.state.in_(("creating",)),
                        )
                        .values(
                            state="created",
                            version=GlobalJob.version + 1,
                            updated_at=now,
                        )
                        .returning(GlobalJob.id)
                    )
                    if changed is None:
                        raise StaleTaskVersionError("analysis version is stale")

                key = await session.scalar(
                    select(IdempotencyKey)
                    .where(
                        IdempotencyKey.operation == _CREATE_OPERATION,
                        IdempotencyKey.scope_type == "team",
                        IdempotencyKey.scope_id == team_id,
                        IdempotencyKey.key == idempotency_key,
                    )
                    .with_for_update()
                )
                if (
                    key is None
                    or key.response_resource_id != analysis_id
                    or not hmac.compare_digest(key.request_hash, request_hash)
                ):
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                if key.state == "completed":
                    return
                if key.state != "pending":
                    raise AnalysisUnavailableError("analysis creation state is unavailable")
                key_changed = await session.scalar(
                    update(IdempotencyKey)
                    .where(
                        IdempotencyKey.id == key.id,
                        IdempotencyKey.version == key.version,
                        IdempotencyKey.state.in_(("pending",)),
                    )
                    .values(
                        state="completed",
                        version=IdempotencyKey.version + 1,
                        updated_at=now,
                    )
                    .returning(IdempotencyKey.id)
                )
                if key_changed is None:
                    raise StaleTaskVersionError("idempotency version is stale")

    async def load_view(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> AnalysisView:
        async with self._control_session_factory() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                )
            )
            if job is None:
                raise AnalysisNotFoundError("analysis was not found")
            latest_smartperfetto = await session.scalar(
                select(EngineExecution)
                .where(
                    EngineExecution.analysis_id == analysis_id,
                    EngineExecution.team_id == team_id,
                    EngineExecution.engine_id == "smartperfetto",
                )
                .order_by(EngineExecution.attempt_number.desc())
                .limit(1)
            )
            latest_synthesis = (
                await session.scalar(
                    select(SynthesisExecution)
                    .where(
                        SynthesisExecution.analysis_id == analysis_id,
                        SynthesisExecution.team_id == team_id,
                        SynthesisExecution.source_execution_id == latest_smartperfetto.id,
                    )
                    .order_by(SynthesisExecution.generation.desc())
                    .limit(1)
                )
                if latest_smartperfetto is not None
                else None
            )
            latest_source_task = await session.scalar(
                select(SourceTask)
                .where(
                    SourceTask.analysis_id == analysis_id,
                    SourceTask.team_id == team_id,
                    SourceTask.task_type == "source_context",
                )
                .order_by(SourceTask.created_at.desc(), SourceTask.id.desc())
                .limit(1)
            )
            children = list(
                (
                    await session.scalars(
                        select(ScenarioJob).where(ScenarioJob.analysis_id == analysis_id)
                    )
                ).all()
            )
            leases = list(
                (
                    await session.scalars(
                        select(AgentLease)
                        .where(
                            AgentLease.global_job_id == analysis_id,
                            AgentLease.state == "active",
                            AgentLease.expires_at > now,
                        )
                        .order_by(AgentLease.acquired_at.desc())
                        .limit(2)
                    )
                ).all()
            )
            if len(leases) > 1:
                raise AnalysisUnavailableError("analysis lease state is unavailable")
            lease = leases[0] if leases else None

        async with self._tenant_router.session(team_id) as session:
            tenant_analysis = await session.get(Analysis, analysis_id)
            if (
                tenant_analysis is None
                or tenant_analysis.tombstoned_at is not None
                or tenant_analysis.state == "deleted"
            ):
                raise AnalysisUnavailableError("tenant analysis state is unavailable")
            artifact = await session.scalar(
                select(Artifact)
                .where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.artifact_kind == "apk",
                    Artifact.idempotency_key == "initial-apk",
                    Artifact.deleted_at.is_(None),
                )
                .order_by(Artifact.created_at.desc())
            )
            input_artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).where(
                            Artifact.analysis_id == analysis_id,
                            Artifact.idempotency_key.like("input-%"),
                            Artifact.deleted_at.is_(None),
                        )
                    )
                ).all()
            )
            sample_attempts = list(
                (
                    await session.scalars(
                        select(SampleAttempt)
                        .join(
                            ScenarioResult,
                            ScenarioResult.id == SampleAttempt.scenario_job_id,
                        )
                        .where(ScenarioResult.analysis_id == analysis_id)
                    )
                ).all()
            )
            tenant_scenarios = list(
                (
                    await session.scalars(
                        select(ScenarioResult).where(ScenarioResult.analysis_id == analysis_id)
                    )
                ).all()
            )
            application_version = (
                await session.get(ApplicationVersion, tenant_analysis.application_version_id)
                if tenant_analysis.application_version_id is not None
                else None
            )
            report_versions = list(
                (
                    await session.scalars(
                        select(ReportVersion).where(
                            ReportVersion.analysis_id == analysis_id,
                            ReportVersion.scenario_result_id.is_not(None),
                        )
                    )
                ).all()
            )
            trace_report_versions = list(
                (
                    await session.scalars(
                        select(ReportVersion)
                        .where(
                            ReportVersion.analysis_id == analysis_id,
                            ReportVersion.scenario_result_id.is_(None),
                        )
                        .order_by(ReportVersion.report_version.desc())
                    )
                ).all()
            )

        if job.analysis_mode != tenant_analysis.analysis_mode:
            raise AnalysisUnavailableError("tenant analysis state is unavailable")
        source_binding = _stored_source_binding(job)
        if job.analysis_mode == "trace_upload":
            stored_manifest = tenant_analysis.input_manifest
            question = tenant_analysis.question
            if (
                tenant_analysis.application_version_id is not None
                or application_version is not None
                or artifact is not None
                or children
                or tenant_scenarios
                or sample_attempts
                or report_versions
                or lease is not None
                or tenant_analysis.analysis_profile not in ("auto", "startup", "scroll")
                or not isinstance(stored_manifest, list)
                or not all(isinstance(item, dict) for item in stored_manifest)
                or question is not None
                and (not question or question != question.strip() or len(question) > 2_000)
            ):
                raise AnalysisUnavailableError("trace analysis state is unavailable")
            try:
                canonical_manifest = _canonical_trace_inputs(tuple(stored_manifest))
            except AnalysisInvalidRequestError:
                raise AnalysisUnavailableError("trace analysis state is unavailable") from None
            if list(canonical_manifest) != stored_manifest:
                raise AnalysisUnavailableError("trace analysis state is unavailable")
            trace_report = _load_trace_report_from_versions(
                analysis_id=analysis_id,
                versions=trace_report_versions,
            )
            latest_trace_content = next(
                (row for row in trace_report_versions if row.report is not None),
                None,
            )
            report_available = (
                trace_report is not None
                and latest_trace_content is not None
                and latest_smartperfetto is not None
                and latest_smartperfetto.normalized_report_version_id == latest_trace_content.id
            )
            if (
                latest_synthesis is not None
                and latest_synthesis.report_version_id is not None
                and not report_available
            ):
                raise AnalysisUnavailableError("trace analysis report is unavailable")
            manifest_by_kind = {str(item["kind"]): item for item in canonical_manifest}
            artifacts_by_kind: dict[str, Artifact] = {}
            for input_artifact in input_artifacts:
                kind = input_artifact.artifact_kind
                expected = manifest_by_kind.get(kind)
                if (
                    expected is None
                    or kind in artifacts_by_kind
                    or input_artifact.idempotency_key != f"input-{kind}"
                    or input_artifact.mime_type != expected["mime"]
                    or input_artifact.size_bytes != expected["size"]
                    or input_artifact.sha256_b64 != expected["sha256_b64"]
                    or input_artifact.request_hash is None
                    or input_artifact.state not in ("pending", "finalized")
                    or input_artifact.state == "pending"
                    and (
                        input_artifact.version_id is not None
                        or input_artifact.finalized_at is not None
                    )
                    or input_artifact.state == "finalized"
                    and (input_artifact.version_id is None or input_artifact.finalized_at is None)
                ):
                    raise AnalysisUnavailableError("trace analysis input state is unavailable")
                artifacts_by_kind[kind] = input_artifact

            input_uploads: list[InputUploadView] = []
            for item in canonical_manifest:
                kind = str(item["kind"])
                input_artifact = artifacts_by_kind.get(kind)
                if input_artifact is None:
                    input_uploads.append(
                        InputUploadView(
                            state="awaiting_upload",
                            artifact_kind=kind,
                            mime=str(item["mime"]),
                            size=int(item["size"]),
                            sha256_b64=str(item["sha256_b64"]),
                            upload_id=None,
                            artifact_id=None,
                            expires_at=None,
                            finalized_at=None,
                        )
                    )
                elif input_artifact.state == "pending":
                    input_uploads.append(
                        InputUploadView(
                            state="pending",
                            artifact_kind=kind,
                            mime=input_artifact.mime_type,
                            size=input_artifact.size_bytes,
                            sha256_b64=input_artifact.sha256_b64,
                            upload_id=input_artifact.upload_id,
                            artifact_id=None,
                            expires_at=input_artifact.expires_at,
                            finalized_at=None,
                        )
                    )
                else:
                    input_uploads.append(
                        InputUploadView(
                            state="finalized",
                            artifact_kind=kind,
                            mime=input_artifact.mime_type,
                            size=input_artifact.size_bytes,
                            sha256_b64=input_artifact.sha256_b64,
                            upload_id=input_artifact.upload_id,
                            artifact_id=input_artifact.id,
                            expires_at=None,
                            finalized_at=input_artifact.finalized_at,
                        )
                    )
            return AnalysisView(
                analysis_id=job.id,
                team_id=job.team_id,
                analysis_mode="trace_upload",
                state=job.state,
                version=job.version,
                application_version_id=None,
                application_metadata=None,
                apk_upload=None,
                scenarios=(),
                sample_verdict_counts=SampleVerdictCounts(
                    valid=0,
                    invalid=0,
                    pending=0,
                    validation_error=0,
                    total=0,
                ),
                active_lease=None,
                report_available=report_available,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                failure_code=job.failure_code,
                cancel_requested_at=job.cancel_requested_at,
                question=question,
                analysis_profile=tenant_analysis.analysis_profile,  # type: ignore[arg-type]
                input_uploads=tuple(input_uploads),
                stages=_trace_stages(
                    job=job,
                    engine=latest_smartperfetto,
                    synthesis=latest_synthesis,
                    report_available=report_available,
                ),
                source_binding=source_binding,
                source_code_analysis=source_code_analysis_view(
                    source_binding, latest_source_task
                ),
            )

        children_by_type = {child.scenario_type: child for child in children}
        if len(children_by_type) != len(children):
            raise AnalysisUnavailableError("analysis child state is unavailable")
        ordered_children: list[ScenarioView] = []
        attempts_by_scenario: dict[UUID, list[SampleAttempt]] = {}
        for attempt in sample_attempts:
            attempts_by_scenario.setdefault(attempt.scenario_job_id, []).append(attempt)
        aggregate = SampleVerdictCounts(
            valid=0,
            invalid=0,
            pending=0,
            validation_error=0,
            total=0,
        )
        for scenario_type in _SCENARIOS:
            child = children_by_type.pop(scenario_type, None)
            if child is None:
                ordered_children.append(
                    ScenarioView(
                        scenario_job_id=None,
                        scenario_type=scenario_type,  # type: ignore[arg-type]
                        state="awaiting_input",
                        version=None,
                        device_group_id=None,
                        sample_verdict_counts=SampleVerdictCounts(
                            valid=0,
                            invalid=0,
                            pending=0,
                            validation_error=0,
                            total=0,
                        ),
                        started_at=None,
                        completed_at=None,
                        failure_code=None,
                    )
                )
                continue
            verdicts = _verdict_counts(
                attempts=attempts_by_scenario.get(child.id, []),
            )
            aggregate = _add_verdict_counts(aggregate, verdicts)
            ordered_children.append(
                ScenarioView(
                    scenario_job_id=child.id,
                    scenario_type=scenario_type,  # type: ignore[arg-type]
                    state=child.state,
                    version=child.version,
                    device_group_id=child.device_group_id,
                    sample_verdict_counts=verdicts,
                    started_at=child.started_at,
                    completed_at=child.completed_at,
                    failure_code=child.failure_code,
                )
            )
        if children_by_type:
            raise AnalysisUnavailableError("analysis child state is unavailable")

        application_metadata = self._application_metadata(application_version)
        if tenant_analysis.application_version_id is not None and application_metadata is None:
            raise AnalysisUnavailableError("application metadata is unavailable")
        if job.analysis_mode == "memory_upload":
            if (
                tenant_analysis.application_version_id is None
                or application_metadata is None
                or artifact is not None
                or children
                or tenant_scenarios
                or sample_attempts
                or report_versions
                or lease is not None
            ):
                raise AnalysisUnavailableError("memory analysis state is unavailable")
            return AnalysisView(
                analysis_id=job.id,
                team_id=job.team_id,
                analysis_mode="memory_upload",
                state=job.state,
                version=job.version,
                application_version_id=tenant_analysis.application_version_id,
                application_metadata=application_metadata,
                apk_upload=None,
                scenarios=(),
                sample_verdict_counts=aggregate,
                active_lease=None,
                report_available=False,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                failure_code=job.failure_code,
                cancel_requested_at=job.cancel_requested_at,
                question=tenant_analysis.question,
                source_binding=None,
                source_code_analysis=source_code_analysis_view(None),
            )
        if job.analysis_mode == "device" and tenant_analysis.question is not None:
            raise AnalysisUnavailableError("tenant analysis state is unavailable")
        apk_upload = self._stored_upload(artifact)
        if apk_upload is None:
            raise AnalysisUnavailableError("analysis artifact state is unavailable")
        active_lease = (
            ActiveLeaseView(
                lease_id=lease.id,
                device_id=lease.device_id,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
            )
            if lease is not None
            else None
        )
        terminal = AnalysisState(job.state) in ANALYSIS_TERMINAL_STATES
        device_report = _load_trace_report_from_versions(
            analysis_id=analysis_id,
            versions=trace_report_versions,
            analysis_mode="device",
        )
        latest_device_report = next(
            (row for row in trace_report_versions if row.report is not None),
            None,
        )
        device_report_available = (
            device_report is not None
            and latest_device_report is not None
            and latest_smartperfetto is not None
            and latest_smartperfetto.normalized_report_version_id
            == latest_device_report.id
        )
        if (
            latest_synthesis is not None
            and latest_synthesis.report_version_id is not None
            and not device_report_available
        ):
            raise AnalysisUnavailableError("device analysis report is unavailable")
        return AnalysisView(
            analysis_id=job.id,
            team_id=job.team_id,
            analysis_mode=job.analysis_mode,  # type: ignore[arg-type]
            device_id=job.selected_device_id,
            state=job.state,
            version=job.version,
            application_version_id=tenant_analysis.application_version_id,
            application_metadata=application_metadata,
            apk_upload=apk_upload,
            scenarios=tuple(ordered_children),
            sample_verdict_counts=aggregate,
            active_lease=active_lease,
            report_available=device_report_available
            or terminal
            and _report_is_available(
                children,
                tenant_scenarios,
                report_versions,
                parent_state=job.state,
            ),
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            failure_code=job.failure_code,
            cancel_requested_at=job.cancel_requested_at,
            question=tenant_analysis.question,
            source_binding=source_binding,
            source_code_analysis=source_code_analysis_view(
                source_binding, latest_source_task
            ),
        )

    async def require_finalizable(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
        sha256_b64: str,
        size: int,
        now: datetime,
    ) -> FinalizationPreparation:
        async with self._control_session_factory() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                    GlobalJob.analysis_mode == "device",
                )
            )
        if job is None or job.state in ("creating", "canceled"):
            raise AnalysisNotFoundError("analysis was not found")
        if job.state == "failed":
            if job.failure_code is None:
                raise AnalysisUnavailableError("analysis failure state is unavailable")
            raise ApkInspectionError(code=job.failure_code)
        control_requirements: SchedulingRequirements | None = None
        if job.state not in ("created", "uploading"):
            if not all(isinstance(value, str) for value in job.supported_abis):
                raise AnalysisUnavailableError("scheduling requirements are unavailable")
            control_requirements = SchedulingRequirements(
                min_api_level=job.min_api_level,
                supported_abis=tuple(job.supported_abis),  # type: ignore[arg-type]
            )

        async with self._tenant_router.session(team_id) as session:
            tenant_analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.upload_id == upload_id,
                    Artifact.idempotency_key == "initial-apk",
                    Artifact.artifact_kind == "apk",
                    Artifact.sha256_b64 == sha256_b64,
                    Artifact.size_bytes == size,
                    Artifact.deleted_at.is_(None),
                )
            )
            if tenant_analysis is None:
                raise AnalysisNotFoundError("analysis was not found")
            if tenant_analysis.state == "failed":
                if tenant_analysis.failure_code is None:
                    raise AnalysisUnavailableError("analysis failure state is unavailable")
                await self._fail_control_apk_inspection(
                    team_id=team_id,
                    analysis_id=analysis_id,
                    failure_code=tenant_analysis.failure_code,
                    now=now,
                )
                raise ApkInspectionError(code=tenant_analysis.failure_code)
            if tenant_analysis.state in ("canceled", "deleted"):
                raise AnalysisNotFoundError("analysis was not found")
            if artifact is None:
                raise AnalysisNotFoundError("analysis APK was not found")

            if tenant_analysis.application_version_id is not None:
                version = await session.get(
                    ApplicationVersion,
                    tenant_analysis.application_version_id,
                )
                metadata = self._application_metadata(version)
                if metadata is None:
                    raise AnalysisUnavailableError("application metadata is unavailable")
                requirements = SchedulingRequirements(
                    min_api_level=metadata.min_sdk,
                    supported_abis=metadata.supported_abis,
                )
                if control_requirements is not None and control_requirements != requirements:
                    raise AnalysisUnavailableError("scheduling requirements changed")
                if artifact.state != "finalized" or artifact.version_id is None:
                    raise AnalysisUnavailableError("analysis APK state is unavailable")
                return FinalizationPreparation(
                    requirements=requirements,
                    inspection_token=tenant_analysis.apk_inspection_token,
                )

            if control_requirements is not None:
                raise AnalysisUnavailableError("application metadata is unavailable")
            if artifact.state not in ("pending", "finalized"):
                raise AnalysisNotFoundError("analysis APK was not found")
            if (tenant_analysis.apk_inspection_token is None) != (
                tenant_analysis.apk_inspection_claimed_at is None
            ):
                raise AnalysisUnavailableError("APK inspection claim is invalid")
            if (
                tenant_analysis.apk_inspection_token is not None
                and tenant_analysis.apk_inspection_claimed_at is not None
                and tenant_analysis.apk_inspection_claimed_at > now - _APK_INSPECTION_CLAIM_TTL
            ):
                raise AnalysisUnavailableError("APK inspection is already in progress")

            inspection_token = uuid4()
            tenant_analysis.apk_inspection_token = inspection_token
            tenant_analysis.apk_inspection_claimed_at = now
            if tenant_analysis.state == "created":
                tenant_analysis.state = "uploading"
            tenant_analysis.version += 1
            tenant_analysis.updated_at = now
            await session.flush()
            return FinalizationPreparation(
                requirements=None,
                inspection_token=inspection_token,
            )

    async def release_apk_inspection(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        inspection_token: UUID,
        now: datetime,
    ) -> None:
        async with self._tenant_router.session(team_id) as session:
            await session.execute(
                update(Analysis)
                .where(
                    Analysis.id == analysis_id,
                    Analysis.apk_inspection_token == inspection_token,
                )
                .values(
                    apk_inspection_token=None,
                    apk_inspection_claimed_at=None,
                    version=Analysis.version + 1,
                    updated_at=now,
                )
            )

    async def classify_upload(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        upload_id: UUID,
    ) -> Literal["initial_apk", "trace_input", "generic"]:
        async with self._control_session_factory() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                )
            )
        if job is None:
            raise AnalysisNotFoundError("analysis was not found")
        async with self._tenant_router.session(team_id) as session:
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.upload_id == upload_id,
                    Artifact.deleted_at.is_(None),
                )
            )
        if artifact is None:
            raise AnalysisNotFoundError("analysis upload was not found")
        if artifact.idempotency_key == "initial-apk":
            if artifact.artifact_kind != "apk":
                raise AnalysisUnavailableError("initial APK state is unavailable")
            return "initial_apk"
        if job.analysis_mode == "trace_upload":
            if artifact.idempotency_key != f"input-{artifact.artifact_kind}":
                raise AnalysisUnavailableError("trace input state is unavailable")
            return "trace_input"
        return "generic"

    async def trace_required_input_ready(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> bool:
        async with self._control_session_factory() as session:
            exists = await session.scalar(
                select(GlobalJob.id).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                    GlobalJob.analysis_mode == "trace_upload",
                )
            )
        if exists is None:
            raise AnalysisNotFoundError("analysis was not found")

        async with self._tenant_router.session(team_id) as session:
            analysis = await session.get(Analysis, analysis_id)
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.analysis_id == analysis_id,
                    Artifact.idempotency_key == "input-trace",
                    Artifact.artifact_kind == "trace",
                    Artifact.deleted_at.is_(None),
                )
            )
        if (
            analysis is None
            or analysis.analysis_mode != "trace_upload"
            or analysis.tombstoned_at is not None
            or analysis.state == "deleted"
            or not isinstance(analysis.input_manifest, list)
        ):
            raise AnalysisUnavailableError("trace analysis state is unavailable")
        try:
            manifest = _canonical_trace_inputs(tuple(analysis.input_manifest))
        except AnalysisInvalidRequestError:
            raise AnalysisUnavailableError("trace analysis state is unavailable") from None
        trace_input = next((item for item in manifest if item["kind"] == "trace"), None)
        if trace_input is None:
            raise AnalysisUnavailableError("trace analysis state is unavailable")
        if artifact is None:
            return False
        if (
            artifact.mime_type != trace_input["mime"]
            or artifact.size_bytes != trace_input["size"]
            or artifact.sha256_b64 != trace_input["sha256_b64"]
            or artifact.request_hash is None
        ):
            raise AnalysisUnavailableError("trace input state is unavailable")
        if artifact.state == "pending":
            if artifact.version_id is not None or artifact.finalized_at is not None:
                raise AnalysisUnavailableError("trace input state is unavailable")
            return False
        if (
            artifact.state != "finalized"
            or artifact.version_id is None
            or artifact.finalized_at is None
        ):
            raise AnalysisUnavailableError("trace input state is unavailable")
        return True

    @staticmethod
    def _trace_ready_event_matches(
        event: OutboxEvent | None,
        *,
        event_id: UUID,
        team_id: UUID,
        analysis_id: UUID,
    ) -> bool:
        return (
            event is not None
            and event.id == event_id
            and event.team_id == team_id
            and event.global_job_id == analysis_id
            and event.scenario_job_id is None
            and event.event_type == "trace_analysis_ready"
            and event.subject_type == "analysis"
            and event.subject_id == analysis_id
            and event.ready_at is not None
        )

    async def queue_trace_execution(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        now: datetime,
    ) -> None:
        if not await self.trace_required_input_ready(
            team_id=team_id,
            analysis_id=analysis_id,
        ):
            raise AnalysisUnavailableError("trace analysis input is not ready")

        event_id = trace_analysis_ready_event_id(analysis_id)
        terminal_states = {"completed", "partially_completed", "failed", "canceled"}
        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                    )
                    .with_for_update()
                )
                if job is None:
                    raise AnalysisNotFoundError("analysis was not found")
                if job.analysis_mode != "trace_upload":
                    raise AnalysisUnavailableError("trace analysis state is unavailable")

                event = await session.get(OutboxEvent, event_id)
                if job.state == "uploading":
                    if event is not None:
                        raise AnalysisUnavailableError("trace analysis queue state is unavailable")
                    try:
                        transition(job.state, "analyzing")
                    except InvalidTransition:
                        raise AnalysisUnavailableError(
                            "trace analysis queue state is unavailable"
                        ) from None
                    session.add(
                        OutboxEvent(
                            id=event_id,
                            team_id=team_id,
                            global_job_id=analysis_id,
                            scenario_job_id=None,
                            event_type="trace_analysis_ready",
                            subject_type="analysis",
                            subject_id=analysis_id,
                            ready_at=now,
                            published_at=None,
                            dead_lettered_at=None,
                            retry_count=0,
                            version=1,
                        )
                    )
                    changed = await session.scalar(
                        update(GlobalJob)
                        .where(
                            GlobalJob.id == analysis_id,
                            GlobalJob.team_id == team_id,
                            GlobalJob.analysis_mode == "trace_upload",
                            GlobalJob.state == "uploading",
                            GlobalJob.version == job.version,
                        )
                        .values(
                            state="analyzing",
                            started_at=job.started_at or now,
                            completed_at=None,
                            failure_code=None,
                            version=GlobalJob.version + 1,
                            updated_at=now,
                        )
                        .returning(GlobalJob.id)
                    )
                    if changed is None:
                        raise StaleTaskVersionError("analysis version is stale")
                elif job.state == "analyzing" or job.state in terminal_states:
                    if not self._trace_ready_event_matches(
                        event,
                        event_id=event_id,
                        team_id=team_id,
                        analysis_id=analysis_id,
                    ):
                        raise AnalysisUnavailableError("trace analysis queue state is unavailable")
                else:
                    raise AnalysisUnavailableError("trace analysis queue state is unavailable")

                control_state = job.state

        if control_state in terminal_states:
            return

        async with self._tenant_router.session(team_id) as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None or analysis.analysis_mode != "trace_upload":
                raise AnalysisUnavailableError("trace analysis state is unavailable")
            if analysis.state == "uploading":
                try:
                    transition(analysis.state, "analyzing")
                except InvalidTransition:
                    raise AnalysisUnavailableError(
                        "trace analysis queue state is unavailable"
                    ) from None
                changed = await session.scalar(
                    update(Analysis)
                    .where(
                        Analysis.id == analysis_id,
                        Analysis.analysis_mode == "trace_upload",
                        Analysis.state == "uploading",
                        Analysis.version == analysis.version,
                    )
                    .values(
                        state="analyzing",
                        started_at=analysis.started_at or now,
                        completed_at=None,
                        failure_code=None,
                        version=Analysis.version + 1,
                        updated_at=now,
                    )
                    .returning(Analysis.id)
                )
                if changed is None:
                    raise StaleTaskVersionError("analysis version is stale")
            elif analysis.state != "analyzing" and analysis.state not in terminal_states:
                raise AnalysisUnavailableError("trace analysis queue state is unavailable")

    @staticmethod
    def _application_metadata(
        version: ApplicationVersion | None,
    ) -> ApplicationMetadataView | None:
        if (
            version is None
            or version.has_native_libraries is None
            or not all(isinstance(value, str) for value in version.supported_abis)
        ):
            return None
        return ApplicationMetadataView(
            package_name=version.package_name,
            version_name=version.version_name,
            version_code=version.version_code,
            launch_activity=version.launch_activity,
            min_sdk=version.min_api_level,
            target_sdk=version.target_api_level,
            supported_abis=tuple(version.supported_abis),  # type: ignore[arg-type]
            has_native_libraries=version.has_native_libraries,
        )

    @staticmethod
    def _stored_upload(artifact: Artifact | None) -> UploadSlot | None:
        if artifact is None or artifact.state not in ("pending", "finalized"):
            return None
        if artifact.state == "finalized" and (
            artifact.version_id is None or artifact.finalized_at is None
        ):
            raise AnalysisUnavailableError("analysis artifact state is unavailable")
        return UploadSlot(
            artifact_id=artifact.id,
            upload_id=artifact.upload_id,
            artifact_kind=artifact.artifact_kind,
            mime=artifact.mime_type,
            size=artifact.size_bytes,
            sha256_b64=artifact.sha256_b64,
            state=artifact.state,  # type: ignore[arg-type]
            expires_at=artifact.expires_at,
            finalized_at=artifact.finalized_at,
            required_headers={},
            put_url=None,
            object_key=artifact.object_key,
            version_id=artifact.version_id,
        )

    async def persist_apk_metadata(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        apk_sha256_b64: str,
        metadata: InspectedApkMetadata,
        inspection_token: UUID,
        now: datetime,
    ) -> UUID:
        application_id = uuid5(_APPLICATION_NAMESPACE, metadata.package_name)
        version_id = uuid5(
            _APPLICATION_VERSION_NAMESPACE,
            f"{application_id}:{metadata.version_code}:{apk_sha256_b64}",
        )
        async with self._tenant_router.session(team_id) as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None or analysis.state in ("failed", "canceled", "deleted"):
                raise AnalysisNotFoundError("analysis was not found")
            if (
                analysis.apk_inspection_token != inspection_token
                or analysis.apk_inspection_claimed_at is None
            ):
                raise StaleTaskVersionError("APK inspection claim is stale")
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                    Artifact.idempotency_key == "initial-apk",
                    Artifact.artifact_kind == "apk",
                    Artifact.state == "finalized",
                    Artifact.sha256_b64 == apk_sha256_b64,
                    Artifact.version_id.is_not(None),
                    Artifact.deleted_at.is_(None),
                )
            )
            if artifact is None:
                raise AnalysisNotFoundError("analysis APK was not found")

            await session.execute(
                postgresql_insert(Application)
                .values(
                    id=application_id,
                    name=metadata.package_name,
                    package_name=metadata.package_name,
                    description=None,
                )
                .on_conflict_do_nothing(index_elements=(Application.package_name,))
            )
            application = await session.scalar(
                select(Application).where(Application.package_name == metadata.package_name)
            )
            if application is None:
                raise AnalysisUnavailableError("application catalog is unavailable")
            application_id = application.id
            version_id = uuid5(
                _APPLICATION_VERSION_NAMESPACE,
                f"{application_id}:{metadata.version_code}:{apk_sha256_b64}",
            )
            await session.execute(
                postgresql_insert(ApplicationVersion)
                .values(
                    id=version_id,
                    application_id=application_id,
                    package_name=metadata.package_name,
                    version_name=metadata.version_name,
                    version_code=metadata.version_code,
                    min_api_level=metadata.min_sdk,
                    target_api_level=metadata.target_sdk,
                    launch_activity=metadata.launch_activity,
                    supported_abis=list(metadata.supported_abis),
                    has_native_libraries=metadata.has_native_libraries,
                    apk_sha256_b64=apk_sha256_b64,
                    manifest_sha256=metadata.manifest_sha256,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        ApplicationVersion.application_id,
                        ApplicationVersion.version_code,
                        ApplicationVersion.apk_sha256_b64,
                    )
                )
            )
            version = await session.scalar(
                select(ApplicationVersion).where(
                    ApplicationVersion.application_id == application_id,
                    ApplicationVersion.version_code == metadata.version_code,
                    ApplicationVersion.apk_sha256_b64 == apk_sha256_b64,
                )
            )
            if version is None or not self._metadata_matches(version, metadata):
                raise AnalysisUnavailableError("application version is unavailable")
            for scenario_type in _SCENARIOS:
                default_recipe = _default_recipe(scenario_type)
                default_hash = _recipe_hash(default_recipe)
                await session.execute(
                    postgresql_insert(ScenarioRecipe)
                    .values(
                        id=uuid5(
                            _RECIPE_NAMESPACE,
                            f"{version.id}:{scenario_type}:1:{default_hash}",
                        ),
                        application_id=application_id,
                        application_version_id=version.id,
                        scenario_type=scenario_type,
                        recipe_version=1,
                        recipe_hash=default_hash,
                        recipe=default_recipe,
                        is_active=True,
                    )
                    .on_conflict_do_nothing(
                        index_elements=(
                            ScenarioRecipe.application_version_id,
                            ScenarioRecipe.scenario_type,
                            ScenarioRecipe.recipe_version,
                        )
                    )
                )
            if analysis.application_version_id == version.id:
                return version.id
            if analysis.application_version_id is not None:
                raise AnalysisUnavailableError("analysis application version changed")
            changed = await session.scalar(
                update(Analysis)
                .where(
                    Analysis.id == analysis_id,
                    Analysis.version == analysis.version,
                    Analysis.state.in_(("creating", "created", "uploading")),
                    Analysis.application_version_id.is_(None),
                )
                .values(
                    application_version_id=version.id,
                    version=Analysis.version + 1,
                    updated_at=now,
                )
                .returning(Analysis.id)
            )
            if changed is None:
                raise StaleTaskVersionError("tenant analysis version is stale")
            return version.id

    async def fail_apk_inspection(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        failure_code: str,
        inspection_token: UUID,
        now: datetime,
    ) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", failure_code) is None:
            failure_code = "apk_invalid"
        async with self._tenant_router.session(team_id) as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            if analysis is None:
                raise AnalysisNotFoundError("analysis was not found")
            if (
                analysis.apk_inspection_token != inspection_token
                or analysis.apk_inspection_claimed_at is None
            ):
                raise StaleTaskVersionError("APK inspection claim is stale")
            if analysis.state == "failed":
                if analysis.failure_code != failure_code:
                    raise AnalysisUnavailableError("analysis failure state changed")
            elif analysis.state in ("creating", "created", "uploading"):
                changed = await session.scalar(
                    update(Analysis)
                    .where(
                        Analysis.id == analysis_id,
                        Analysis.version == analysis.version,
                        Analysis.state.in_(("creating", "created", "uploading")),
                    )
                    .values(
                        state="failed",
                        failure_code=failure_code,
                        completed_at=now,
                        version=Analysis.version + 1,
                        updated_at=now,
                    )
                    .returning(Analysis.id)
                )
                if changed is None:
                    raise StaleTaskVersionError("tenant analysis version is stale")
            else:
                raise AnalysisUnavailableError("analysis cannot fail APK inspection")

        await self._fail_control_apk_inspection(
            team_id=team_id,
            analysis_id=analysis_id,
            failure_code=failure_code,
            now=now,
        )
        await self.release_apk_inspection(
            team_id=team_id,
            analysis_id=analysis_id,
            inspection_token=inspection_token,
            now=now,
        )

    async def _fail_control_apk_inspection(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        failure_code: str,
        now: datetime,
    ) -> None:
        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                        GlobalJob.analysis_mode == "device",
                    )
                    .with_for_update()
                )
                if job is None:
                    raise AnalysisNotFoundError("analysis was not found")
                if job.state == "failed":
                    if job.failure_code != failure_code:
                        raise AnalysisUnavailableError("analysis failure state changed")
                    return
                if job.state not in ("created", "uploading"):
                    raise AnalysisUnavailableError("analysis cannot fail APK inspection")
                changed = await session.scalar(
                    update(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                        GlobalJob.version == job.version,
                        GlobalJob.state.in_(("created", "uploading")),
                    )
                    .values(
                        state="failed",
                        failure_code=failure_code,
                        completed_at=now,
                        version=GlobalJob.version + 1,
                        updated_at=now,
                    )
                    .returning(GlobalJob.id)
                )
                if changed is None:
                    raise StaleTaskVersionError("analysis version is stale")

    @staticmethod
    def _metadata_matches(
        version: ApplicationVersion,
        metadata: InspectedApkMetadata,
    ) -> bool:
        return (
            version.package_name == metadata.package_name
            and version.version_name == metadata.version_name
            and version.version_code == metadata.version_code
            and version.min_api_level == metadata.min_sdk
            and version.target_api_level == metadata.target_sdk
            and version.launch_activity == metadata.launch_activity
            and version.supported_abis == list(metadata.supported_abis)
            and version.has_native_libraries == metadata.has_native_libraries
            and version.manifest_sha256 == metadata.manifest_sha256
        )

    async def stage_tenant_scenarios(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> tuple[PreparedScenario, ...]:
        async with self._tenant_router.session(team_id) as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id).with_for_update()
            )
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.analysis_id == analysis_id,
                    Artifact.artifact_kind == "apk",
                    Artifact.state == "finalized",
                    Artifact.version_id.is_not(None),
                    Artifact.deleted_at.is_(None),
                )
            )
            if analysis is None or artifact is None:
                raise AnalysisNotFoundError("analysis was not found")
            if analysis.state in ("failed", "canceled", "deleted"):
                raise AnalysisInvalidRequestError("analysis cannot be queued")

            if analysis.application_version_id is None:
                raise AnalysisUnavailableError("APK metadata is unavailable")
            application_version = await session.get(
                ApplicationVersion,
                analysis.application_version_id,
            )
            if application_version is None:
                raise AnalysisUnavailableError("application version is unavailable")

            existing_children = list(
                (
                    await session.scalars(
                        select(ScenarioResult).where(ScenarioResult.analysis_id == analysis_id)
                    )
                ).all()
            )
            if existing_children:
                if analysis.state in ("creating", "created", "uploading"):
                    raise AnalysisUnavailableError("tenant scenario projection is unavailable")
                return _prepared_scenarios_from_results(
                    analysis_id,
                    existing_children,
                )
            if analysis.state not in ("creating", "created", "uploading"):
                raise AnalysisUnavailableError("tenant scenario projection is unavailable")

            prepared: list[PreparedScenario] = []
            for scenario_type in _SCENARIOS:
                recipe = await session.scalar(
                    select(ScenarioRecipe)
                    .where(
                        ScenarioRecipe.application_id == application_version.application_id,
                        ScenarioRecipe.application_version_id == application_version.id,
                        ScenarioRecipe.scenario_type == scenario_type,
                        ScenarioRecipe.is_active.is_(True),
                    )
                    .order_by(ScenarioRecipe.recipe_version.desc())
                    .limit(1)
                )
                if recipe is None:
                    raise AnalysisUnavailableError("scenario recipe is unavailable")
                _validate_recipe(recipe.recipe, scenario_type)
                if _recipe_hash(recipe.recipe) != recipe.recipe_hash:
                    raise AnalysisUnavailableError("scenario recipe is unavailable")
                prepared.append(
                    PreparedScenario(
                        scenario_job_id=scenario_job_id(analysis_id, scenario_type),
                        scenario_type=scenario_type,  # type: ignore[arg-type]
                        scenario_recipe_id=recipe.id,
                        recipe_version=recipe.recipe_version,
                        recipe_hash=recipe.recipe_hash,
                        recipe_snapshot=_copy_recipe(recipe.recipe),
                    )
                )

            for item in prepared:
                await session.execute(
                    postgresql_insert(ScenarioResult)
                    .values(
                        id=item.scenario_job_id,
                        analysis_id=analysis_id,
                        scenario_type=item.scenario_type,
                        scenario_recipe_id=item.scenario_recipe_id,
                        recipe_version=item.recipe_version,
                        recipe_hash=item.recipe_hash,
                        recipe_snapshot=item.recipe_snapshot,
                        state="queued",
                        version=1,
                    )
                    .on_conflict_do_nothing(
                        index_elements=(
                            ScenarioResult.analysis_id,
                            ScenarioResult.scenario_type,
                        )
                    )
                )

            children = list(
                (
                    await session.scalars(
                        select(ScenarioResult).where(ScenarioResult.analysis_id == analysis_id)
                    )
                ).all()
            )
            frozen = _prepared_scenarios_from_results(analysis_id, children)
            if frozen != tuple(prepared):
                raise AnalysisUnavailableError("tenant scenario projection is unavailable")

            if analysis.state in ("creating", "created", "uploading"):
                changed = await session.scalar(
                    update(Analysis)
                    .where(
                        Analysis.id == analysis_id,
                        Analysis.version == analysis.version,
                        Analysis.state.in_(("creating", "created", "uploading")),
                    )
                    .values(
                        state="queued",
                        version=Analysis.version + 1,
                        updated_at=now,
                    )
                    .returning(Analysis.id)
                )
                if changed is None:
                    raise StaleTaskVersionError("tenant analysis version is stale")
            return frozen

    async def queue_control_scenarios(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        scenarios: tuple[PreparedScenario, ...],
        requirements: SchedulingRequirements,
        now: datetime,
    ) -> None:
        scenarios_by_type = {item.scenario_type: item for item in scenarios}
        if (
            tuple(item.scenario_type for item in scenarios) != _SCENARIOS
            or len(scenarios_by_type) != 3
        ):
            raise AnalysisUnavailableError("scenario preparation is unavailable")
        if (
            requirements.min_api_level is not None
            and requirements.min_api_level < 1
            or len(set(requirements.supported_abis)) != len(requirements.supported_abis)
            or not set(requirements.supported_abis) <= _SUPPORTED_ABIS
        ):
            raise AnalysisUnavailableError("scheduling requirements are unavailable")
        async with self._control_session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                    )
                    .with_for_update()
                )
                if job is None:
                    raise AnalysisNotFoundError("analysis was not found")
                if job.state in ("creating", "failed", "canceled"):
                    raise AnalysisInvalidRequestError("analysis cannot be queued")

                existing_children = list(
                    (
                        await session.scalars(
                            select(ScenarioJob).where(ScenarioJob.analysis_id == analysis_id)
                        )
                    ).all()
                )
                expected = {
                    scenario_type: scenario_job_id(analysis_id, scenario_type)
                    for scenario_type in _SCENARIOS
                }
                if job.state not in ("creating", "created", "uploading"):
                    event = await session.get(
                        OutboxEvent,
                        analysis_queued_event_id(analysis_id),
                    )
                    if (
                        job.input_artifact_id != artifact_id
                        or job.min_api_level != requirements.min_api_level
                        or job.supported_abis != list(requirements.supported_abis)
                        or len(existing_children) != 3
                        or any(
                            expected.get(child.scenario_type) != child.id
                            or child.input_artifact_id != artifact_id
                            or child.min_api_level != requirements.min_api_level
                            or child.supported_abis != list(requirements.supported_abis)
                            or child.recipe_hash
                            != scenarios_by_type[child.scenario_type].recipe_hash
                            or child.recipe_version
                            != scenarios_by_type[child.scenario_type].recipe_version
                            or child.scenario_recipe_id
                            != scenarios_by_type[child.scenario_type].scenario_recipe_id
                            for child in existing_children
                        )
                        or event is None
                        or event.id != analysis_queued_event_id(analysis_id)
                        or event.team_id != team_id
                        or event.global_job_id != analysis_id
                        or event.scenario_job_id is not None
                        or event.event_type != "analysis_queued"
                        or event.subject_type != "analysis"
                        or event.subject_id != analysis_id
                        or event.ready_at is None
                    ):
                        raise AnalysisUnavailableError("analysis queue state is unavailable")
                    return
                if existing_children:
                    raise AnalysisUnavailableError("analysis queue state is unavailable")

                for scenario_type in _SCENARIOS:
                    prepared = scenarios_by_type[scenario_type]
                    session.add(
                        ScenarioJob(
                            id=expected[scenario_type],
                            analysis_id=analysis_id,
                            scenario_type=scenario_type,
                            scenario_recipe_id=prepared.scenario_recipe_id,
                            recipe_version=prepared.recipe_version,
                            recipe_hash=prepared.recipe_hash,
                            state="queued",
                            input_artifact_id=artifact_id,
                            required_abi=job.required_abi,
                            supported_abis=list(requirements.supported_abis),
                            min_api_level=requirements.min_api_level,
                            attempt_count=0,
                            valid_sample_count=0,
                            invalid_sample_count=0,
                            retry_count=0,
                            max_attempts=10,
                            version=1,
                        )
                    )
                session.add(
                    OutboxEvent(
                        id=analysis_queued_event_id(analysis_id),
                        team_id=team_id,
                        global_job_id=analysis_id,
                        scenario_job_id=None,
                        event_type="analysis_queued",
                        subject_type="analysis",
                        subject_id=analysis_id,
                        ready_at=now,
                        retry_count=0,
                        version=1,
                    )
                )
                changed = await session.scalar(
                    update(GlobalJob)
                    .where(
                        GlobalJob.id == analysis_id,
                        GlobalJob.team_id == team_id,
                        GlobalJob.version == job.version,
                        GlobalJob.state.in_(("created", "uploading")),
                    )
                    .values(
                        state="queued",
                        input_artifact_id=artifact_id,
                        supported_abis=list(requirements.supported_abis),
                        min_api_level=requirements.min_api_level,
                        version=GlobalJob.version + 1,
                        updated_at=now,
                    )
                    .returning(GlobalJob.id)
                )
                if changed is None:
                    raise StaleTaskVersionError("analysis version is stale")
                await session.flush()

    async def load_report(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
    ) -> dict[str, object]:
        async with self._control_session_factory() as session:
            job = await session.scalar(
                select(GlobalJob).where(
                    GlobalJob.id == analysis_id,
                    GlobalJob.team_id == team_id,
                )
            )
            if job is None:
                raise AnalysisNotFoundError("analysis was not found")
            if (
                job.analysis_mode != "trace_upload"
                and AnalysisState(job.state) not in ANALYSIS_TERMINAL_STATES
            ):
                raise ReportNotAvailableError("analysis report is not available")
            children = (
                []
                if job.analysis_mode == "trace_upload"
                else list(
                    (
                        await session.scalars(
                            select(ScenarioJob).where(ScenarioJob.analysis_id == analysis_id)
                        )
                    ).all()
                )
            )

        async with self._tenant_router.session(team_id) as session:
            analysis = await session.get(Analysis, analysis_id)
            if analysis is None or analysis.tombstoned_at is not None:
                raise AnalysisNotFoundError("analysis was not found")
            if analysis.analysis_mode != job.analysis_mode:
                raise AnalysisUnavailableError("analysis report identity is invalid")
            if job.analysis_mode in {"trace_upload", "device"}:
                trace_versions = list(
                    (
                        await session.scalars(
                            select(ReportVersion)
                            .where(
                                ReportVersion.analysis_id == analysis_id,
                                ReportVersion.scenario_result_id.is_(None),
                            )
                            .order_by(ReportVersion.report_version.desc())
                        )
                    ).all()
                )
                report = _load_trace_report_from_versions(
                    analysis_id=analysis_id,
                    versions=trace_versions,
                    analysis_mode=job.analysis_mode,  # type: ignore[arg-type]
                )
                if report is not None:
                    return report
                if job.analysis_mode == "trace_upload":
                    raise ReportNotAvailableError("analysis report is not available")
            scenarios = list(
                (
                    await session.scalars(
                        select(ScenarioResult).where(ScenarioResult.analysis_id == analysis_id)
                    )
                ).all()
            )
            versions = list(
                (
                    await session.scalars(
                        select(ReportVersion)
                        .where(
                            ReportVersion.analysis_id == analysis_id,
                            ReportVersion.scenario_result_id.is_not(None),
                        )
                        .order_by(ReportVersion.report_version.desc())
                    )
                ).all()
            )
        if not _report_is_available(
            children,
            scenarios,
            versions,
            parent_state=job.state,
        ):
            raise ReportNotAvailableError("analysis report is not available")
        return _assemble_report(job=job, scenarios=scenarios, versions=versions)


def _validated_scenario_bundle(
    *,
    version: ReportVersion | None,
    scenario: ScenarioResult,
    scenario_type: str,
) -> dict[str, object] | None:
    if version is None:
        return None
    if (version.bundle is None) != (version.bundle_sha256_b64 is None):
        raise AnalysisUnavailableError("analysis bundle metadata is invalid")
    if version.bundle is None or version.bundle_sha256_b64 is None:
        return None
    if not hmac.compare_digest(
        _bundle_sha256_b64(version.bundle),
        version.bundle_sha256_b64,
    ):
        raise AnalysisUnavailableError("analysis bundle checksum is invalid")
    candidate = _copy_public_json(version.bundle)
    if not isinstance(candidate, dict):
        raise AnalysisUnavailableError("analysis bundle is invalid")
    _validate_report_contract("analysis-bundle.schema.json", candidate)
    if (
        candidate.get("schema_version") != "1.0"
        or candidate.get("scenario_job_id") != str(scenario.id)
        or candidate.get("scenario_type") != scenario_type
        or candidate.get("bundle_state") not in ("complete", "partial")
    ):
        raise AnalysisUnavailableError("analysis bundle identity is invalid")
    return candidate


def _assemble_report(
    *,
    job: GlobalJob,
    scenarios: list[ScenarioResult],
    versions: list[ReportVersion],
) -> dict[str, object]:
    latest: dict[UUID, ReportVersion] = {}
    for version in sorted(
        versions,
        key=lambda item: item.report_version,
        reverse=True,
    ):
        if version.scenario_result_id is not None:
            latest.setdefault(version.scenario_result_id, version)
    scenarios_by_type = {scenario.scenario_type: scenario for scenario in scenarios}
    if len(scenarios_by_type) != len(scenarios):
        raise AnalysisUnavailableError("analysis report state is unavailable")
    try:
        aggregate_state = derive_parent_state(scenario.state for scenario in scenarios)
    except InvalidAggregateState:
        raise AnalysisUnavailableError("analysis aggregate state is unavailable") from None
    if aggregate_state.value != job.state:
        raise AnalysisUnavailableError("analysis aggregate state is unavailable")

    scenario_reports: list[dict[str, object]] = []
    selected_versions: list[ReportVersion] = []
    for scenario_type in _SCENARIOS:
        scenario = scenarios_by_type.pop(scenario_type, None)
        if scenario is None or scenario.state not in ("completed", "failed", "canceled"):
            raise ReportNotAvailableError("analysis report is not available")
        version = latest.get(scenario.id)
        bundle = _validated_scenario_bundle(
            version=version,
            scenario=scenario,
            scenario_type=scenario_type,
        )
        if version is not None:
            selected_versions.append(version)
        device_group_reason = scenario.device_group_reason
        if scenario.device_group_id is None and device_group_reason is None:
            raise ReportNotAvailableError("analysis report is not available")
        if scenario.device_group_id is not None and device_group_reason is not None:
            raise AnalysisUnavailableError("analysis device group state is invalid")
        failure: dict[str, object] | None = None
        if scenario.failure_code is not None:
            failure = {
                "code": scenario.failure_code,
                "message": "场景未能完成",
                "retryable": False,
            }
        item: dict[str, object] = {
            "scenario_job_id": str(scenario.id),
            "scenario_type": scenario_type,
            "result_state": scenario.state,
            "device_group_id": (
                str(scenario.device_group_id) if scenario.device_group_id is not None else None
            ),
            "device_group_reason": device_group_reason,
            "bundle": bundle,
            "failure": failure,
        }
        if scenario.state == "completed":
            if bundle is None or bundle.get("bundle_state") != "complete":
                raise ReportNotAvailableError("analysis report is not available")
            item["failure"] = None
        else:
            if bundle is not None and bundle.get("bundle_state") != "partial":
                raise AnalysisUnavailableError("partial analysis bundle is invalid")
            if failure is None and bundle is None:
                raise ReportNotAvailableError("analysis report is not available")
        scenario_reports.append(item)
    if scenarios_by_type:
        raise ReportNotAvailableError("analysis report is not available")
    generated_candidates = [job.updated_at]
    generated_candidates.extend(scenario.updated_at for scenario in scenarios)
    generated_candidates.extend(version.generated_at for version in selected_versions)
    generated_at = max(generated_candidates)
    aggregate_version = _aggregate_report_version(
        scenarios=scenarios,
        latest=latest,
    )
    report: dict[str, object] = {
        "schema_version": "1.0",
        "analysis_id": str(job.id),
        "analysis_mode": job.analysis_mode,
        "state": job.state,
        "report_version": aggregate_version,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "scenario_reports": scenario_reports,
    }
    _validate_report_contract("analysis-report.schema.json", report)
    return report


def _aggregate_report_version(
    *,
    scenarios: list[ScenarioResult],
    latest: dict[UUID, ReportVersion],
) -> int:
    by_type = {scenario.scenario_type: scenario for scenario in scenarios}
    if len(by_type) != 3:
        raise AnalysisUnavailableError("analysis report state is unavailable")
    components: list[int] = []
    for scenario_type in _SCENARIOS:
        scenario = by_type.get(scenario_type)
        if scenario is None or scenario.version < 1:
            raise AnalysisUnavailableError("analysis report state is unavailable")
        components.append(scenario.version)
        report = latest.get(scenario.id)
        components.append(report.report_version if report is not None else 0)
    digest = hashlib.sha256(json.dumps(components, separators=(",", ":")).encode("ascii")).digest()
    aggregate = int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)
    return aggregate or 1


def _verdict_counts(
    *,
    attempts: list[SampleAttempt],
) -> SampleVerdictCounts:
    total = len(attempts)
    valid = sum(attempt.state == "valid" for attempt in attempts)
    invalid = sum(attempt.state == "invalid" for attempt in attempts)
    validation_error = sum(attempt.state == "validation_error" for attempt in attempts)
    settled = valid + invalid + validation_error
    if total < settled:
        raise AnalysisUnavailableError("analysis verdict counts are inconsistent")
    return SampleVerdictCounts(
        valid=valid,
        invalid=invalid,
        pending=total - settled,
        validation_error=validation_error,
        total=total,
    )


def _add_verdict_counts(
    left: SampleVerdictCounts,
    right: SampleVerdictCounts,
) -> SampleVerdictCounts:
    return SampleVerdictCounts(
        valid=left.valid + right.valid,
        invalid=left.invalid + right.invalid,
        pending=left.pending + right.pending,
        validation_error=left.validation_error + right.validation_error,
        total=left.total + right.total,
    )


def _report_is_available(
    children: list[ScenarioJob],
    tenant_scenarios: list[ScenarioResult],
    versions: list[ReportVersion],
    *,
    parent_state: str,
) -> bool:
    if len(children) != 3 or len(tenant_scenarios) != 3:
        return False
    try:
        if derive_parent_state(child.state for child in children).value != parent_state:
            return False
    except InvalidAggregateState:
        return False
    tenant_by_id = {scenario.id: scenario for scenario in tenant_scenarios}
    if len(tenant_by_id) != 3:
        return False
    latest: dict[UUID, ReportVersion] = {}
    for version in sorted(
        versions,
        key=lambda item: item.report_version,
        reverse=True,
    ):
        if version.scenario_result_id is not None:
            latest.setdefault(version.scenario_result_id, version)
    for child in children:
        scenario = tenant_by_id.get(child.id)
        if (
            scenario is None
            or scenario.scenario_type != child.scenario_type
            or scenario.state != child.state
            or scenario.failure_code != child.failure_code
        ):
            return False
        if scenario.device_group_id is None and scenario.device_group_reason is None:
            return False
        if scenario.device_group_id is not None and scenario.device_group_reason is not None:
            return False
        version = latest.get(child.id)
        try:
            bundle = _validated_scenario_bundle(
                version=version,
                scenario=scenario,
                scenario_type=child.scenario_type,
            )
        except AnalysisError:
            return False
        if child.state == "completed":
            if bundle is None or bundle.get("bundle_state") != "complete":
                return False
        elif child.state in ("failed", "canceled"):
            if bundle is not None and bundle.get("bundle_state") != "partial":
                return False
            if child.failure_code is None and bundle is None:
                return False
        else:
            return False
    return True


def _copy_public_json(value: object) -> object:
    forbidden = {
        "access_token",
        "authorization",
        "bucket",
        "bucket_name",
        "credential",
        "database_url",
        "db_url",
        "download_url",
        "dsn",
        "object_key",
        "password",
        "presigned_url",
        "put_url",
        "secret",
        "signature",
        "storage_uri",
        "token",
        "url",
        "version_id",
    }
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = (
                re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).casefold()
                if isinstance(key, str)
                else ""
            )
            if not isinstance(key, str) or normalized_key in forbidden:
                raise AnalysisUnavailableError("analysis report contains private data")
            result[key] = _copy_public_json(item)
        return result
    if isinstance(value, list):
        return [_copy_public_json(item) for item in value]
    if isinstance(value, str) and _PRIVATE_REPORT_VALUE.search(value) is not None:
        raise AnalysisUnavailableError("analysis report contains private data")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise AnalysisUnavailableError("analysis report contains unsupported data")


__all__ = [
    "ActiveLeaseView",
    "AnalysisError",
    "AnalysisDeviceUnavailableError",
    "AnalysisIdempotencyConflictError",
    "AnalysisInvalidRequestError",
    "AnalysisNotFoundError",
    "AnalysisQueueLimitError",
    "AnalysisService",
    "AnalysisUnavailableError",
    "AnalysisView",
    "ApplicationMetadataView",
    "CreationReservation",
    "MemoryAnalysisView",
    "ReportNotAvailableError",
    "SQLAlchemyAnalysisRepository",
    "SampleVerdictCounts",
    "ScenarioView",
    "SourceCodeAnalysisView",
    "StaleTaskVersionError",
    "analysis_queued_event_id",
    "canonical_analysis_request_hash",
    "canonical_memory_analysis_request_hash",
    "canonical_trace_analysis_request_hash",
    "scenario_job_id",
    "source_code_analysis_view",
    "trace_analysis_ready_event_id",
]
