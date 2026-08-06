"""Loopback-only PerfPilot runtime for local Trace analysis.

This module deliberately stays separate from the production PostgreSQL/S3/Redis
composition. It implements the browser contract with local files and delegates
the actual Trace work to an independently running SmartPerfetto checkout.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from perfpilot_api.ai.local_report import (
    LocalReportSynthesizer,
    LocalReportUsage,
    LocalSynthesisError,
    build_local_report_synthesizer,
)
from perfpilot_api.ai.synthesis import AISynthesisOutput
from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.engines.smartperfetto_contracts import (
    SmartPerfettoAnalyzeResponse,
    SmartPerfettoCancelResponse,
    SmartPerfettoReportResponse,
    SmartPerfettoStatusResponse,
    SmartPerfettoTraceUploadResponse,
)
from perfpilot_api.engines.smartperfetto_transport import SmartPerfettoTransport
from perfpilot_api.local_device import AdbDeviceProbe, LocalDevice, LocalDeviceProbe
from perfpilot_api.local_analysis_store import LocalAnalysisStore
from perfpilot_api.local_device_capture import (
    LocalAndroidToolchain,
    LocalDeviceCaptureError,
    LocalDeviceCaptureGateway,
    build_local_device_capture_gateway,
    resolve_local_adb,
    resolve_local_android_toolchain,
)
from perfpilot_api.local_memory_analysis import (
    LocalMemoryAnalysisError,
    LocalMemoryAnalysisGateway,
    build_local_memory_analysis_gateway,
)
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.memory_join import (
    AndroidMemoryNormalizationError,
    join_android_memory_result,
    join_unavailable_android_memory,
)
from perfpilot_api.reports.normalizer import (
    NormalizedTraceReport,
    SmartPerfettoNormalizationError,
    normalize_smartperfetto_result,
)
from perfpilot_api.reports.smartperfetto_live_normalizer import (
    normalize_live_smartperfetto_result,
)
from perfpilot_api.reports.projection import (
    AIProjection,
    ProjectionPrivacyError,
    ProjectionQuestionError,
    ProjectionSizeError,
    build_ai_projection,
)
from perfpilot_api.reports.writer import AnalysisReportWriteRequest, compose_analysis_report
from perfpilot_api.services.canonical_result_reader import LoadedCanonicalResult


LOCAL_TEAM_ID = UUID("81000000-0000-4000-8000-000000000001")
LOCAL_USER_ID = UUID("80000000-0000-4000-8000-000000000001")
LOCAL_AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
_SMARTPERFETTO_COMMIT = "1508f99788bfcf18cc861e4bf4f8b472e84240c3"
_ENGINE_IMAGE_DIGEST = "sha256:" + hashlib.sha256(_SMARTPERFETTO_COMMIT.encode()).hexdigest()
_MAX_UPLOAD_BYTES = 5 * 1024**3
_INPUT_KINDS = {
    "trace",
    "memory_evidence",
    "apk",
    "source_archive",
    "mapping",
    "native_symbols",
    "log",
}
_PROFILES = {"auto", "startup", "scroll"}
_ACTIVE_ANALYSIS_STATES = {
    "creating",
    "created",
    "uploading",
    "queued",
    "scheduled",
    "running",
    "analyzing",
}
_TERMINAL_ANALYSIS_STATES = {
    "completed",
    "partially_completed",
    "failed",
    "canceled",
    "deleted",
}
_LOCAL_RECOVERY_NAMESPACE = UUID("e2ac7e9c-50e3-5d78-bd3f-53a56e2b2978")
_LOCAL_DEVICE_NAMESPACE = UUID("06905aa0-0e0a-55c7-b63a-87e7a93775ca")
_LOCAL_APPLICATION_NAMESPACE = UUID("60e4336a-f4c6-5aae-b62d-b54627317ccb")
_EARLIEST_LOCAL_ANALYSIS_TIME = datetime.min.replace(tzinfo=UTC)
_LOGGER = logging.getLogger(__name__)

LocalRunStatus = Literal[
    "pending",
    "running",
    "awaiting_user",
    "completed",
    "failed",
    "cancelled",
    "quota_exceeded",
]
LocalAIRole = Literal["report", "extract", "review", "finalize"]


@dataclass(frozen=True, slots=True)
class LocalEngineRun:
    session_id: str
    run_id: str


class _StaleLocalGeneration(RuntimeError):
    pass


class LocalAnalysisGateway(Protocol):
    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun: ...

    async def status(self, run: LocalEngineRun) -> LocalRunStatus: ...

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult: ...

    async def cancel(self, run: LocalEngineRun) -> None: ...

    async def aclose(self) -> None: ...


async def _local_credential(_reference: SecretStr) -> SecretStr:
    return SecretStr("local-smartperfetto-no-auth")


class SmartPerfettoLocalGateway:
    """Narrow adapter over the pinned workspace-scoped SmartPerfetto API."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:3001",
        workspace_id: str = "default-workspace",
    ) -> None:
        self._workspace_id = workspace_id
        self._transport = SmartPerfettoTransport(
            base_url=base_url,
            credential_reference=SecretStr("local-smartperfetto"),
            credential_resolver=_local_credential,
            max_json_bytes=10 * 1024**2,
        )

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        with trace_path.open("rb") as trace_file:
            uploaded = await self._transport.request_multipart_json(
                f"/api/workspaces/{self._workspace_id}/traces/upload",
                workspace_id=self._workspace_id,
                filename=f"perfpilot-trace-{uuid4()}.pftrace",
                file=trace_file,
            )
        if not 200 <= uploaded.status_code <= 299:
            raise RuntimeError("SmartPerfetto Trace upload failed")
        trace = SmartPerfettoTraceUploadResponse.model_validate(uploaded.payload)
        query = {
            "auto": "Analyze this Android Perfetto trace and identify the most important issues.",
            "startup": "Analyze Android application startup performance and its root causes.",
            "scroll": "Analyze Android scrolling jank and its root causes.",
        }[profile]
        if question:
            query = f"{query}\n\nAdditional analysis context: {question}"
        analyzed = await self._transport.request_json(
            "POST",
            f"/api/workspaces/{self._workspace_id}/agent/analyze",
            workspace_id=self._workspace_id,
            json_body={
                "traceId": trace.trace.id,
                "query": query,
                "options": {"analysisMode": "full"},
            },
        )
        if not 200 <= analyzed.status_code <= 299:
            raise RuntimeError("SmartPerfetto analysis could not start")
        run = SmartPerfettoAnalyzeResponse.model_validate(analyzed.payload)
        return LocalEngineRun(session_id=run.session_id, run_id=run.run_id)

    async def status(self, run: LocalEngineRun) -> LocalRunStatus:
        response = await self._transport.request_json(
            "GET",
            f"/api/workspaces/{self._workspace_id}/agent/{run.session_id}/status",
            workspace_id=self._workspace_id,
        )
        if not 200 <= response.status_code <= 299:
            raise RuntimeError("SmartPerfetto status is unavailable")
        parsed = SmartPerfettoStatusResponse.model_validate(response.payload)
        if parsed.session_id != run.session_id:
            raise RuntimeError("SmartPerfetto session changed")
        return parsed.status

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        response = await self._transport.request_json(
            "GET",
            f"/api/workspaces/{self._workspace_id}/agent/{run.session_id}/report",
            workspace_id=self._workspace_id,
        )
        if not 200 <= response.status_code <= 299:
            raise RuntimeError("SmartPerfetto report is unavailable")
        parsed = SmartPerfettoReportResponse.model_validate(response.payload)
        report = parsed.sanitized_report
        usable = bool(
            isinstance(report.get("summary"), Mapping)
            and str(report["summary"].get("conclusion", "")).strip()
        ) or any(bool(report.get(key)) for key in ("findings", "claimSupport"))
        return EngineResult(
            contract="workspace-agent-v1",
            state="completed" if usable else "insufficient_data",
            payload={"reportId": parsed.report_id, "report": report},
        )

    async def cancel(self, run: LocalEngineRun) -> None:
        response = await self._transport.request_json(
            "POST",
            f"/api/workspaces/{self._workspace_id}/agent/{run.session_id}/cancel",
            workspace_id=self._workspace_id,
        )
        if not 200 <= response.status_code <= 299:
            raise RuntimeError("SmartPerfetto analysis could not be canceled")
        parsed = SmartPerfettoCancelResponse.model_validate(response.payload)
        if parsed.session_id != run.session_id:
            raise RuntimeError("SmartPerfetto session changed")

    async def aclose(self) -> None:
        await self._transport.aclose()


def _validated_checksum(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise ValueError("invalid checksum") from None
    if len(decoded) != hashlib.sha256().digest_size:
        raise ValueError("invalid checksum")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("invalid checksum")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _InputDescriptor(_StrictModel):
    kind: str
    mime: str = Field(min_length=3, max_length=255)
    size: int = Field(gt=0, le=_MAX_UPLOAD_BYTES)
    sha256_b64: str

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in _INPUT_KINDS:
            raise ValueError("invalid input kind")
        return value

    @field_validator("sha256_b64")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        return _validated_checksum(value)


class _CreateTraceAnalysisRequest(_StrictModel):
    schema_version: Literal["1.0"]
    analysis_mode: Literal["trace_upload"]
    analysis_profile: str
    question: str | None = Field(default=None, max_length=2000)
    inputs: list[_InputDescriptor] = Field(min_length=1, max_length=7)

    @field_validator("analysis_profile")
    @classmethod
    def valid_profile(cls, value: str) -> str:
        if value not in _PROFILES:
            raise ValueError("invalid analysis profile")
        return value

    @field_validator("inputs")
    @classmethod
    def valid_inputs(cls, value: list[_InputDescriptor]) -> list[_InputDescriptor]:
        kinds = [item.kind for item in value]
        if "trace" not in kinds or len(kinds) != len(set(kinds)):
            raise ValueError("one Trace and unique input kinds are required")
        return value


class _ApkDescriptor(_StrictModel):
    artifact_kind: Literal["apk"]
    mime: Literal["application/vnd.android.package-archive"]
    size: int = Field(gt=0, le=_MAX_UPLOAD_BYTES)
    sha256_b64: str

    @field_validator("sha256_b64")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        return _validated_checksum(value)


class _CreateDeviceAnalysisRequest(_StrictModel):
    schema_version: Literal["1.0"]
    analysis_mode: Literal["device"]
    device_id: UUID = Field(strict=False)
    scenarios: list[Literal["cold_start", "scroll", "memory_cycle"]]
    apk: _ApkDescriptor

    @field_validator("scenarios")
    @classmethod
    def valid_scenarios(
        cls,
        value: list[Literal["cold_start", "scroll", "memory_cycle"]],
    ) -> list[Literal["cold_start", "scroll", "memory_cycle"]]:
        if value != ["cold_start", "scroll", "memory_cycle"]:
            raise ValueError("fixed device scenarios are required")
        return value


_CreateAnalysisRequest = Annotated[
    _CreateTraceAnalysisRequest | _CreateDeviceAnalysisRequest,
    Field(discriminator="analysis_mode"),
]


class _ReserveUploadRequest(_StrictModel):
    artifact_kind: str
    mime: str = Field(min_length=3, max_length=255)
    size: int = Field(gt=0, le=_MAX_UPLOAD_BYTES)
    sha256_b64: str

    @field_validator("artifact_kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in _INPUT_KINDS:
            raise ValueError("invalid input kind")
        return value

    @field_validator("sha256_b64")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        return _validated_checksum(value)


class _FinalizeUploadRequest(_StrictModel):
    upload_id: str = Field(min_length=1, max_length=128)
    sha256_b64: str
    size: int = Field(gt=0, le=_MAX_UPLOAD_BYTES)

    @field_validator("sha256_b64")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        return _validated_checksum(value)


class _LocalRecoveryRequest(_StrictModel):
    schema_version: Literal["1.0"]
    session_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,256}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,256}$")
    analysis_profile: str
    question: str | None = Field(default=None, max_length=2000)
    trace_size: int = Field(gt=0, le=_MAX_UPLOAD_BYTES)
    trace_sha256_b64: str

    @field_validator("analysis_profile")
    @classmethod
    def valid_profile(cls, value: str) -> str:
        if value not in _PROFILES:
            raise ValueError("invalid analysis profile")
        return value

    @field_validator("trace_sha256_b64")
    @classmethod
    def valid_trace_checksum(cls, value: str) -> str:
        return _validated_checksum(value)


