"""Compose and atomically publish immutable AnalysisReport 1.1 documents."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping
from uuid import UUID, uuid5

from sqlalchemy import func, select

from perfpilot_api.db.tenant.models import Analysis, ReportVersion
from perfpilot_api.db.tenant.router import TenantRouter
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract


_PUBLIC_REPORT_ITEM_NAMESPACE = UUID("03cff422-a02c-57ec-bb96-f5b8848d5bdb")
_REPORT_VERSION_NAMESPACE = UUID("876132ea-ed61-56de-9ba4-70d0507def52")
_BUNDLE_NAMESPACE = UUID("0b50e5e2-920a-5295-ab31-9ea9c4a689ab")
_SCENARIO_ORDER = {"startup": 0, "scroll": 1, "memory_cycle": 2}
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")


class ReportWriterError(RuntimeError):
    """Stable report publication failure."""


class ReportIntegrityError(ReportWriterError):
    """The deterministic report identity already names different bytes."""


class ReportSourceError(ReportWriterError):
    """The report inputs or routed tenant authority are invalid."""


@dataclass(frozen=True, slots=True)
class AnalysisReportWriteRequest:
    team_id: UUID
    analysis_id: UUID
    synthesis_execution_id: UUID
    tenant_resource_version: int
    generation: int
    generated_at: datetime
    core_document: Mapping[str, object]
    synthesis_document: Mapping[str, object] | None
    synthesis_failure_code: str | None
    canonical_artifact_id: UUID
    canonical_sha256_b64: str
    projection_artifact_id: UUID
    projection_sha256_b64: str
    synthesis_artifact_id: UUID | None
    synthesis_sha256_b64: str | None
    normalizer_version: str
    prompt_template_version: str
    prompt_template_sha256_b64: str
    report_worker_image_digest: str
    provider_protocol: str
    provider_name: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None


@dataclass(frozen=True, slots=True)
class ComposedAnalysisReport:
    id: UUID
    document: dict[str, object]
    canonical_bytes: bytes
    sha256_b64: str
    tenant_provenance: dict[str, object]


@dataclass(frozen=True, slots=True)
class PublishedAnalysisReport:
    id: UUID
    analysis_id: UUID
    report_version: int
    state: str
    generated_at: datetime
    sha256_b64: str
    document: dict[str, object]


def public_item_id(synthesis_execution_id: UUID, kind: str, index: int) -> UUID:
    if not isinstance(synthesis_execution_id, UUID) or not kind or type(index) is not int or index < 0:
        raise ValueError("public report identity is invalid")
    return uuid5(
        _PUBLIC_REPORT_ITEM_NAMESPACE,
        f"{synthesis_execution_id}:{kind}:{index}",
    )


def report_version_id(synthesis_execution_id: UUID) -> UUID:
    if not isinstance(synthesis_execution_id, UUID):
        raise ValueError("report identity is invalid")
    return uuid5(_REPORT_VERSION_NAMESPACE, str(synthesis_execution_id))


def _checksum(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise ReportSourceError("report source is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ReportSourceError("report source is invalid")
    return value


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReportSourceError("report source is invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _not_applicable(reason: str) -> dict[str, object]:
    return {
        "status": "not_applicable",
        "identifier": None,
        "version": None,
        "sha256_b64": None,
        "reason": reason,
    }


def _bundle(
    *,
    request: AnalysisReportWriteRequest,
    core: Mapping[str, object],
    scenario: Mapping[str, object],
    index: int,
) -> dict[str, object]:
    scenario_id = scenario.get("scenario_id")
    scenario_type = scenario.get("scenario_type")
    core_state = scenario.get("core_state")
    if (
        not isinstance(scenario_id, str)
        or scenario_type not in _SCENARIO_ORDER
        or core_state not in {"complete", "partial"}
    ):
        raise ReportSourceError("report source is invalid")
    provenance = core.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ReportSourceError("report source is invalid")
    adapter_version = provenance.get("adapter_version")
    result_contract_version = provenance.get("result_contract_version")
    engine_image_digest = provenance.get("engine_image_digest")
    source_contract = provenance.get("source_contract")
    if not all(
        isinstance(value, str) and value
        for value in (
            adapter_version,
            result_contract_version,
            engine_image_digest,
            source_contract,
        )
    ):
        raise ReportSourceError("report source is invalid")
    metrics = scenario.get("metrics")
    findings = scenario.get("findings")
    evidence = scenario.get("evidence")
    capabilities = scenario.get("trace_capabilities")
    health = scenario.get("trace_health")
    if not all(isinstance(value, list) for value in (metrics, findings, evidence, capabilities)) or not isinstance(health, Mapping):
        raise ReportSourceError("report source is invalid")
    sample_ids = sorted(
        {
            sample_id
            for metric in metrics
            if isinstance(metric, Mapping)
            for sample_id in metric.get("sample_ids", [])
            if isinstance(sample_id, str)
        }
    )
    checksum = request.canonical_sha256_b64
    return {
        "schema_version": "1.0",
        "bundle_id": str(uuid5(_BUNDLE_NAMESPACE, f"{request.synthesis_execution_id}:{index}")),
        "scenario_job_id": scenario_id,
        "scenario_type": scenario_type,
        "bundle_state": core_state,
        "valid_measurement": core_state == "complete",
        "validity_reasons": [] if core_state == "complete" else ["insufficient_data"],
        "sample_ids": sample_ids[:10],
        "generated_at": _utc(request.generated_at),
        "metrics": metrics,
        "findings": findings,
        "evidence": evidence,
        "artifacts": [],
        "trace_health": dict(health),
        "trace_capabilities": capabilities,
        "provenance": {
            "input_artifacts": [
                {
                    "artifact_id": str(request.canonical_artifact_id),
                    "artifact_kind": "engine_result",
                    "sha256_b64": checksum,
                }
            ],
            "capture_manifest": _not_applicable("canonical_engine_result"),
            "apk": _not_applicable("canonical_engine_result"),
            "device": _not_applicable("canonical_engine_result"),
            "agent": {
                "status": "present",
                "identifier": "smartperfetto",
                "version": adapter_version,
                "sha256_b64": None,
                "reason": None,
            },
            "worker_container_image_digest": engine_image_digest,
            "tracekit_version": adapter_version,
            "tracekit_sha256_b64": checksum,
            "trace_processor_version": result_contract_version,
            "trace_processor_sha256_b64": checksum,
            "sql_bundle_version": source_contract,
            "sql_bundle_sha256_b64": checksum,
            "analysis_bundle_schema_version": "1.0",
            "analysis_bundle_sha256_b64": checksum,
            "rules_version": request.normalizer_version,
            "rules_sha256_b64": checksum,
            "scenario_recipe_version": 1,
            "scenario_recipe_sha256_b64": checksum,
            "query_parameters": [],
        },
    }


def compose_analysis_report(
    request: AnalysisReportWriteRequest,
    *,
    report_version: int,
) -> ComposedAnalysisReport:
    if (
        not isinstance(request.team_id, UUID)
        or not isinstance(request.analysis_id, UUID)
        or not isinstance(request.synthesis_execution_id, UUID)
        or type(request.tenant_resource_version) is not int
        or request.tenant_resource_version < 1
        or type(request.generation) is not int
        or request.generation < 1
        or type(report_version) is not int
        or report_version < 1
        or request.latency_ms is not None
        and (type(request.latency_ms) is not int or request.latency_ms < 0)
    ):
        raise ReportSourceError("report source is invalid")
    generated_at = _utc(request.generated_at)
    for value in (
        request.canonical_sha256_b64,
        request.projection_sha256_b64,
        request.prompt_template_sha256_b64,
    ):
        _checksum(value)
    core = validate_contract("normalized-trace-report", request.core_document)
    provenance = core.get("provenance")
    analysis_mode = core.get("analysis_mode")
    if (
        core.get("analysis_id") != str(request.analysis_id)
        or analysis_mode not in {"trace_upload", "device"}
        or not isinstance(provenance, Mapping)
        or provenance.get("canonical_artifact_id") != str(request.canonical_artifact_id)
        or provenance.get("canonical_sha256_b64") != request.canonical_sha256_b64
        or provenance.get("normalizer_version") != request.normalizer_version
    ):
        raise ReportSourceError("report source is invalid")
    raw_scenarios = core.get("scenario_reports")
    if not isinstance(raw_scenarios, list):
        raise ReportSourceError("report source is invalid")
    ordered = sorted(
        raw_scenarios,
        key=lambda value: _SCENARIO_ORDER.get(
            value.get("scenario_type") if isinstance(value, Mapping) else "", 99
        ),
    )
    scenario_reports: list[dict[str, object]] = []
    partial_core = core.get("core_state") == "partial"
    for index, value in enumerate(ordered):
        if not isinstance(value, Mapping):
            raise ReportSourceError("report source is invalid")
        bundle = _bundle(request=request, core=core, scenario=value, index=index)
        complete = value.get("core_state") == "complete"
        partial_core = partial_core or not complete
        scenario_reports.append(
            {
                "scenario_job_id": value["scenario_id"],
                "scenario_type": value["scenario_type"],
                "result_state": "completed" if complete else "failed",
                "device_group_id": None,
                "device_group_reason": "not_applicable",
                "bundle": bundle,
                "failure": None
                if complete
                else {
                    "code": "insufficient_data",
                    "message": "Core measurements are incomplete.",
                    "retryable": False,
                },
            }
        )

    public_ids: dict[str, list[str]] = {"recommendations": [], "retest_plan": []}
    if request.synthesis_document is None:
        if (
            request.synthesis_artifact_id is not None
            or request.synthesis_sha256_b64 is not None
            or not isinstance(request.synthesis_failure_code, str)
            or _FAILURE_CODE.fullmatch(request.synthesis_failure_code) is None
        ):
            raise ReportSourceError("report source is invalid")
        synthesis: dict[str, object] = {
            "state": "failed",
            "output": None,
            "synthesis_artifact_id": None,
            "failure_code": request.synthesis_failure_code,
            "provenance": None,
        }
        synthesis_failed = True
    else:
        if (
            request.synthesis_failure_code is not None
            or request.synthesis_artifact_id is None
            or request.synthesis_sha256_b64 is None
        ):
            raise ReportSourceError("report source is invalid")
        _checksum(request.synthesis_sha256_b64)
        output = validate_contract("synthesis-output", request.synthesis_document)
        usage = (request.prompt_tokens, request.completion_tokens, request.total_tokens)
        if (
            any(type(value) is not int or value < 0 for value in usage)
            or request.total_tokens != request.prompt_tokens + request.completion_tokens  # type: ignore[operator]
        ):
            raise ReportSourceError("report source is invalid")
        public_ids = {
            "recommendations": [
                str(public_item_id(request.synthesis_execution_id, "recommendation", index))
                for index, _ in enumerate(output["recommendations"])  # type: ignore[arg-type]
            ],
            "retest_plan": [
                str(public_item_id(request.synthesis_execution_id, "retest", index))
                for index, _ in enumerate(output["retest_plan"])  # type: ignore[arg-type]
            ],
        }
        synthesis = {
            "state": "completed",
            "output": output,
            "synthesis_artifact_id": str(request.synthesis_artifact_id),
            "failure_code": None,
            "provenance": {
                "provider_protocol": request.provider_protocol,
                "provider_name": request.provider_name,
                "model": request.model,
                "prompt_template_version": request.prompt_template_version,
                "prompt_template_sha256_b64": request.prompt_template_sha256_b64,
                "normalizer_version": request.normalizer_version,
                "report_worker_image_digest": request.report_worker_image_digest,
                "projection_artifact_id": str(request.projection_artifact_id),
                "projection_sha256_b64": request.projection_sha256_b64,
                "generated_at": generated_at,
                "prompt_tokens": request.prompt_tokens,
                "completion_tokens": request.completion_tokens,
                "total_tokens": request.total_tokens,
                "generation": request.generation,
            },
        }
        synthesis_failed = False
    state = "partially_completed" if partial_core or synthesis_failed else "completed"
    document = validate_contract(
        "analysis-report",
        {
            "schema_version": "1.1",
            "analysis_id": str(request.analysis_id),
            "analysis_mode": analysis_mode,
            "state": state,
            "report_version": report_version,
            "generated_at": generated_at,
            "scenario_reports": scenario_reports,
            "synthesis": synthesis,
        },
    )
    payload = canonical_json_bytes(document)
    digest = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    tenant_provenance: dict[str, object] = {
        "canonical_artifact_id": str(request.canonical_artifact_id),
        "canonical_sha256_b64": request.canonical_sha256_b64,
        "projection_artifact_id": str(request.projection_artifact_id),
        "projection_sha256_b64": request.projection_sha256_b64,
        "synthesis_artifact_id": str(request.synthesis_artifact_id) if request.synthesis_artifact_id else None,
        "synthesis_sha256_b64": request.synthesis_sha256_b64,
        "normalizer_version": request.normalizer_version,
        "prompt_template_version": request.prompt_template_version,
        "prompt_template_sha256_b64": request.prompt_template_sha256_b64,
        "provider_protocol": request.provider_protocol,
        "provider_name": request.provider_name,
        "model": request.model,
        "report_worker_image_digest": request.report_worker_image_digest,
        "prompt_tokens": request.prompt_tokens,
        "completion_tokens": request.completion_tokens,
        "total_tokens": request.total_tokens,
        "latency_ms": request.latency_ms,
        "generation": request.generation,
        "public_item_ids": public_ids,
    }
    return ComposedAnalysisReport(
        id=report_version_id(request.synthesis_execution_id),
        document=document,
        canonical_bytes=payload,
        sha256_b64=digest,
        tenant_provenance=tenant_provenance,
    )


class AnalysisReportWriter:
    def __init__(self, *, tenant_router: TenantRouter) -> None:
        self._tenant_router = tenant_router

    async def publish(self, request: AnalysisReportWriteRequest) -> PublishedAnalysisReport:
        # Validate all caller-controlled content before acquiring the tenant write lock.
        preview = compose_analysis_report(request, report_version=1)
        analysis_mode = preview.document["analysis_mode"]
        async with self._tenant_router.session(request.team_id) as session:
            if session.info.get("tenant_resource_version") != request.tenant_resource_version:
                raise ReportSourceError("report source is invalid")
            analysis = await session.scalar(
                select(Analysis)
                .where(Analysis.id == request.analysis_id)
                .with_for_update()
            )
            if analysis is None or analysis.analysis_mode != analysis_mode:
                raise ReportSourceError("report source is invalid")
            identity = report_version_id(request.synthesis_execution_id)
            existing = await session.get(ReportVersion, identity, with_for_update=True)
            if existing is not None:
                if existing.analysis_id != request.analysis_id or existing.report is None:
                    raise ReportIntegrityError("immutable report conflict")
                composed = compose_analysis_report(
                    request,
                    report_version=existing.report_version,
                )
                if (
                    existing.report_sha256_b64 != composed.sha256_b64
                    or canonical_json_bytes(existing.report) != composed.canonical_bytes
                    or existing.source_artifact_id != request.canonical_artifact_id
                    or existing.ai_projection_artifact_id != request.projection_artifact_id
                    or existing.ai_synthesis_artifact_id != request.synthesis_artifact_id
                ):
                    raise ReportIntegrityError("immutable report conflict")
                return _published(existing)
            latest = await session.scalar(
                select(func.max(ReportVersion.report_version)).where(
                    ReportVersion.analysis_id == request.analysis_id,
                    ReportVersion.scenario_result_id.is_(None),
                )
            )
            next_version = (latest or 0) + 1
            composed = compose_analysis_report(request, report_version=next_version)
            row = ReportVersion(
                id=composed.id,
                analysis_id=request.analysis_id,
                scenario_result_id=None,
                report_version=next_version,
                state={
                    "completed": "complete",
                    "partially_completed": "partial",
                    "failed": "failed",
                }[str(composed.document["state"])],
                generated_at=request.generated_at.astimezone(UTC),
                tool_version="perfpilot-report-writer-1",
                rule_version=request.normalizer_version,
                source_artifact_id=request.canonical_artifact_id,
                provenance=composed.tenant_provenance,
                bundle=None,
                bundle_sha256_b64=None,
                report=composed.document,
                report_sha256_b64=composed.sha256_b64,
                ai_projection_artifact_id=request.projection_artifact_id,
                ai_synthesis_artifact_id=request.synthesis_artifact_id,
            )
            session.add(row)
            await session.flush()
            return _published(row)


def _published(row: ReportVersion) -> PublishedAnalysisReport:
    if row.report is None or row.report_sha256_b64 is None:
        raise ReportIntegrityError("immutable report conflict")
    return PublishedAnalysisReport(
        id=row.id,
        analysis_id=row.analysis_id,
        report_version=row.report_version,
        state=row.state,
        generated_at=row.generated_at,
        sha256_b64=row.report_sha256_b64,
        document=row.report,
    )


__all__ = [
    "AnalysisReportWriteRequest",
    "AnalysisReportWriter",
    "ComposedAnalysisReport",
    "PublishedAnalysisReport",
    "ReportIntegrityError",
    "ReportSourceError",
    "ReportWriterError",
    "compose_analysis_report",
    "public_item_id",
    "report_version_id",
]