@dataclass(slots=True)
class _LocalInput:
    descriptor: _InputDescriptor
    upload_id: str | None = None
    artifact_id: str | None = None
    finalized: bool = False


@dataclass(slots=True)
class _LocalUpload:
    upload_id: str
    analysis_id: UUID
    kind: str
    mime: str
    size: int
    sha256_b64: str
    token: str
    path: Path
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=1)
    )
    bytes_ready: bool = False


@dataclass(slots=True)
class _LocalAIRound:
    number: int
    role: LocalAIRole
    state: Literal["pending", "running", "completed", "failed"] = "pending"
    attempts: int = 0


def _default_ai_rounds() -> list[_LocalAIRound]:
    return [_LocalAIRound(1, "report")]


def _restore_ai_rounds(value: object) -> list[_LocalAIRound]:
    if not isinstance(value, list):
        raise ValueError("invalid persisted local analysis")
    expected_roles: tuple[LocalAIRole, ...]
    if len(value) == 1:
        expected_roles = ("report",)
    elif len(value) == 3:
        expected_roles = ("extract", "review", "finalize")
    else:
        raise ValueError("invalid persisted local analysis")
    restored: list[_LocalAIRound] = []
    for number, (raw_round, role) in enumerate(
        zip(value, expected_roles, strict=True),
        start=1,
    ):
        if not isinstance(raw_round, Mapping):
            raise ValueError("invalid persisted local analysis")
        raw_number = raw_round.get("round")
        state = raw_round.get("state")
        attempts = raw_round.get("attempts")
        if (
            type(raw_number) is not int
            or raw_number != number
            or raw_round.get("role") != role
            or not isinstance(state, str)
            or state not in {"pending", "running", "completed", "failed"}
            or type(attempts) is not int
            or attempts < 0
        ):
            raise ValueError("invalid persisted local analysis")
        restored.append(
            _LocalAIRound(number, role, state, attempts)  # type: ignore[arg-type]
        )
    return restored


@dataclass(slots=True)
class _LocalAnalysis:
    analysis_id: UUID
    profile: str
    question: str | None
    inputs: dict[str, _LocalInput]
    analysis_mode: Literal["trace_upload", "device"] = "trace_upload"
    device_id: UUID | None = None
    application_version_id: UUID | None = None
    application_metadata: dict[str, object] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    state: str = "created"
    version: int = 1
    generation: int = 1
    report: dict[str, object] | None = None
    failure: dict[str, object] | None = None
    cancel_requested_at: datetime | None = None
    task: asyncio.Task[None] | None = None
    source_run: LocalEngineRun | None = None
    source_rounds: int | None = None
    source_verification: Literal["passed", "failed", "unknown"] = "unknown"
    ai_rounds: list[_LocalAIRound] = field(default_factory=_default_ai_rounds)
    stages: dict[str, str] = field(
        default_factory=lambda: {
            "input_validation": "running",
            "smartperfetto": "pending",
            "perfpilot_ai": "pending",
            "report": "pending",
        }
    )


@dataclass(frozen=True, slots=True)
class _PreparedLocalReport:
    core_document: dict[str, object]
    projection: AIProjection
    projection_failure_code: str | None
    canonical_artifact_id: UUID
    canonical_sha256_b64: str
    normalizer_version: str
    source_report: dict[str, object]


@dataclass(frozen=True, slots=True)
class _NormalizedLocalResult:
    report: NormalizedTraceReport
    artifact_id: UUID
    canonical_sha256_b64: str
    source_report: dict[str, object]


def _parse_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _sha256_b64(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


def _blocked_ai_projection(
    *,
    analysis_id: UUID,
    analysis_profile: str,
    canonical_artifact_id: UUID,
) -> AIProjection:
    document = validate_contract(
        "analysis-projection",
        {
            "schema_version": "1.0",
            "analysis_id": str(analysis_id),
            "analysis_profile": analysis_profile,
            "question": None,
            "source": {
                "engine_id": "smartperfetto",
                "adapter_version": "privacy-blocked",
                "source_contract": "workspace-agent-v1",
                "canonical_artifact_id": str(canonical_artifact_id),
            },
            "scenarios": [],
            "limitations": [],
        },
    )
    payload = canonical_json_bytes(document)
    return AIProjection(
        canonical_bytes=payload,
        sha256_b64=_sha256_b64(payload),
    )


def _public_origin(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    private_hostname = hostname in {"localhost", "127.0.0.1", "::1"}
    if hostname is not None and not private_hostname:
        try:
            private_hostname = ipaddress.ip_address(hostname).is_private
        except ValueError:
            private_hostname = False
    if (
        parsed.scheme != "http"
        or not private_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("local public origin must be loopback or private LAN HTTP")
    return value.rstrip("/")


def _team_device(connected: LocalDevice) -> dict[str, object]:
    serial_suffix = connected.serial[-4:]
    return {
        "device_id": str(uuid5(_LOCAL_DEVICE_NAMESPACE, connected.serial)),
        "agent_id": str(LOCAL_AGENT_ID),
        "agent_name": "本机 ADB",
        "serial_suffix": serial_suffix,
        "manufacturer": connected.manufacturer or None,
        "model": connected.model or None,
        "android_release": connected.android_version or None,
        "api_level": connected.api_level,
        "connection_type": "wifi" if ":" in connected.serial else "usb",
        "adb_state": "device",
        "state": "ready",
        "last_seen_at": datetime.now(UTC).isoformat(),
    }


def _synthesis_from_core(
    core: Mapping[str, object],
    upstream_report: Mapping[str, object],
) -> dict[str, object]:
    scenario_values = core.get("scenario_reports")
    scenarios = scenario_values if isinstance(scenario_values, list) else []
    findings: list[tuple[Mapping[str, object], str]] = []
    metric_groups: list[tuple[str, list[Mapping[str, object]], str | None]] = []
    for raw_scenario in scenarios:
        if not isinstance(raw_scenario, Mapping):
            continue
        scenario_type = str(raw_scenario.get("scenario_type", "startup"))
        scenario_findings = raw_scenario.get("findings")
        first_retest: str | None = None
        if isinstance(scenario_findings, list):
            for raw_finding in scenario_findings:
                if isinstance(raw_finding, Mapping):
                    findings.append((raw_finding, scenario_type))
                    retest = raw_finding.get("retest")
                    if first_retest is None and isinstance(retest, str) and retest.strip():
                        first_retest = retest.strip()[:2000]
        raw_metrics = raw_scenario.get("metrics")
        metrics = [item for item in raw_metrics or [] if isinstance(item, Mapping)]
        metric_groups.append((scenario_type, metrics, first_retest))

    top_findings: list[dict[str, object]] = []
    recommendations: list[dict[str, object]] = []
    for finding, _scenario_type in findings:
        finding_id = finding.get("finding_id")
        evidence = finding.get("evidence_ids")
        if not isinstance(finding_id, str) or not isinstance(evidence, list) or not evidence:
            continue
        evidence_ids = [item for item in evidence if isinstance(item, str)][:20]
        if not evidence_ids:
            continue
        summary = str(finding.get("summary") or finding.get("title") or "性能问题")[:2000]
        if len(top_findings) < 5:
            top_findings.append(
                {
                    "finding_id": finding_id,
                    "evidence_ids": evidence_ids,
                    "user_impact": summary,
                }
            )
        recommendation = finding.get("recommendation")
        action = (
            recommendation.strip()
            if isinstance(recommendation, str) and recommendation.strip()
            else "根据关联证据修复该问题，并在相同场景下复测。"
        )
        severity = str(finding.get("severity", "informational"))
        priority = {"critical": "p0", "warning": "p1", "healthy": "p3"}.get(
            severity, "p2"
        )
        if len(recommendations) < 10:
            recommendations.append(
                {
                    "priority": priority,
                    "title": str(finding.get("title") or "优化性能问题")[:2000],
                    "action": action[:2000],
                    "expected_effect": f"降低“{str(finding.get('title') or '该问题')}”对体验的影响。"[
                        :2000
                    ],
                    "finding_ids": [finding_id],
                    "evidence_ids": evidence_ids,
                }
            )

    retest_plan: list[dict[str, object]] = []
    for scenario_type, metrics, first_retest in metric_groups:
        available = [
            metric
            for metric in metrics
            if metric.get("status") == "available" and isinstance(metric.get("metric_id"), str)
        ]
        if not available or len(retest_plan) >= 5:
            continue
        retest_plan.append(
            {
                "mode": "verify_metric",
                "scenario_type": scenario_type,
                "metric_ids": [str(metric["metric_id"]) for metric in available[:20]],
                "limitation_ids": [],
                "steps": first_retest or "使用相同设备与场景重新采集 Trace，并比较关键指标。",
                "success_condition": (
                    "meet_existing_threshold"
                    if any(metric.get("threshold") is not None for metric in available)
                    else "improve_from_baseline"
                ),
                "failure_condition": "threshold_missed",
            }
        )

    raw_limitations = core.get("limitations")
    limitations = [
        {"limitation_id": item["limitation_id"], "summary": item["summary"]}
        for item in (raw_limitations if isinstance(raw_limitations, list) else [])
        if isinstance(item, Mapping)
        and isinstance(item.get("limitation_id"), str)
        and isinstance(item.get("summary"), str)
    ][:20]
    summary = upstream_report.get("summary")
    conclusion = summary.get("conclusion") if isinstance(summary, Mapping) else None
    executive_summary = (
        conclusion.strip()[:2000]
        if isinstance(conclusion, str) and conclusion.strip()
        else f"SmartPerfetto 已完成分析，确认 {len(top_findings)} 个有证据支持的问题。"
    )
    return validate_contract(
        "synthesis-output",
        {
            "schema_version": "1.0",
            "executive_summary": executive_summary,
            "top_findings": top_findings,
            "recommendations": recommendations,
            "retest_plan": retest_plan,
            "limitations": limitations,
        },
    )


def _normalize_local_smartperfetto_result(
    analysis: _LocalAnalysis,
    result: EngineResult,
    *,
    profile: str,
) -> _NormalizedLocalResult:
    execution_id = uuid4()
    artifact_id = result_artifact_id(execution_id)
    source_input = analysis.inputs[
        "trace" if analysis.analysis_mode == "trace_upload" else "apk"
    ].descriptor
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=LOCAL_TEAM_ID,
            analysis_id=analysis.analysis_id,
            execution_id=execution_id,
            expected_execution_version=1,
            tenant_resource_version=1,
            artifact_id=artifact_id,
            engine_id="smartperfetto",
            adapter_version="1.0.0",
            engine_commit_sha=_SMARTPERFETTO_COMMIT,
            engine_image_digest=_ENGINE_IMAGE_DIGEST,
            attempt_number=1,
            input_manifest_hash=hashlib.sha256(
                source_input.sha256_b64.encode()
            ).hexdigest(),
            config_hash=hashlib.sha256(
                f"{profile}\0{analysis.question or ''}".encode()
            ).hexdigest(),
            result=result,
        )
    )
    loaded = LoadedCanonicalResult(
        team_id=LOCAL_TEAM_ID,
        analysis_id=analysis.analysis_id,
        execution_id=execution_id,
        artifact_id=artifact_id,
        tenant_resource_version=1,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )
    try:
        normalized = normalize_smartperfetto_result(
            loaded,
            analysis_mode=analysis.analysis_mode,
        )
    except SmartPerfettoNormalizationError:
        normalized = normalize_live_smartperfetto_result(
            loaded,
            analysis_mode=analysis.analysis_mode,
        )
    report_payload = result.payload.get("report")
    if not isinstance(report_payload, Mapping):
        raise ValueError("SmartPerfetto report is invalid")
    return _NormalizedLocalResult(
        report=normalized,
        artifact_id=artifact_id,
        canonical_sha256_b64=canonical.checksum_sha256_b64,
        source_report=dict(report_payload),
    )


def _missing_scroll_scenario(analysis_id: UUID) -> tuple[dict[str, object], dict[str, object]]:
    scenario_id = str(uuid5(_LOCAL_RECOVERY_NAMESPACE, f"{analysis_id}:scroll-unavailable"))
    limitation_id = str(
        uuid5(_LOCAL_RECOVERY_NAMESPACE, f"{analysis_id}:scroll-result-unavailable")
    )
    return (
        {
            "scenario_id": scenario_id,
            "scenario_type": "scroll",
            "core_state": "partial",
            "metrics": [],
            "findings": [],
            "evidence": [],
            "trace_health": {
                "parse_status": "failed",
                "trace_start_ns": None,
                "trace_end_ns": None,
                "target_resolution": {
                    "package_name": None,
                    "process_name": None,
                    "upid": None,
                    "pid": None,
                    "main_thread_id": None,
                },
                "measurement_window": {
                    "start_ns": None,
                    "end_ns": None,
                    "coverage": "missing",
                },
                "data_loss": {
                    "buffer_overruns": 0,
                    "ftrace_events_lost": 0,
                    "traced_buf_patches_failed": 0,
                    "incomplete_slices": 0,
                    "boundary_truncations": 0,
                },
                "frame_timeline_coverage": "unavailable",
                "target_display_coverage": "unavailable",
                "refresh_mode_coverage": "unavailable",
            },
            "trace_capabilities": [
                {
                    "name": "smartperfetto_scroll_result",
                    "required": True,
                    "status": "unavailable",
                    "reason": "SmartPerfetto did not produce a usable scroll result.",
                }
            ],
        },
        {
            "limitation_id": limitation_id,
            "code": "smartperfetto.scroll_result_unavailable",
            "summary": "滑动 Trace 已采集，但 SmartPerfetto 未返回可用的滑动分析结果。",
            "evidence_ids": [],
        },
    )


def _merge_local_smartperfetto_reports(
    primary: NormalizedTraceReport,
    secondary: NormalizedTraceReport,
) -> NormalizedTraceReport:
    primary_document = validate_contract("normalized-trace-report", primary.document)
    secondary_document = validate_contract("normalized-trace-report", secondary.document)
    if (
        primary_document["analysis_id"] != secondary_document["analysis_id"]
        or primary_document["analysis_mode"] != "device"
        or secondary_document["analysis_mode"] != "device"
    ):
        raise ValueError("SmartPerfetto device reports cannot be merged")
    selected: dict[str, dict[str, object]] = {}
    for raw in primary_document["scenario_reports"]:
        if isinstance(raw, Mapping):
            selected[str(raw["scenario_type"])] = dict(raw)
    for raw in secondary_document["scenario_reports"]:
        if isinstance(raw, Mapping) and (
            raw.get("scenario_type") == "scroll"
            or str(raw.get("scenario_type")) not in selected
        ):
            selected[str(raw["scenario_type"])] = dict(raw)
    limitations: dict[str, dict[str, object]] = {}
    for raw in [
        *primary_document["limitations"],
        *secondary_document["limitations"],
    ]:
        if isinstance(raw, Mapping):
            limitations[str(raw["limitation_id"])] = dict(raw)
    if "scroll" not in selected:
        scenario, limitation = _missing_scroll_scenario(
            UUID(str(primary_document["analysis_id"]))
        )
        selected["scroll"] = scenario
        limitations[str(limitation["limitation_id"])] = limitation
    order = {"startup": 0, "scroll": 1, "memory_cycle": 2}
    scenarios = sorted(selected.values(), key=lambda item: order[str(item["scenario_type"])])
    merged = {
        **primary_document,
        "core_state": (
            "partial"
            if any(item.get("core_state") == "partial" for item in scenarios)
            else "complete"
        ),
        "scenario_reports": scenarios,
        "limitations": sorted(
            limitations.values(),
            key=lambda item: str(item["limitation_id"]),
        )[:20],
    }
    validated = validate_contract("normalized-trace-report", merged)
    payload = canonical_json_bytes(validated)
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=_sha256_b64(payload),
    )


def _canonical_local_memory_result(
    analysis: _LocalAnalysis,
    result: EngineResult,
    *,
    engine_commit_sha: str,
) -> LoadedCanonicalResult:
    execution_id = uuid4()
    artifact_id = result_artifact_id(execution_id)
    apk = analysis.inputs["apk"].descriptor
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=LOCAL_TEAM_ID,
            analysis_id=analysis.analysis_id,
            execution_id=execution_id,
            expected_execution_version=1,
            tenant_resource_version=1,
            artifact_id=artifact_id,
            engine_id="android_memory",
            adapter_version="1.0.0",
            engine_commit_sha=engine_commit_sha,
            engine_image_digest=(
                "sha256:" + hashlib.sha256(engine_commit_sha.encode()).hexdigest()
            ),
            attempt_number=1,
            input_manifest_hash=hashlib.sha256(apk.sha256_b64.encode()).hexdigest(),
            config_hash=hashlib.sha256(b"auto\0local-device-memory").hexdigest(),
            result=result,
        )
    )
    return LoadedCanonicalResult(
        team_id=LOCAL_TEAM_ID,
        analysis_id=analysis.analysis_id,
        execution_id=execution_id,
        artifact_id=artifact_id,
        tenant_resource_version=1,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )


def _prepare_local_report(
    analysis: _LocalAnalysis,
    result: EngineResult,
    *,
    scroll_result: EngineResult | None = None,
    memory_result: EngineResult | None = None,
    memory_engine_commit_sha: str | None = None,
) -> _PreparedLocalReport:
    primary = _normalize_local_smartperfetto_result(
        analysis,
        result,
        profile=analysis.profile,
    )
    normalized = primary.report
    if analysis.analysis_mode == "device":
        if scroll_result is not None:
            scroll = _normalize_local_smartperfetto_result(
                analysis,
                scroll_result,
                profile="scroll",
            )
            normalized = _merge_local_smartperfetto_reports(normalized, scroll.report)
        else:
            normalized = _merge_local_smartperfetto_reports(normalized, normalized)
        if memory_result is not None and memory_engine_commit_sha is not None:
            try:
                normalized = join_android_memory_result(
                    normalized,
                    _canonical_local_memory_result(
                        analysis,
                        memory_result,
                        engine_commit_sha=memory_engine_commit_sha,
                    ),
                )
            except AndroidMemoryNormalizationError:
                normalized = join_unavailable_android_memory(
                    normalized,
                    reason="result_invalid",
                )
        else:
            normalized = join_unavailable_android_memory(
                normalized,
                reason="result_unavailable",
            )
    normalized_provenance = normalized.document.get("provenance")
    if not isinstance(normalized_provenance, Mapping) or not isinstance(
        normalized_provenance.get("normalizer_version"), str
    ):
        raise ValueError("SmartPerfetto normalization provenance is invalid")
    normalizer_version = normalized_provenance["normalizer_version"]
    projection_failure_code: str | None = None
    try:
        projection = build_ai_projection(
            normalized,
            analysis_profile=analysis.profile,  # type: ignore[arg-type]
            question=analysis.question,
        )
    except ProjectionPrivacyError:
        projection_failure_code = "ai_projection_private_data"
        projection = _blocked_ai_projection(
            analysis_id=analysis.analysis_id,
            analysis_profile=analysis.profile,
            canonical_artifact_id=primary.artifact_id,
        )
    except ProjectionQuestionError:
        projection_failure_code = "ai_projection_invalid_question"
        projection = _blocked_ai_projection(
            analysis_id=analysis.analysis_id,
            analysis_profile=analysis.profile,
            canonical_artifact_id=primary.artifact_id,
        )
    except ProjectionSizeError:
        projection_failure_code = "ai_projection_too_large"
        projection = _blocked_ai_projection(
            analysis_id=analysis.analysis_id,
            analysis_profile=analysis.profile,
            canonical_artifact_id=primary.artifact_id,
        )
    return _PreparedLocalReport(
        core_document=normalized.document,
        projection=projection,
        projection_failure_code=projection_failure_code,
        canonical_artifact_id=primary.artifact_id,
        canonical_sha256_b64=primary.canonical_sha256_b64,
        normalizer_version=normalizer_version,
        source_report=primary.source_report,
    )


def _compose_local_report(
    analysis: _LocalAnalysis,
    prepared: _PreparedLocalReport,
    *,
    generation: int,
    synthesis: AISynthesisOutput | None,
    synthesis_failure_code: str | None,
    rounds: tuple[LocalReportUsage, ...],
    synthesizer: LocalReportSynthesizer | None,
) -> dict[str, object]:
    synthesis_document = synthesis.document if synthesis is not None else None
    synthesis_bytes = synthesis.canonical_bytes if synthesis is not None else None
    generated_at = datetime.now(UTC)
    synthesis_execution_id = uuid4()
    projection_artifact_id = uuid4()
    synthesis_artifact_id = uuid4() if synthesis is not None else None
    prompt_version = (
        synthesizer.prompt_version
        if synthesizer is not None
        else "perfpilot-local-report-v2"
    )
    prompt_checksum = synthesizer.prompt_sha256_b64 if synthesizer is not None else ""
    try:
        _validated_checksum(prompt_checksum)
    except ValueError:
        prompt_checksum = _sha256_b64(prompt_version.encode("utf-8"))
    prompt_tokens = sum(item.prompt_tokens for item in rounds)
    completion_tokens = sum(item.completion_tokens for item in rounds)
    latency_ms = sum(item.latency_ms for item in rounds)
    composed = compose_analysis_report(
        AnalysisReportWriteRequest(
            team_id=LOCAL_TEAM_ID,
            analysis_id=analysis.analysis_id,
            synthesis_execution_id=synthesis_execution_id,
            tenant_resource_version=1,
            generation=generation,
            generated_at=generated_at,
            core_document=prepared.core_document,
            synthesis_document=synthesis_document,
            synthesis_failure_code=synthesis_failure_code,
            canonical_artifact_id=prepared.canonical_artifact_id,
            canonical_sha256_b64=prepared.canonical_sha256_b64,
            projection_artifact_id=projection_artifact_id,
            projection_sha256_b64=prepared.projection.sha256_b64,
            synthesis_artifact_id=synthesis_artifact_id,
            synthesis_sha256_b64=(
                _sha256_b64(synthesis_bytes) if synthesis_bytes is not None else None
            ),
            normalizer_version=prepared.normalizer_version,
            prompt_template_version=prompt_version[:128],
            prompt_template_sha256_b64=prompt_checksum,
            report_worker_image_digest=_ENGINE_IMAGE_DIGEST,
            provider_protocol="openai-compatible",
            provider_name=(synthesizer.provider_name[:128] if synthesizer else "unavailable"),
            model=(synthesizer.model[:128] if synthesizer else "unavailable"),
            prompt_tokens=prompt_tokens if synthesis is not None else None,
            completion_tokens=completion_tokens if synthesis is not None else None,
            total_tokens=(prompt_tokens + completion_tokens) if synthesis is not None else None,
            latency_ms=latency_ms if synthesis is not None else None,
        ),
        report_version=analysis.generation,
    )
    return composed.document


def _source_metadata(
    report: Mapping[str, object],
) -> tuple[int | None, Literal["passed", "failed", "unknown"]]:
    rounds: int | None = None
    summary = report.get("summary")
    if isinstance(summary, Mapping):
        raw_rounds = summary.get("rounds")
        if type(raw_rounds) is int and raw_rounds >= 0:
            rounds = raw_rounds
    verification: Literal["passed", "failed", "unknown"] = "unknown"
    raw_verification = report.get("claimVerificationResult")
    if isinstance(raw_verification, Mapping):
        raw_status = str(raw_verification.get("status", "")).casefold()
        if raw_status in {"passed", "pass", "verified", "success"}:
            verification = "passed"
        elif raw_status in {"failed", "fail", "rejected", "error"}:
            verification = "failed"
    return rounds, verification


class _LocalRuntime:
    def __init__(
        self,
        *,
        gateway: LocalAnalysisGateway,
        synthesizer: LocalReportSynthesizer | None,
        device_probe: LocalDeviceProbe,
        device_capture_gateway: LocalDeviceCaptureGateway | None,
        memory_analysis_gateway: LocalMemoryAnalysisGateway | None,
        data_root: Path,
        public_origin: str,
        poll_interval_seconds: float,
    ) -> None:
        if poll_interval_seconds < 0:
            raise ValueError("poll interval must not be negative")
        self.gateway = gateway
        self.synthesizer = synthesizer
        self.device_probe = device_probe
        self.device_capture_gateway = device_capture_gateway
        self.memory_analysis_gateway = memory_analysis_gateway
        self.data_root = data_root.resolve()
        self.store = LocalAnalysisStore(self.data_root)
        self.public_origin = _public_origin(public_origin)
        self.poll_interval_seconds = poll_interval_seconds
        self.upload_root = self.data_root / "uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.analyses: dict[UUID, _LocalAnalysis] = {}
        self.uploads: dict[str, _LocalUpload] = {}
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[None]] = set()

    def _state_document(self, analysis: _LocalAnalysis) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "analysis_id": str(analysis.analysis_id),
            "analysis_mode": analysis.analysis_mode,
            "device_id": str(analysis.device_id) if analysis.device_id is not None else None,
            "application_version_id": (
                str(analysis.application_version_id)
                if analysis.application_version_id is not None
                else None
            ),
            "application_metadata": analysis.application_metadata,
            "profile": analysis.profile,
            "question": analysis.question,
            "created_at": analysis.created_at.isoformat(),
            "started_at": (
                analysis.started_at.isoformat() if analysis.started_at is not None else None
            ),
            "completed_at": (
                analysis.completed_at.isoformat()
                if analysis.completed_at is not None
                else None
            ),
            "state": analysis.state,
            "version": analysis.version,
            "generation": analysis.generation,
            "inputs": [
                {
                    "descriptor": item.descriptor.model_dump(mode="json"),
                    "upload_id": item.upload_id,
                    "artifact_id": item.artifact_id,
                    "finalized": item.finalized,
                }
                for item in analysis.inputs.values()
            ],
            "failure": analysis.failure,
            "cancel_requested_at": (
                analysis.cancel_requested_at.isoformat()
                if analysis.cancel_requested_at is not None
                else None
            ),
            "stages": dict(analysis.stages),
            "source_run": (
                {
                    "session_id": analysis.source_run.session_id,
                    "run_id": analysis.source_run.run_id,
                }
                if analysis.source_run is not None
                else None
            ),
            "source_rounds": analysis.source_rounds,
            "source_verification": analysis.source_verification,
            "ai_rounds": [
                {
                    "round": item.number,
                    "role": item.role,
                    "state": item.state,
                    "attempts": item.attempts,
                }
                for item in analysis.ai_rounds
            ],
            "report_available": analysis.report is not None,
        }

    async def _persist(self, analysis: _LocalAnalysis) -> None:
        document = self._state_document(analysis)
        await asyncio.to_thread(
            self.store.save_state,
            analysis.analysis_id,
            document,
        )

    def _restore_analysis(self, document: Mapping[str, object]) -> _LocalAnalysis:
        analysis_id = UUID(str(document["analysis_id"]))
        raw_created_at = document.get("created_at")
        created_at = _parse_utc_datetime(raw_created_at)
        if raw_created_at is not None and created_at is None:
            raise ValueError("invalid persisted local analysis")
        raw_inputs = document.get("inputs")
        if not isinstance(raw_inputs, list):
            raise ValueError("invalid persisted local analysis")
        inputs: dict[str, _LocalInput] = {}
        for raw_input in raw_inputs:
            if not isinstance(raw_input, Mapping):
                raise ValueError("invalid persisted local analysis")
            descriptor = _InputDescriptor.model_validate(raw_input.get("descriptor"))
            local_input = _LocalInput(
                descriptor=descriptor,
                upload_id=(
                    str(raw_input["upload_id"])
                    if isinstance(raw_input.get("upload_id"), str)
                    else None
                ),
                artifact_id=(
                    str(raw_input["artifact_id"])
                    if isinstance(raw_input.get("artifact_id"), str)
                    else None
                ),
                finalized=raw_input.get("finalized") is True,
            )
            inputs[descriptor.kind] = local_input
        raw_source = document.get("source_run")
        source_run = None
        if isinstance(raw_source, Mapping):
            source_run = LocalEngineRun(
                session_id=str(raw_source["session_id"]),
                run_id=str(raw_source["run_id"]),
            )
        ai_rounds = (
            _restore_ai_rounds(document["ai_rounds"])
            if "ai_rounds" in document
            else _default_ai_rounds()
        )
        stages = document.get("stages")
        if not isinstance(stages, Mapping):
            raise ValueError("invalid persisted local analysis")
        raw_cancel_requested_at = document.get("cancel_requested_at")
        cancel_requested_at = _parse_utc_datetime(raw_cancel_requested_at)
        if raw_cancel_requested_at is not None and cancel_requested_at is None:
            raise ValueError("invalid persisted local analysis")
        analysis_mode = str(document.get("analysis_mode", "trace_upload"))
        if analysis_mode not in {"trace_upload", "device"}:
            raise ValueError("invalid persisted local analysis")
        raw_started_at = document.get("started_at")
        started_at = _parse_utc_datetime(raw_started_at)
        if raw_started_at is not None and started_at is None:
            raise ValueError("invalid persisted local analysis")
        raw_completed_at = document.get("completed_at")
        completed_at = _parse_utc_datetime(raw_completed_at)
        if raw_completed_at is not None and completed_at is None:
            raise ValueError("invalid persisted local analysis")
        analysis = _LocalAnalysis(
            analysis_id=analysis_id,
            profile=str(document["profile"]),
            question=(
                str(document["question"])
                if isinstance(document.get("question"), str)
                else None
            ),
            inputs=inputs,
            analysis_mode=analysis_mode,  # type: ignore[arg-type]
            device_id=(
                UUID(str(document["device_id"]))
                if document.get("device_id") is not None
                else None
            ),
            application_version_id=(
                UUID(str(document["application_version_id"]))
                if document.get("application_version_id") is not None
                else None
            ),
            application_metadata=(
                dict(document["application_metadata"])
                if isinstance(document.get("application_metadata"), Mapping)
                else None
            ),
            created_at=created_at or _EARLIEST_LOCAL_ANALYSIS_TIME,
            started_at=started_at,
            completed_at=completed_at,
            state=str(document["state"]),
            version=int(document["version"]),
            generation=int(document.get("generation", 1)),
            failure=(
                dict(document["failure"])
                if isinstance(document.get("failure"), Mapping)
                else None
            ),
            cancel_requested_at=cancel_requested_at,
            source_run=source_run,
            source_rounds=(
                int(document["source_rounds"])
                if type(document.get("source_rounds")) is int
                else None
            ),
            source_verification=(
                str(document.get("source_verification", "unknown"))  # type: ignore[arg-type]
            ),
            ai_rounds=ai_rounds,
            stages={key: str(value) for key, value in stages.items()},
        )
        return analysis

    async def start(self) -> None:
        persisted = await asyncio.to_thread(self.store.load_states)
        for analysis_id, document in persisted.items():
            analysis = self._restore_analysis(document)
            migrate_created_at = document.get("created_at") is None
            if analysis.analysis_id != analysis_id:
                raise ValueError("persisted local analysis identity changed")
            if document.get("report_available") is True:
                analysis.report = await asyncio.to_thread(
                    self.store.load_document,
                    analysis.analysis_id,
                    "report.json",
                )
                if migrate_created_at:
                    analysis.created_at = (
                        _parse_utc_datetime(analysis.report.get("generated_at"))
                        or _EARLIEST_LOCAL_ANALYSIS_TIME
                    )
            for item in analysis.inputs.values():
                if item.upload_id is None:
                    continue
                path = self.upload_root / f"{item.upload_id}.bin"
                self.uploads[item.upload_id] = _LocalUpload(
                    upload_id=item.upload_id,
                    analysis_id=analysis.analysis_id,
                    kind=item.descriptor.kind,
                    mime=item.descriptor.mime,
                    size=item.descriptor.size,
                    sha256_b64=item.descriptor.sha256_b64,
                    token=secrets.token_urlsafe(32),
                    path=path,
                    bytes_ready=path.is_file(),
                )
            self.analyses[analysis_id] = analysis
            restore_canceled = (
                analysis.cancel_requested_at is not None
                and analysis.state in _ACTIVE_ANALYSIS_STATES
            )
            if restore_canceled:
                analysis.state = "canceled"
                analysis.failure = None
                for stage_name, stage_state in analysis.stages.items():
                    if stage_state not in {"completed", "failed", "not_requested"}:
                        analysis.stages[stage_name] = "canceled"
                analysis.version += 1
            if migrate_created_at or restore_canceled:
                await self._persist(analysis)

    async def close(self) -> None:
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.gateway.aclose()
        if self.synthesizer is not None:
            await self.synthesizer.aclose()
        if self.memory_analysis_gateway is not None:
            await self.memory_analysis_gateway.aclose()

    async def create(self, request: _CreateAnalysisRequest) -> _LocalAnalysis:
        if isinstance(request, _CreateDeviceAnalysisRequest):
            descriptor = _InputDescriptor(
                kind="apk",
                mime=request.apk.mime,
                size=request.apk.size,
                sha256_b64=request.apk.sha256_b64,
            )
            analysis = _LocalAnalysis(
                analysis_id=uuid4(),
                profile="startup",
                question=None,
                inputs={"apk": _LocalInput(descriptor)},
                analysis_mode="device",
                device_id=request.device_id,
            )
        else:
            analysis = _LocalAnalysis(
                analysis_id=uuid4(),
                profile=request.analysis_profile,
                question=(
                    request.question.strip()
                    if request.question and request.question.strip()
                    else None
                ),
                inputs={item.kind: _LocalInput(item) for item in request.inputs},
            )
        async with self.lock:
            if any(
                current.state in _ACTIVE_ANALYSIS_STATES
                for current in self.analyses.values()
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "analysis already active",
                )
            self.analyses[analysis.analysis_id] = analysis
            if analysis.analysis_mode == "device":
                target = analysis.inputs["apk"]
                upload_id = str(uuid4())
                upload = _LocalUpload(
                    upload_id=upload_id,
                    analysis_id=analysis.analysis_id,
                    kind="apk",
                    mime=target.descriptor.mime,
                    size=target.descriptor.size,
                    sha256_b64=target.descriptor.sha256_b64,
                    token=secrets.token_urlsafe(32),
                    path=self.upload_root / f"{upload_id}.bin",
                )
                self.uploads[upload_id] = upload
                target.upload_id = upload_id
        await self._persist(analysis)
        return analysis

    async def recover(self, request: _LocalRecoveryRequest) -> _LocalAnalysis:
        analysis_id = uuid5(
            _LOCAL_RECOVERY_NAMESPACE,
            f"default-workspace\0{request.session_id}",
        )
        async with self.lock:
            existing = self.analyses.get(analysis_id)
        if existing is not None:
            return existing
        run = LocalEngineRun(session_id=request.session_id, run_id=request.run_id)
        current = await self.gateway.status(run)
        if current != "completed":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "SmartPerfetto analysis is not completed",
            )
        descriptor = _InputDescriptor(
            kind="trace",
            mime="application/octet-stream",
            size=request.trace_size,
            sha256_b64=request.trace_sha256_b64,
        )
        analysis = _LocalAnalysis(
            analysis_id=analysis_id,
            profile=request.analysis_profile,
            question=(
                request.question.strip()
                if request.question and request.question.strip()
                else None
            ),
            inputs={
                "trace": _LocalInput(
                    descriptor=descriptor,
                    artifact_id=str(uuid4()),
                    finalized=True,
                )
            },
            state="analyzing",
            source_run=run,
            stages={
                "input_validation": "completed",
                "smartperfetto": "running",
                "perfpilot_ai": "pending",
                "report": "pending",
            },
        )
        async with self.lock:
            raced = self.analyses.get(analysis_id)
            if raced is not None:
                return raced
            self.analyses[analysis_id] = analysis
        await self._persist(analysis)
        async with self.lock:
            task = asyncio.create_task(
                self._execute_run(
                    analysis,
                    run,
                    generation=analysis.generation,
                )
            )
            analysis.task = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return analysis

    async def rerun_synthesis(self, analysis: _LocalAnalysis) -> int:
        start_gate = asyncio.Event()

        async def execute_reserved(
            run: LocalEngineRun,
            generation: int,
        ) -> None:
            await start_gate.wait()
            await self._execute_run(
                analysis,
                run,
                generation=generation,
            )

        async with self.lock:
            if analysis.source_run is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "SmartPerfetto source analysis is unavailable",
                )
            if analysis.task is not None and not analysis.task.done():
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "analysis is already running",
                )
            source_run = analysis.source_run
            previous_generation = analysis.generation
            previous_state = analysis.state
            previous_failure = (
                dict(analysis.failure) if analysis.failure is not None else None
            )
            previous_stages = dict(analysis.stages)
            previous_ai_rounds = [
                _LocalAIRound(item.number, item.role, item.state, item.attempts)
                for item in analysis.ai_rounds
            ]
            previous_version = analysis.version
            analysis.generation += 1
            analysis.state = "analyzing"
            analysis.failure = None
            analysis.stages["smartperfetto"] = "completed"
            analysis.stages["perfpilot_ai"] = "running"
            analysis.stages["report"] = "pending"
            analysis.ai_rounds = _default_ai_rounds()
            analysis.version += 1
            generation = analysis.generation
            reserved_version = analysis.version
            task = asyncio.create_task(execute_reserved(source_run, generation))
            analysis.task = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        try:
            await self._persist(analysis)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with self.lock:
                if analysis.task is task:
                    analysis.task = None
                    if (
                        analysis.generation == generation
                        and analysis.version == reserved_version
                    ):
                        analysis.generation = previous_generation
                        analysis.state = previous_state
                        analysis.failure = previous_failure
                        analysis.stages = previous_stages
                        analysis.ai_rounds = previous_ai_rounds
                        analysis.version = previous_version
            self.tasks.discard(task)
            raise
        start_gate.set()
        return generation

    async def analysis(self, analysis_id: UUID) -> _LocalAnalysis:
        async with self.lock:
            analysis = self.analyses.get(analysis_id)
        if analysis is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "analysis not found")
        return analysis

    async def report_analyses(self, *, limit: int) -> tuple[_LocalAnalysis, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid list limit")
        async with self.lock:
            available = tuple(
                analysis for analysis in self.analyses.values() if analysis.report is not None
            )
        return tuple(
            sorted(
                available,
                key=lambda analysis: (analysis.created_at, str(analysis.analysis_id)),
                reverse=True,
            )[:limit]
        )

    async def active_analyses(self, *, limit: int) -> tuple[_LocalAnalysis, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid list limit")
        async with self.lock:
            active = tuple(
                analysis
                for analysis in self.analyses.values()
                if analysis.state in _ACTIVE_ANALYSIS_STATES
            )
        return tuple(
            sorted(
                active,
                key=lambda analysis: (analysis.created_at, str(analysis.analysis_id)),
                reverse=True,
            )[:limit]
        )

    async def cancel(
        self,
        analysis: _LocalAnalysis,
    ) -> tuple[_LocalAnalysis, bool]:
        async with self.lock:
            if analysis.state in _TERMINAL_ANALYSIS_STATES:
                return analysis, False
            if analysis.cancel_requested_at is not None:
                return analysis, True
            analysis.cancel_requested_at = datetime.now(UTC)
            analysis.version += 1
            requested_at = analysis.cancel_requested_at
            task = analysis.task
            run = (
                analysis.source_run
                if analysis.stages.get("smartperfetto") == "running"
                else None
            )
        await self._persist(analysis)

        if run is not None:
            try:
                await self.gateway.cancel(run)
            except Exception as error:
                async with self.lock:
                    if (
                        analysis.state in _ACTIVE_ANALYSIS_STATES
                        and analysis.cancel_requested_at == requested_at
                    ):
                        analysis.cancel_requested_at = None
                        analysis.version += 1
                await self._persist(analysis)
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "SmartPerfetto cancellation failed",
                ) from error

        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        async with self.lock:
            if analysis.state in _TERMINAL_ANALYSIS_STATES:
                return analysis, False
            analysis.state = "canceled"
            analysis.failure = None
            for stage_name, stage_state in analysis.stages.items():
                if stage_state not in {"completed", "failed", "not_requested"}:
                    analysis.stages[stage_name] = "canceled"
            analysis.version += 1
        await self._persist(analysis)
        return analysis, True

    async def reserve(
        self,
        analysis: _LocalAnalysis,
        request: _ReserveUploadRequest,
    ) -> _LocalUpload:
        async with self.lock:
            target = analysis.inputs.get(request.artifact_kind)
            if target is None or (
                target.descriptor.mime != request.mime
                or target.descriptor.size != request.size
                or target.descriptor.sha256_b64 != request.sha256_b64
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "upload metadata changed")
            if target.upload_id is not None:
                return self.uploads[target.upload_id]
            upload_id = str(uuid4())
            upload = _LocalUpload(
                upload_id=upload_id,
                analysis_id=analysis.analysis_id,
                kind=request.artifact_kind,
                mime=request.mime,
                size=request.size,
                sha256_b64=request.sha256_b64,
                token=secrets.token_urlsafe(32),
                path=self.upload_root / f"{upload_id}.bin",
            )
            self.uploads[upload_id] = upload
            target.upload_id = upload_id
            analysis.state = "uploading"
            analysis.version += 1
        await self._persist(analysis)
        return upload

    async def put(self, upload_id: str, token: str, request: Request) -> None:
        async with self.lock:
            upload = self.uploads.get(upload_id)
        if upload is None or not hmac.compare_digest(upload.token, token):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
        if request.headers.get("content-type", "").split(";", 1)[0] != upload.mime:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "content type changed")
        if not hmac.compare_digest(
            request.headers.get("x-amz-checksum-sha256", ""), upload.sha256_b64
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "checksum header changed")
        temporary = upload.path.with_suffix(".uploading")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > upload.size:
                        raise HTTPException(status.HTTP_400_BAD_REQUEST, "upload size changed")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            actual = base64.b64encode(digest.digest()).decode("ascii")
            if size != upload.size or not hmac.compare_digest(actual, upload.sha256_b64):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "upload integrity failed")
            os.replace(temporary, upload.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        async with self.lock:
            upload.bytes_ready = True

    async def finalize(
        self,
        analysis: _LocalAnalysis,
        request: _FinalizeUploadRequest,
    ) -> _LocalUpload:
        async with self.lock:
            upload = self.uploads.get(request.upload_id)
            if (
                upload is None
                or upload.analysis_id != analysis.analysis_id
                or upload.size != request.size
                or upload.sha256_b64 != request.sha256_b64
                or not upload.bytes_ready
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "upload is not ready")
            target = analysis.inputs[upload.kind]
            target.finalized = True
            target.artifact_id = target.artifact_id or str(uuid4())
            analysis.version += 1
            ready = all(item.finalized for item in analysis.inputs.values())
            if ready and analysis.task is None:
                analysis.stages["input_validation"] = "completed"
                analysis.stages["smartperfetto"] = "running"
                analysis.state = "analyzing"
                analysis.started_at = analysis.started_at or datetime.now(UTC)
                task = asyncio.create_task(self._execute(analysis))
                analysis.task = task
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        await self._persist(analysis)
        return upload

    async def _execute(self, analysis: _LocalAnalysis) -> None:
        generation = analysis.generation
        try:
            if analysis.analysis_mode == "device":
                await self._execute_device(analysis, generation=generation)
                return
            trace = analysis.inputs["trace"]
            if trace.upload_id is None:
                raise RuntimeError("Trace upload is missing")
            trace_path = self.uploads[trace.upload_id].path
            run = await self.gateway.submit(
                trace_path=trace_path,
                profile=analysis.profile,
                question=analysis.question,
            )
            await self._register_source_run(analysis, run)
            await self._execute_run(analysis, run, generation=generation)
        except asyncio.CancelledError:
            raise
        except _StaleLocalGeneration:
            return
        except Exception as error:
            _LOGGER.exception(
                "Local analysis execution failed type=%s",
                type(error).__name__,
            )
            await self._fail_analysis(analysis, generation=generation)

    async def _execute_device(
        self,
        analysis: _LocalAnalysis,
        *,
        generation: int,
    ) -> None:
        if self.device_capture_gateway is None or analysis.device_id is None:
            raise RuntimeError("local device capture is unavailable")
        detected = await self.device_probe.inspect()
        if (
            detected.state != "connected"
            or detected.device is None
            or UUID(str(_team_device(detected.device)["device_id"])) != analysis.device_id
        ):
            raise RuntimeError("selected Android device is unavailable")
        apk = analysis.inputs["apk"]
        if apk.upload_id is None:
            raise RuntimeError("APK upload is missing")
        apk_path = self.uploads[apk.upload_id].path
        workspace = self.data_root / "device-captures" / str(analysis.analysis_id)
        workspace.mkdir(parents=True, exist_ok=True)
        capture = await self.device_capture_gateway.capture(
            apk_path=apk_path,
            serial=detected.device.serial,
            workspace=workspace,
        )
        metadata = capture.metadata
        memory_task: asyncio.Task[EngineResult | None] | None = None
        if self.memory_analysis_gateway is not None:

            async def analyze_memory() -> EngineResult | None:
                try:
                    return await self.memory_analysis_gateway.analyze(
                        analysis_id=analysis.analysis_id,
                        evidence_path=capture.memory_evidence,
                        package_name=metadata.package_name,
                        android_release=detected.device.android_version or None,
                        api_level=detected.device.api_level,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.warning(
                        "Local Android memory analysis unavailable type=%s",
                        type(error).__name__,
                    )
                    return None

            memory_task = asyncio.create_task(analyze_memory())
        async with self.lock:
            if analysis.cancel_requested_at is not None:
                raise asyncio.CancelledError
            analysis.application_version_id = uuid5(
                _LOCAL_APPLICATION_NAMESPACE,
                (
                    f"{metadata.package_name}\0{metadata.version_code}\0"
                    f"{apk.descriptor.sha256_b64}"
                ),
            )
            analysis.application_metadata = {
                "package_name": metadata.package_name,
                "version_name": metadata.version_name,
                "version_code": metadata.version_code,
                "launch_activity": metadata.launch_activity,
                "min_sdk": metadata.min_sdk,
                "target_sdk": metadata.target_sdk,
                "supported_abis": list(metadata.supported_abis),
                "has_native_libraries": metadata.has_native_libraries,
            }
            analysis.version += 1
        await self._persist(analysis)

        try:
            startup_run = await self.gateway.submit(
                trace_path=capture.startup_trace,
                profile="startup",
                question=None,
            )
            await self._register_source_run(analysis, startup_run)
            startup_result = await self._wait_engine_result(analysis, startup_run)

            scroll_run = await self.gateway.submit(
                trace_path=capture.scroll_trace,
                profile="scroll",
                question=None,
            )
            await self._register_source_run(analysis, scroll_run)
            scroll_result = await self._wait_engine_result(analysis, scroll_run)
            memory_result = await memory_task if memory_task is not None else None
        finally:
            if memory_task is not None and not memory_task.done():
                memory_task.cancel()
                await asyncio.gather(memory_task, return_exceptions=True)
        await self._mark_smartperfetto_completed(analysis, generation=generation)
        if memory_result is not None:
            await asyncio.to_thread(
                self.store.save_document,
                analysis.analysis_id,
                "android-memory-result.json",
                memory_result.payload,
            )
        prepared = _prepare_local_report(
            analysis,
            startup_result,
            scroll_result=scroll_result,
            memory_result=memory_result,
            memory_engine_commit_sha=(
                self.memory_analysis_gateway.engine_commit_sha
                if self.memory_analysis_gateway is not None
                else None
            ),
        )
        await self._publish_prepared(
            analysis,
            prepared,
            generation=generation,
        )

    async def _register_source_run(
        self,
        analysis: _LocalAnalysis,
        run: LocalEngineRun,
    ) -> None:
        async with self.lock:
            canceled = analysis.cancel_requested_at is not None
            if not canceled:
                analysis.source_run = run
                analysis.version += 1
        if canceled:
            await self.gateway.cancel(run)
            raise asyncio.CancelledError
        await self._persist(analysis)

    async def _wait_engine_result(
        self,
        analysis: _LocalAnalysis,
        run: LocalEngineRun,
    ) -> EngineResult:
        while True:
            async with self.lock:
                if analysis.cancel_requested_at is not None:
                    raise asyncio.CancelledError
            current = await self.gateway.status(run)
            if current == "completed":
                return await self.gateway.fetch_result(run)
            if current in {"failed", "cancelled", "quota_exceeded", "awaiting_user"}:
                raise RuntimeError("SmartPerfetto analysis did not complete")
            await asyncio.sleep(self.poll_interval_seconds)

    async def _mark_smartperfetto_completed(
        self,
        analysis: _LocalAnalysis,
        *,
        generation: int,
    ) -> None:
        async with self.lock:
            if analysis.cancel_requested_at is not None:
                raise asyncio.CancelledError
            if analysis.generation != generation:
                raise _StaleLocalGeneration
            analysis.stages["smartperfetto"] = "completed"
            analysis.stages["perfpilot_ai"] = "running"
            analysis.version += 1
        await self._persist(analysis)

    async def _execute_run(
        self,
        analysis: _LocalAnalysis,
        run: LocalEngineRun,
        *,
        generation: int,
    ) -> None:
        try:
            result = await self._wait_engine_result(analysis, run)
            await self._mark_smartperfetto_completed(
                analysis,
                generation=generation,
            )
            prepared = _prepare_local_report(analysis, result)
            await self._publish_prepared(
                analysis,
                prepared,
                generation=generation,
            )
        except asyncio.CancelledError:
            raise
        except _StaleLocalGeneration:
            return
        except Exception as error:
            _LOGGER.exception(
                "Local report execution failed type=%s",
                type(error).__name__,
            )
            await self._fail_analysis(analysis, generation=generation)

    async def _publish_prepared(
        self,
        analysis: _LocalAnalysis,
        prepared: _PreparedLocalReport,
        *,
        generation: int,
    ) -> None:
        async with self.lock:
            if analysis.generation != generation:
                raise _StaleLocalGeneration
            analysis.source_rounds, analysis.source_verification = _source_metadata(
                prepared.source_report
            )
        await asyncio.to_thread(
            self.store.save_document,
            analysis.analysis_id,
            "smartperfetto-report.json",
            prepared.source_report,
        )
        await asyncio.to_thread(
            self.store.save_document,
            analysis.analysis_id,
            "projection.json",
            prepared.projection.document,
        )
        synthesis: AISynthesisOutput | None = None
        rounds: tuple[LocalReportUsage, ...] = ()
        synthesis_failure_code: str | None = None
        try:
            if prepared.projection_failure_code is not None:
                raise LocalSynthesisError(
                    prepared.projection_failure_code,
                    retryable=False,
                )
            if self.synthesizer is None:
                raise LocalSynthesisError("ai_not_configured", retryable=False)

            async def observe_report(
                number: int,
                role: Literal["report"],
                state: Literal["running", "completed", "failed"],
                attempts: int,
                output: AISynthesisOutput | None,
            ) -> None:
                async with self.lock:
                    if analysis.generation != generation:
                        raise _StaleLocalGeneration
                    if not 1 <= number <= len(analysis.ai_rounds):
                        raise LocalSynthesisError(
                            "ai_state_invalid",
                            retryable=False,
                        )
                    round_state = analysis.ai_rounds[number - 1]
                    if round_state.role != role:
                        raise LocalSynthesisError(
                            "ai_state_invalid",
                            retryable=False,
                        )
                    round_state.state = state
                    round_state.attempts = attempts
                    analysis.version += 1
                if output is not None:
                    await asyncio.to_thread(
                        self.store.save_document,
                        analysis.analysis_id,
                        "round-1.json",
                        output.document,
                    )
                await self._persist(analysis)

            synthesis_result = await self.synthesizer.synthesize(
                prepared.projection,
                on_report=observe_report,
            )
            synthesis = synthesis_result.output
            rounds = synthesis_result.rounds
            async with self.lock:
                if analysis.generation != generation:
                    raise _StaleLocalGeneration
                analysis.stages["perfpilot_ai"] = "completed"
                analysis.failure = None
                analysis.version += 1
        except LocalSynthesisError as error:
            synthesis_failure_code = error.stable_code
            _LOGGER.warning(
                "Local AI synthesis failed code=%s detail=%s round=%s",
                error.stable_code,
                error.detail_code,
                error.round_number,
            )
            async with self.lock:
                if analysis.generation != generation:
                    raise _StaleLocalGeneration
                analysis.stages["perfpilot_ai"] = "failed"
                analysis.failure = {
                    "code": error.stable_code,
                    "message": "PerfPilot AI 最终报告生成失败，SmartPerfetto 基础报告仍可查看",
                    "retryable": error.retryable,
                }
                analysis.version += 1
        async with self.lock:
            if analysis.generation != generation:
                raise _StaleLocalGeneration
        report = _compose_local_report(
            analysis,
            prepared,
            generation=generation,
            synthesis=synthesis,
            synthesis_failure_code=synthesis_failure_code,
            rounds=rounds,
            synthesizer=self.synthesizer,
        )
        await asyncio.to_thread(
            self.store.save_document,
            analysis.analysis_id,
            "report.json",
            report,
        )
        async with self.lock:
            if analysis.cancel_requested_at is not None:
                raise asyncio.CancelledError
            if analysis.generation != generation:
                raise _StaleLocalGeneration
            analysis.report = report
            analysis.state = str(report["state"])
            analysis.completed_at = datetime.now(UTC)
            analysis.stages["report"] = "completed"
            analysis.version += 1
        await self._persist(analysis)

    async def _fail_analysis(
        self,
        analysis: _LocalAnalysis,
        *,
        generation: int,
    ) -> None:
        failure = {
            "code": "local_analysis_failed",
            "message": "本地 SmartPerfetto 分析未能完成",
            "retryable": True,
        }
        async with self.lock:
            if analysis.generation != generation:
                return
            if analysis.cancel_requested_at is not None or analysis.state == "canceled":
                return
            analysis.state = "failed"
            analysis.failure = failure
            if analysis.stages["smartperfetto"] != "completed":
                analysis.stages["smartperfetto"] = "failed"
            else:
                analysis.stages["perfpilot_ai"] = "failed"
            analysis.stages["report"] = "not_requested"
            analysis.version += 1
        await self._persist(analysis)

    def response(self, analysis: _LocalAnalysis) -> dict[str, object]:
        if analysis.analysis_mode == "device":
            target = analysis.inputs["apk"]
            if target.upload_id is None:
                raise RuntimeError("local APK upload is unavailable")
            upload = self.uploads[target.upload_id]
            verdicts = {
                "valid": 0,
                "invalid": 0,
                "pending": 0,
                "validation_error": 0,
                "total": 0,
            }
            if analysis.state in {"creating", "created", "uploading"}:
                scenario_state = "awaiting_input"
                scenario_version = None
            elif analysis.state in {"queued", "scheduled"}:
                scenario_state = analysis.state
                scenario_version = analysis.version
            elif analysis.state in {"running", "analyzing"}:
                scenario_state = "analyzing"
                scenario_version = analysis.version
            elif analysis.state == "partially_completed":
                scenario_state = "completed"
                scenario_version = analysis.version
            else:
                scenario_state = analysis.state
                scenario_version = analysis.version
            report_scenarios: dict[str, Mapping[str, object]] = {}
            if analysis.report is not None:
                raw_scenarios = analysis.report.get("scenario_reports")
                if isinstance(raw_scenarios, list):
                    report_scenarios = {
                        str(item["scenario_type"]): item
                        for item in raw_scenarios
                        if isinstance(item, Mapping)
                        and item.get("scenario_type")
                        in {"startup", "scroll", "memory_cycle"}
                    }

            def render_scenario(scenario_type: str) -> dict[str, object]:
                report_type = "startup" if scenario_type == "cold_start" else scenario_type
                reported = report_scenarios.get(report_type)
                reported_state = (
                    str(reported["result_state"])
                    if reported is not None
                    and reported.get("result_state")
                    in {"completed", "failed", "canceled"}
                    else scenario_state
                )
                raw_failure = reported.get("failure") if reported is not None else None
                failure = (
                    dict(raw_failure)
                    if isinstance(raw_failure, Mapping)
                    else analysis.failure if reported_state == "failed" else None
                )
                return {
                    "scenario_job_id": (
                        reported.get("scenario_job_id") if reported is not None else None
                    ),
                    "scenario_type": scenario_type,
                    "state": reported_state,
                    "version": scenario_version,
                    "device_group_id": (
                        reported.get("device_group_id") if reported is not None else None
                    ),
                    "sample_verdict_counts": dict(verdicts),
                    "started_at": (
                        analysis.started_at.isoformat()
                        if analysis.started_at is not None
                        else None
                    ),
                    "completed_at": (
                        analysis.completed_at.isoformat()
                        if analysis.completed_at is not None
                        else None
                    ),
                    "failure": failure,
                }

            return {
                "schema_version": "1.0",
                "analysis_id": str(analysis.analysis_id),
                "team_id": str(LOCAL_TEAM_ID),
                "analysis_mode": "device",
                "device_id": str(analysis.device_id),
                "state": analysis.state,
                "version": analysis.version,
                "application_version_id": (
                    str(analysis.application_version_id)
                    if analysis.application_version_id is not None
                    else None
                ),
                "application_metadata": analysis.application_metadata,
                "apk_upload": self.slot(upload, finalized=target.finalized)["upload"],
                "scenarios": [
                    render_scenario(scenario_type)
                    for scenario_type in ("cold_start", "scroll", "memory_cycle")
                ],
                "sample_verdict_counts": verdicts,
                "active_lease": None,
                "report_available": analysis.report is not None,
                "created_at": analysis.created_at.isoformat(),
                "started_at": (
                    analysis.started_at.isoformat()
                    if analysis.started_at is not None
                    else None
                ),
                "completed_at": (
                    analysis.completed_at.isoformat()
                    if analysis.completed_at is not None
                    else None
                ),
                "cancel_requested_at": (
                    analysis.cancel_requested_at.isoformat()
                    if analysis.cancel_requested_at is not None
                    else None
                ),
                "failure": analysis.failure,
            }
        stage_failure = analysis.failure
        input_uploads: list[dict[str, object]] = []
        for item in analysis.inputs.values():
            state = "finalized" if item.finalized else "pending" if item.upload_id else "awaiting_upload"
            rendered: dict[str, object] = {
                "state": state,
                "artifact_kind": item.descriptor.kind,
                "mime": item.descriptor.mime,
                "size": item.descriptor.size,
                "sha256_b64": item.descriptor.sha256_b64,
            }
            if item.upload_id:
                rendered["upload_id"] = item.upload_id
            if item.artifact_id:
                rendered["artifact_id"] = item.artifact_id
            input_uploads.append(rendered)
        stages = []
        for name in ("input_validation", "smartperfetto", "perfpilot_ai", "report"):
            stage_state = analysis.stages[name]
            stages.append(
                {
                    "stage": name,
                    "state": stage_state,
                    "failure": stage_failure if stage_state == "failed" else None,
                }
            )
        return {
            "schema_version": "1.0",
            "analysis_id": str(analysis.analysis_id),
            "team_id": str(LOCAL_TEAM_ID),
            "analysis_mode": "trace_upload",
            "analysis_profile": analysis.profile,
            "question": analysis.question,
            "created_at": analysis.created_at.isoformat(),
            "state": analysis.state,
            "version": analysis.version,
            "cancel_requested_at": (
                analysis.cancel_requested_at.isoformat()
                if analysis.cancel_requested_at is not None
                else None
            ),
            "report_available": analysis.report is not None,
            "stages": stages,
            "input_uploads": input_uploads,
            "failure": analysis.failure,
            "ai_rounds": [
                {
                    "round": item.number,
                    "role": item.role,
                    "state": item.state,
                    "attempts": item.attempts,
                }
                for item in analysis.ai_rounds
            ],
            "source_analysis": {
                "engine": "smartperfetto",
                "rounds": analysis.source_rounds,
                "verification": analysis.source_verification,
                "session_id": (
                    analysis.source_run.session_id if analysis.source_run else None
                ),
                "run_id": analysis.source_run.run_id if analysis.source_run else None,
            },
        }

    def slot(self, upload: _LocalUpload, *, finalized: bool) -> dict[str, object]:
        target = self.analyses[upload.analysis_id].inputs[upload.kind]
        result: dict[str, object] = {
            "state": "finalized" if finalized else "pending",
            "upload_id": upload.upload_id,
            "artifact_kind": upload.kind,
            "mime": upload.mime,
            "size": upload.size,
            "sha256_b64": upload.sha256_b64,
        }
        if finalized:
            result["artifact_id"] = target.artifact_id
            result["finalized_at"] = datetime.now(UTC).isoformat()
        else:
            result["expires_at"] = upload.expires_at.isoformat()
            result["put_url"] = (
                f"{self.public_origin}/local/v1/uploads/{upload.upload_id}?token={upload.token}"
            )
            result["required_headers"] = {
                "Content-Type": upload.mime,
                "x-amz-checksum-sha256": upload.sha256_b64,
            }
        return {"schema_version": "1.0", "upload": result}


def create_local_app(
    *,
    gateway: LocalAnalysisGateway | None = None,
    synthesizer: LocalReportSynthesizer | None = None,
    device_probe: LocalDeviceProbe | None = None,
    device_capture_gateway: LocalDeviceCaptureGateway | None = None,
    memory_analysis_gateway: LocalMemoryAnalysisGateway | None = None,
    data_root: Path | None = None,
    public_origin: str | None = None,
    poll_interval_seconds: float = 2.0,
) -> FastAPI:
    resolved_gateway = gateway or SmartPerfettoLocalGateway(
        base_url=os.getenv("PERFPILOT_LOCAL_SMARTPERFETTO_URL", "http://127.0.0.1:3001")
    )
    resolved_synthesizer = synthesizer or build_local_report_synthesizer()
    resolved_data_root = data_root or Path(
        os.getenv("PERFPILOT_LOCAL_DATA_DIR", ".perfpilot/local-runtime")
    )
    resolved_device_probe = device_probe
    if resolved_device_probe is None:
        try:
            adb_binary = resolve_local_adb()
        except LocalDeviceCaptureError as error:
            _LOGGER.warning("Local Android toolchain unavailable code=%s", error.code)
            resolved_device_probe = AdbDeviceProbe()
        else:
            resolved_device_probe = AdbDeviceProbe(adb_path=str(adb_binary))
    resolved_device_capture_gateway = device_capture_gateway
    if resolved_device_capture_gateway is None:
        toolchain: LocalAndroidToolchain | None = None
        try:
            toolchain = resolve_local_android_toolchain()
        except LocalDeviceCaptureError as error:
            _LOGGER.warning("Local Android capture unavailable code=%s", error.code)
        if toolchain is not None:
            resolved_device_capture_gateway = build_local_device_capture_gateway(
                toolchain=toolchain,
            )
    resolved_memory_analysis_gateway = memory_analysis_gateway
    if resolved_memory_analysis_gateway is None:
        try:
            resolved_memory_analysis_gateway = build_local_memory_analysis_gateway(
                data_root=resolved_data_root,
            )
        except LocalMemoryAnalysisError as error:
            _LOGGER.warning("Local Android Memory unavailable code=%s", error.code)
    runtime = _LocalRuntime(
        gateway=resolved_gateway,
        synthesizer=resolved_synthesizer,
        device_probe=resolved_device_probe,
        device_capture_gateway=resolved_device_capture_gateway,
        memory_analysis_gateway=resolved_memory_analysis_gateway,
        data_root=resolved_data_root,
        public_origin=public_origin
        or os.getenv("PERFPILOT_LOCAL_API_ORIGIN", "http://localhost:8000"),
        poll_interval_seconds=poll_interval_seconds,
    )
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()

    app = FastAPI(lifespan=lifespan)
    app.state.local_runtime = runtime
    allowed_web_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
    configured_web_origin = os.getenv("PERFPILOT_LOCAL_WEB_ORIGIN")
    if configured_web_origin:
        allowed_web_origins.append(_public_origin(configured_web_origin))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_web_origins,
        allow_credentials=False,
        allow_methods=["PUT", "OPTIONS"],
        allow_headers=["Content-Type", "x-amz-checksum-sha256"],
    )

    def check_team(team_id: UUID) -> None:
        if team_id != LOCAL_TEAM_ID:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "team not found")

    def check_csrf(value: str | None) -> None:
        if value is None or not hmac.compare_digest(value, csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf token is invalid")

    @app.get("/v1/health")
    async def health() -> dict[str, object]:
        return {"schema_version": "1.0", "status": "ready", "runtime": "local"}

    @app.get("/v1/auth/csrf")
    async def csrf() -> dict[str, str]:
        return {"schema_version": "1.0", "csrf_token": csrf_token}

    @app.get("/v1/me")
    async def me() -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "user": {
                "id": str(LOCAL_USER_ID),
                "username": "ray_wu",
                "is_platform_admin": True,
            },
            "memberships": [
                {
                    "id": str(uuid4()),
                    "team": {"id": str(LOCAL_TEAM_ID), "name": "本地测试"},
                    "role": "owner",
                }
            ],
        }

    @app.get("/v1/device")
    async def device() -> dict[str, object]:
        detected = await resolved_device_probe.inspect()
        if detected.device is None:
            return {
                "schema_version": "1.0",
                "state": detected.state,
                "device": None,
            }
        connected = detected.device
        name_parts = [connected.manufacturer, connected.model]
        display_name = " ".join(part for part in name_parts if part).strip() or connected.serial
        os_name = (
            f"Android {connected.android_version}"
            if connected.android_version
            else (
                f"Android API {connected.api_level}"
                if connected.api_level is not None
                else "Android"
            )
        )
        return {
            "schema_version": "1.0",
            "state": detected.state,
            "device": {
                "serial": connected.serial,
                "manufacturer": connected.manufacturer,
                "model": connected.model,
                "name": display_name,
                "os": os_name,
                "api_level": connected.api_level,
            },
        }

    @app.get("/v1/teams/{team_id}/devices")
    async def team_devices(team_id: UUID) -> dict[str, object]:
        check_team(team_id)
        detected = await resolved_device_probe.inspect()
        devices = (
            [_team_device(detected.device)]
            if detected.state == "connected" and detected.device is not None
            else []
        )
        return {"schema_version": "1.0", "devices": devices}

    @app.post("/v1/teams/{team_id}/analyses", status_code=status.HTTP_201_CREATED)
    async def create_analysis(
        team_id: UUID,
        body: _CreateAnalysisRequest,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        check_team(team_id)
        check_csrf(x_csrf_token)
        if isinstance(body, _CreateDeviceAnalysisRequest):
            detected = await resolved_device_probe.inspect()
            if (
                detected.state != "connected"
                or detected.device is None
                or UUID(str(_team_device(detected.device)["device_id"])) != body.device_id
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "selected Android device is unavailable",
                )
        return runtime.response(await runtime.create(body))

    @app.get("/v1/teams/{team_id}/analyses")
    async def list_analyses(
        team_id: UUID,
        analysis_status: Annotated[
            Literal["active"] | None,
            Query(alias="status"),
        ] = None,
        report_available: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 1,
    ) -> dict[str, object]:
        check_team(team_id)
        if analysis_status == "active":
            if report_available is not None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "status and report_available cannot be combined",
                )
            analyses = await runtime.active_analyses(limit=limit)
        elif report_available is not None and not report_available:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "report_available must be true",
            )
        else:
            analyses = await runtime.report_analyses(limit=limit)
        return {
            "schema_version": "1.0",
            "analyses": [runtime.response(analysis) for analysis in analyses],
        }

    @app.post(
        "/v1/teams/{team_id}/local-recoveries",
        status_code=status.HTTP_201_CREATED,
    )
    async def recover_local_analysis(
        team_id: UUID,
        body: _LocalRecoveryRequest,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        check_team(team_id)
        check_csrf(x_csrf_token)
        return runtime.response(await runtime.recover(body))

    @app.post(
        "/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
        status_code=status.HTTP_201_CREATED,
    )
    async def reserve_upload(
        team_id: UUID,
        analysis_id: UUID,
        body: _ReserveUploadRequest,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        check_team(team_id)
        check_csrf(x_csrf_token)
        analysis = await runtime.analysis(analysis_id)
        upload = await runtime.reserve(analysis, body)
        return runtime.slot(upload, finalized=False)

    @app.put("/local/v1/uploads/{upload_id}")
    async def put_upload(
        upload_id: str,
        request: Request,
        token: Annotated[str, Query(min_length=32, max_length=128)],
    ) -> Response:
        await runtime.put(upload_id, token, request)
        return Response(status_code=status.HTTP_200_OK)

    @app.post("/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload")
    async def finalize_upload(
        team_id: UUID,
        analysis_id: UUID,
        body: _FinalizeUploadRequest,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        check_team(team_id)
        check_csrf(x_csrf_token)
        analysis = await runtime.analysis(analysis_id)
        upload = await runtime.finalize(analysis, body)
        return runtime.slot(upload, finalized=True)

    @app.get("/v1/teams/{team_id}/analyses/{analysis_id}")
    async def read_analysis(
        team_id: UUID,
        analysis_id: UUID,
    ) -> dict[str, object]:
        check_team(team_id)
        return runtime.response(await runtime.analysis(analysis_id))

    @app.post("/v1/teams/{team_id}/analyses/{analysis_id}/cancel")
    async def cancel_analysis(
        team_id: UUID,
        analysis_id: UUID,
        http_response: Response,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        check_team(team_id)
        check_csrf(x_csrf_token)
        analysis, accepted = await runtime.cancel(await runtime.analysis(analysis_id))
        http_response.status_code = (
            status.HTTP_202_ACCEPTED if accepted else status.HTTP_200_OK
        )
        return runtime.response(analysis)

    @app.get("/v1/teams/{team_id}/analyses/{analysis_id}/report")
    async def read_report(team_id: UUID, analysis_id: UUID) -> dict[str, object]:
        check_team(team_id)
        analysis = await runtime.analysis(analysis_id)
        if analysis.report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "report not ready")
        return analysis.report

    @app.post(
        "/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
        status_code=status.HTTP_201_CREATED,
    )
    async def rerun_synthesis(
        team_id: UUID,
        analysis_id: UUID,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        check_team(team_id)
        check_csrf(x_csrf_token)
        analysis = await runtime.analysis(analysis_id)
        generation = await runtime.rerun_synthesis(analysis)
        return {
            "schema_version": "1.0",
            "analysis_id": str(analysis_id),
            "generation": generation,
            "state": "queued",
        }

    return app


def run() -> None:
    uvicorn.run(
        "perfpilot_api.local_app:create_local_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
    )


__all__ = [
    "LocalAnalysisGateway",
    "LocalEngineRun",
    "SmartPerfettoLocalGateway",
    "create_local_app",
    "run",
]
