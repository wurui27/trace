from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Mapping, Protocol
from uuid import UUID, uuid4

from perfpilot_api.ai.local_report import LocalReportSynthesizer, LocalReportUsage
from perfpilot_api.ai.synthesis import AISynthesisOutput
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.local_analysis_store import (
    migrate_analysis_runtime_status,
    validate_analysis_runtime_status,
)
from perfpilot_api.local_stage_execution import (
    REPORT_WORKER_IMAGE_DIGEST as _ENGINE_IMAGE_DIGEST,
    PreparedLocalReport as _PreparedLocalReport,
    blocked_ai_projection as _blocked_ai_projection,
    finding_projection_arguments as _finding_projection_arguments,
    prepare_local_report as _prepare_local_report,
    sha256_b64 as _sha256_b64,
    validated_checksum as _validated_checksum,
)
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import (
    AIProjection,
    ProjectionContractError,
    ProjectionPrivacyError,
    ProjectionQuestionError,
    ProjectionSizeError,
    build_ai_projection,
)
from perfpilot_api.reports.smartperfetto_original import (
    SmartPerfettoOriginalReference,
    restore_smartperfetto_original,
)
from perfpilot_api.engines.smartperfetto_contracts import validate_sanitized_report_payload
from perfpilot_api.reports.source_context import validate_persisted_source_context
from perfpilot_api.reports.writer import AnalysisReportWriteRequest, compose_analysis_report
from perfpilot_api.services.source_workspaces import SourceBinding
from perfpilot_api.workers.source_orchestrator import derive_source_authority


class _LocalAnalysis(Protocol):
    pass


class PersistedLocalEvidenceError(RuntimeError):
    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


_PersistedLocalEvidenceError = PersistedLocalEvidenceError
LocalAIRole = Literal["report", "extract", "review", "finalize"]


def _source_binding_document(binding: SourceBinding) -> dict[str, object]:
    return {
        "provider_kind": binding.provider_kind,
        "agent_id": str(binding.agent_id),
        "workspace_id": str(binding.workspace_id),
        "snapshot_policy": binding.snapshot_policy,
        "validation_profile_id": (
            str(binding.validation_profile_id)
            if binding.validation_profile_id is not None
            else None
        ),
    }


_CORE_PERSISTED_KEYS = frozenset(
    {
        "schema_version",
        "team_id",
        "analysis_id",
        "state",
        "version",
        "generation",
        "created_at",
        "started_at",
        "completed_at",
        "cancel_requested_at",
        "report_available",
        "response_schema_version",
        "runtime_status",
    }
)
_PAYLOAD_PERSISTED_KEYS = frozenset(
    {
        "analysis_mode",
        "device_id",
        "device_agent_id",
        "device_digest",
        "application_version_id",
        "application_metadata",
        "capture_configuration",
        "trace_test_type",
        "target_package_name",
        "custom_test_name",
        "custom_test_description",
        "remote_publication",
        "profile",
        "question",
        "inputs",
        "failure",
        "stages",
        "source_run",
        "source_rounds",
        "source_verification",
        "source_binding",
        "source_code_analysis",
        "ai_rounds",
        "evidence_format_version",
        "evidence_manifest",
        "smartperfetto_original",
    }
)
_PERSISTED_KEYS = _CORE_PERSISTED_KEYS | _PAYLOAD_PERSISTED_KEYS
_REQUIRED_PERSISTED_KEYS = frozenset(
    {
        "schema_version",
        "team_id",
        "analysis_id",
        "analysis_mode",
        "profile",
        "question",
        "state",
        "version",
        "generation",
        "inputs",
        "failure",
        "stages",
        "report_available",
    }
)
_PUBLIC_COMMON_KEYS = frozenset(
    {
        "schema_version",
        "analysis_id",
        "team_id",
        "analysis_mode",
        "state",
        "version",
        "created_at",
        "cancel_requested_at",
        "report_available",
        "failure",
        "source_code_analysis",
        "runtime_status",
    }
)
_PUBLIC_TRACE_KEYS = frozenset(
    {
        "analysis_profile",
        "test_type",
        "package_name",
        "custom_test_name",
        "custom_test_description",
        "question",
        "input_uploads",
        "stages",
        "ai_rounds",
        "source_analysis",
    }
)
_PUBLIC_DEVICE_KEYS = frozenset(
    {
        "device_id",
        "application_version_id",
        "application_metadata",
        "capture_configuration",
        "apk_upload",
        "scenarios",
        "sample_verdict_counts",
        "active_lease",
        "started_at",
        "completed_at",
    }
)
_PUBLIC_MEMORY_KEYS = frozenset(
    {"application_version_id", "application_metadata", "question"}
)
_PRIVATE_SOURCE_KEYS = frozenset(
    {"relative_path", "symbol", "diff", "content", "private_path"}
)
_TERMINAL_STATES = frozenset(
    {"completed", "partially_completed", "failed", "canceled", "deleted"}
)


class LocalAnalysisProjectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalAnalysisView:
    analysis_id: UUID
    team_id: UUID
    schema_version: str
    state: str
    version: int
    generation: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    report_available: bool
    runtime_status: Mapping[str, object]
    payload: Mapping[str, object]


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LocalAnalysisProjectionError("persisted analysis rejected") from None
    if parsed.tzinfo is None:
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    return parsed.astimezone(UTC)


def _validate_payload_keys(payload: Mapping[str, object]) -> None:
    keys = set(payload) - {"public_document"}
    if not keys.issubset(_PAYLOAD_PERSISTED_KEYS):
        raise LocalAnalysisProjectionError("persisted analysis rejected")


def to_persisted_document(value: LocalAnalysisView) -> dict[str, object]:
    _validate_payload_keys(value.payload)
    payload = {
        key: item
        for key, item in value.payload.items()
        if key in _PAYLOAD_PERSISTED_KEYS
    }
    document: dict[str, object] = {
        "schema_version": "1.0",
        **payload,
        "team_id": str(value.team_id),
        "analysis_id": str(value.analysis_id),
        "created_at": _timestamp(value.created_at),
        "started_at": _timestamp(value.started_at),
        "completed_at": _timestamp(value.completed_at),
        "state": value.state,
        "version": value.version,
        "generation": value.generation,
        "response_schema_version": value.schema_version,
        "runtime_status": dict(value.runtime_status),
        "cancel_requested_at": _timestamp(value.cancel_requested_at),
        "report_available": value.report_available,
    }
    if set(document) - _PERSISTED_KEYS or not _REQUIRED_PERSISTED_KEYS.issubset(
        document
    ):
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    return document


def from_persisted_document(document: Mapping[str, object]) -> LocalAnalysisView:
    if (
        set(document) - _PERSISTED_KEYS
        or not _REQUIRED_PERSISTED_KEYS.issubset(document)
        or document.get("schema_version") != "1.0"
        or type(document.get("version")) is not int
        or int(document["version"]) < 1
        or type(document.get("generation")) is not int
        or int(document["generation"]) < 1
        or not isinstance(document.get("state"), str)
        or not isinstance(document.get("analysis_mode"), str)
        or not isinstance(document.get("stages"), Mapping)
        or type(document.get("report_available")) is not bool
    ):
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    try:
        team_id = UUID(str(document["team_id"]))
        analysis_id = UUID(str(document["analysis_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise LocalAnalysisProjectionError("persisted analysis rejected") from None
    created_at = _parse_timestamp(document.get("created_at"))
    if created_at is None:
        created_at = datetime(1970, 1, 1, tzinfo=UTC)
    started_at = _parse_timestamp(document.get("started_at"))
    completed_at = _parse_timestamp(document.get("completed_at"))
    cancel_requested_at = _parse_timestamp(document.get("cancel_requested_at"))
    generation = int(document["generation"])
    updated_at = (completed_at or started_at or created_at).isoformat()
    try:
        runtime_status = migrate_analysis_runtime_status(
            document.get("runtime_status"),
            state=str(document["state"]),
            generation=generation,
            updated_at=updated_at,
            stages=document["stages"],
        )
    except Exception:
        raise LocalAnalysisProjectionError("persisted analysis rejected") from None
    payload = {
        key: item for key, item in document.items() if key in _PAYLOAD_PERSISTED_KEYS
    }
    return LocalAnalysisView(
        analysis_id=analysis_id,
        team_id=team_id,
        schema_version=str(document.get("response_schema_version") or "1.0"),
        state=str(document["state"]),
        version=int(document["version"]),
        generation=generation,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        cancel_requested_at=cancel_requested_at,
        report_available=bool(document["report_available"]),
        runtime_status=runtime_status,
        payload=payload,
    )


def _contains_private_source_key(value: object) -> bool:
    if isinstance(value, Mapping):
        if _PRIVATE_SOURCE_KEYS.intersection(value):
            return True
        return any(_contains_private_source_key(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_private_source_key(item) for item in value)
    return False


def to_public_document(value: LocalAnalysisView) -> dict[str, object]:
    raw = value.payload.get("public_document")
    if not isinstance(raw, Mapping):
        raise LocalAnalysisProjectionError("public analysis rejected")
    document = dict(raw)
    mode = document.get("analysis_mode")
    allowed = _PUBLIC_COMMON_KEYS | (
        _PUBLIC_TRACE_KEYS
        if mode == "trace_upload"
        else _PUBLIC_DEVICE_KEYS
        if mode == "device"
        else _PUBLIC_MEMORY_KEYS
        if mode == "memory_upload"
        else frozenset()
    )
    if (
        not allowed
        or set(document) - allowed
        or document.get("analysis_id") != str(value.analysis_id)
        or document.get("team_id") != str(value.team_id)
        or document.get("schema_version") != value.schema_version
        or document.get("state") != value.state
        or document.get("version") != value.version
        or document.get("report_available") is not value.report_available
    ):
        raise LocalAnalysisProjectionError("public analysis rejected")
    has_runtime = "runtime_status" in document
    if (value.schema_version == "1.3") != has_runtime:
        raise LocalAnalysisProjectionError("public analysis rejected")
    if has_runtime and document["runtime_status"] != dict(value.runtime_status):
        raise LocalAnalysisProjectionError("public analysis rejected")
    source = document.get("source_code_analysis")
    if isinstance(source, Mapping) and source.get("match_summary") != "strong":
        if _contains_private_source_key(source):
            raise LocalAnalysisProjectionError("public analysis rejected")
    if value.state in _TERMINAL_STATES and has_runtime:
        actions = value.runtime_status.get("available_actions")
        if actions != []:
            raise LocalAnalysisProjectionError("public analysis rejected")
    return document


def _compose_local_report(
    analysis: _LocalAnalysis,
    prepared: _PreparedLocalReport,
    *,
    generation: int,
    synthesis: AISynthesisOutput | None,
    synthesis_failure_code: str | None,
    rounds: tuple[LocalReportUsage, ...],
    synthesizer: LocalReportSynthesizer | None,
    smartperfetto_original: SmartPerfettoOriginalReference | None = None,
    ai_mode: Literal["available", "deterministic_fallback"] = "available",
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
        else "perfpilot-finding-report-v4"
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
            team_id=analysis.team_id,
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
            source_code_document=_local_source_code_document(analysis),
            projection_document=(
                prepared.projection.document
                if prepared.projection.document.get("schema_version") == "2.1"
                and synthesis_document is not None
                and synthesis_document.get("schema_version") == "2.1"
                else None
            ),
            report_schema_version=(
                "1.3"
                if prepared.projection.document.get("schema_version") == "2.1"
                and synthesis_document is not None
                and synthesis_document.get("schema_version") == "2.1"
                else "legacy"
            ),
            ai_mode=ai_mode,
        ),
        report_version=generation,
    )
    document = dict(composed.document)
    if document.get("schema_version") in {"1.2", "1.3"} and smartperfetto_original is not None:
        document["smartperfetto_original"] = smartperfetto_original.public_document()
        document = validate_contract("analysis-report", document)
    return document


def _local_source_code_document(analysis: _LocalAnalysis) -> dict[str, object]:
    binding = analysis.source_binding
    if binding is None:
        return {
            "requested": False,
            "provider_kind": None,
            "agent_id": None,
            "workspace_id": None,
            "snapshot_policy": None,
            "validation_profile_id": None,
            "snapshot": None,
            "context_state": "not_requested",
            "match_summary": "none",
            "source_refs": [],
            "exclusions": [],
            "fixes": [],
            "limitations": [],
        }
    context = analysis.source_context
    available = (
        isinstance(context, Mapping)
        and analysis.source_code_analysis.get("context_state") == "available"
    )
    match_summary = (
        str(context.get("match_summary"))
        if available
        else str(analysis.source_code_analysis.get("match_summary", "none"))
    )
    source_refs: list[dict[str, object]] = []
    if available and match_summary == "strong":
        fragments = context.get("fragments")
        if isinstance(fragments, list):
            source_refs = [
                {
                    **{
                        key: value
                        for key, value in fragment.items()
                        if key != "content"
                    },
                    "snapshot_hash": context["snapshot_hash"],
                }
                for fragment in fragments
                if isinstance(fragment, Mapping)
                and fragment.get("match_grade") == "strong"
            ]
    snapshot = (
        {
            "snapshot_id": context["snapshot_id"],
            "snapshot_hash": context["snapshot_hash"],
            "git_head": context["git_head"],
        }
        if available
        else None
    )
    exclusions = list(context.get("exclusions", [])) if available else []
    return {
        "requested": True,
        **_source_binding_document(binding),
        "snapshot": snapshot,
        "context_state": analysis.source_code_analysis.get(
            "context_state", "unavailable"
        ),
        "match_summary": match_summary,
        "source_refs": source_refs,
        "exclusions": exclusions,
        "fixes": [],
        "limitations": [],
    }


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


def _validated_persisted_source_report(value: object) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        report_id = value.get("reportId")
        validated = validate_sanitized_report_payload(
            {"reportId": report_id, "report": value}
        )
        report = validated["report"]
        if not isinstance(report, dict):
            raise ValueError
        return report
    except (TypeError, ValueError):
        raise _PersistedLocalEvidenceError("ai_source_evidence_invalid") from None


def _remap_projection_artifact(
    projection_document: Mapping[str, object],
    *,
    replacement: UUID,
) -> dict[str, object]:
    migrated = dict(projection_document)
    source = migrated.get("source")
    scenarios = migrated.get("scenarios")
    if not isinstance(source, Mapping) or not isinstance(scenarios, list):
        raise _PersistedLocalEvidenceError("ai_source_evidence_invalid")
    original = source.get("canonical_artifact_id")
    migrated["source"] = {**source, "canonical_artifact_id": str(replacement)}
    migrated_scenarios: list[dict[str, object]] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or not isinstance(
            scenario.get("evidence"), list
        ):
            raise _PersistedLocalEvidenceError("ai_source_evidence_invalid")
        migrated_evidence = [
            {
                **evidence,
                "artifact_id": (
                    str(replacement)
                    if evidence.get("artifact_id") == original
                    else evidence.get("artifact_id")
                ),
            }
            for evidence in scenario["evidence"]
            if isinstance(evidence, Mapping)
        ]
        if len(migrated_evidence) != len(scenario["evidence"]):
            raise _PersistedLocalEvidenceError("ai_source_evidence_invalid")
        migrated_scenarios.append({**scenario, "evidence": migrated_evidence})
    migrated["scenarios"] = migrated_scenarios
    return validate_contract("analysis-projection", migrated)


def _validate_legacy_report_facts(
    analysis: _LocalAnalysis,
    prepared: _PreparedLocalReport,
    report_value: object,
) -> None:
    try:
        report = validate_contract("analysis-report", report_value)
        if (
            report["analysis_id"] != str(analysis.analysis_id)
            or report["analysis_mode"] != analysis.analysis_mode
        ):
            raise ValueError
        report_scenarios = report["scenario_reports"]
        core_scenarios = prepared.core_document["scenario_reports"]
        if len(report_scenarios) != len(core_scenarios):
            raise ValueError
        selected = {
            (item["scenario_job_id"], item["scenario_type"]): item
            for item in report_scenarios
        }
        if len(selected) != len(report_scenarios):
            raise ValueError
        rebuilt_artifact_id = str(prepared.canonical_artifact_id)
        for core_scenario in core_scenarios:
            key = (
                core_scenario["scenario_id"],
                core_scenario["scenario_type"],
            )
            report_scenario = selected[key]
            bundle = report_scenario["bundle"]
            input_artifacts = bundle["provenance"]["input_artifacts"]
            if (
                len(input_artifacts) != 1
                or input_artifacts[0].get("artifact_kind") != "engine_result"
            ):
                raise ValueError
            old_artifact_id = input_artifacts[0]["artifact_id"]
            mapped_evidence = [
                {
                    **item,
                    "artifact_id": (
                        old_artifact_id
                        if item.get("artifact_id") == rebuilt_artifact_id
                        else item.get("artifact_id")
                    ),
                }
                for item in core_scenario["evidence"]
            ]
            if (
                report_scenario["result_state"]
                != (
                    "completed"
                    if core_scenario["core_state"] == "complete"
                    else "failed"
                )
                or bundle["scenario_job_id"] != core_scenario["scenario_id"]
                or bundle["scenario_type"] != core_scenario["scenario_type"]
                or bundle["bundle_state"] != core_scenario["core_state"]
                or bundle["metrics"] != core_scenario["metrics"]
                or bundle["findings"] != core_scenario["findings"]
                or bundle["evidence"] != mapped_evidence
                or bundle["trace_health"] != core_scenario["trace_health"]
                or bundle["trace_capabilities"]
                != core_scenario["trace_capabilities"]
            ):
                raise ValueError
    except Exception:
        raise _PersistedLocalEvidenceError("ai_source_evidence_invalid") from None


def _prepared_from_persisted_documents(
    analysis: _LocalAnalysis,
    *,
    source_value: object,
    core_value: object | None,
    projection_value: object,
    report_value: object | None,
) -> _PreparedLocalReport:
    source_report = _validated_persisted_source_report(source_value)
    try:
        projection_document = validate_contract("analysis-projection", projection_value)
        projection_bytes = canonical_json_bytes(projection_document)
        projection = AIProjection(
            canonical_bytes=projection_bytes,
            sha256_b64=_sha256_b64(projection_bytes),
        )
        source_rounds, source_verification = _source_metadata(source_report)
        if (
            analysis.source_rounds is not None
            and source_rounds != analysis.source_rounds
        ) or (
            analysis.source_verification != "unknown"
            and source_verification != analysis.source_verification
        ):
            raise ValueError
        if core_value is None:
            if analysis.analysis_mode != "trace_upload":
                raise _PersistedLocalEvidenceError("ai_source_evidence_unavailable")
            for source_state in ("completed", "insufficient_data"):
                rebuilt = _prepare_local_report(
                    analysis,
                    EngineResult(
                        contract="workspace-agent-v1",
                        state=source_state,
                        payload={
                            "reportId": source_report["reportId"],
                            "report": source_report,
                        },
                    ),
                )
                migrated_projection = _remap_projection_artifact(
                    projection_document,
                    replacement=rebuilt.canonical_artifact_id,
                )
                if canonical_json_bytes(migrated_projection) == (
                    rebuilt.projection.canonical_bytes
                ):
                    if report_value is None:
                        raise _PersistedLocalEvidenceError(
                            "ai_source_evidence_unavailable"
                        )
                    _validate_legacy_report_facts(
                        analysis,
                        rebuilt,
                        report_value,
                    )
                    return rebuilt
            raise ValueError
        core_document = validate_contract("normalized-trace-report", core_value)
        if (
            core_document["analysis_id"] != str(analysis.analysis_id)
            or core_document["analysis_mode"] != analysis.analysis_mode
            or projection_document["analysis_id"] != str(analysis.analysis_id)
            or projection_document["analysis_profile"] != analysis.profile
            or projection_document["question"] != analysis.question
        ):
            raise ValueError
        core_bytes = canonical_json_bytes(core_document)
        normalized = NormalizedTraceReport(
            canonical_bytes=core_bytes,
            sha256_b64=_sha256_b64(core_bytes),
        )
        source_context: dict[str, object] | None = None
        if analysis.source_binding is not None:
            if analysis.source_code_analysis.get("context_state") == "available":
                authority = derive_source_authority(core_document)
                context = analysis.source_context
                if not isinstance(context, Mapping):
                    raise ValueError
                source_context = validate_persisted_source_context(
                    context,
                    direct_identifiers=authority.direct_identifiers,
                    allowed_finding_ids=authority.finding_ids,
                    allowed_evidence_ids=authority.evidence_ids,
                )
                if (
                    source_context.get("match_summary")
                    != analysis.source_code_analysis.get("match_summary")
                ):
                    raise ValueError
            elif analysis.source_code_analysis.get("context_state") != "unavailable":
                raise ValueError
        provenance = core_document.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError
        canonical_artifact_id = UUID(str(provenance["canonical_artifact_id"]))
        projection_failure_code: str | None = None
        try:
            expected_projection = build_ai_projection(
                normalized,
                analysis_profile=analysis.profile,  # type: ignore[arg-type]
                question=analysis.question,
                source_context=source_context,
                **_finding_projection_arguments(
                    analysis,
                    normalized,
                    canonical_sha256_b64=str(provenance["canonical_sha256_b64"]),
                    normalizer_version=str(provenance["normalizer_version"]),
                ),
            )
        except (
            ProjectionContractError,
            ProjectionPrivacyError,
            ProjectionQuestionError,
            ProjectionSizeError,
        ) as error:
            projection_failure_code = {
                ProjectionContractError: "ai_projection_contract_invalid",
                ProjectionPrivacyError: "ai_projection_private_data",
                ProjectionQuestionError: "ai_projection_invalid_question",
                ProjectionSizeError: "ai_projection_too_large",
            }[type(error)]
            expected_projection = _blocked_ai_projection(
                analysis_id=analysis.analysis_id,
                analysis_profile=analysis.profile,
                canonical_artifact_id=canonical_artifact_id,
            )
        if (
            expected_projection.canonical_bytes != projection.canonical_bytes
            or expected_projection.sha256_b64 != projection.sha256_b64
        ):
            raise ValueError
        canonical_sha256_b64 = _validated_checksum(
            str(provenance["canonical_sha256_b64"])
        )
        normalizer_version = provenance.get("normalizer_version")
        if not isinstance(normalizer_version, str) or not normalizer_version:
            raise ValueError
        return _PreparedLocalReport(
            core_document=core_document,
            projection=projection,
            projection_failure_code=projection_failure_code,
            canonical_artifact_id=canonical_artifact_id,
            canonical_sha256_b64=canonical_sha256_b64,
            normalizer_version=normalizer_version,
            source_report=source_report,
        )
    except _PersistedLocalEvidenceError:
        raise
    except Exception:
        raise _PersistedLocalEvidenceError("ai_source_evidence_invalid") from None

compose_local_report = _compose_local_report
local_source_code_document = _local_source_code_document
prepared_from_persisted_documents = _prepared_from_persisted_documents
source_metadata = _source_metadata


_PERSISTED_STATE_KEYS = {
    "schema_version",
    "team_id",
    "analysis_id",
    "analysis_mode",
    "device_id",
    "device_agent_id",
    "device_digest",
    "application_version_id",
    "application_metadata",
    "capture_configuration",
    "trace_test_type",
    "target_package_name",
    "custom_test_name",
    "custom_test_description",
    "remote_publication",
    "profile",
    "question",
    "created_at",
    "started_at",
    "completed_at",
    "state",
    "version",
    "generation",
    "inputs",
    "failure",
    "cancel_requested_at",
    "stages",
    "source_run",
    "source_rounds",
    "source_verification",
    "source_binding",
    "source_code_analysis",
    "ai_rounds",
    "report_available",
    "evidence_format_version",
    "evidence_manifest",
    "smartperfetto_original",
    "response_schema_version",
    "runtime_status",
}
_PERSISTED_OPTIONAL_STATE_KEYS = {
    "created_at",
    "source_binding",
    "source_code_analysis",
    "ai_rounds",
    "evidence_format_version",
    "evidence_manifest",
    "smartperfetto_original",
    "remote_publication",
    "capture_configuration",
    "trace_test_type",
    "target_package_name",
    "custom_test_name",
    "custom_test_description",
    "response_schema_version",
    "runtime_status",
}
_PERSISTED_STAGE_KEYS = {
    "input_validation",
    "smartperfetto",
    "perfpilot_ai",
    "report",
}
_PERSISTED_SOURCE_CODE_KEYS = {
    "requested",
    "provider_kind",
    "agent_id",
    "workspace_id",
    "snapshot_policy",
    "validation_profile_id",
    "context_state",
    "match_summary",
    "verification_state",
    "failure_code",
}


def _has_exact_keys(value: object, keys: set[str]) -> bool:
    return isinstance(value, Mapping) and set(value) == keys


def _validate_persisted_state_shape(document: Mapping[str, object]) -> None:
    if document.get("schema_version") != "1.0":
        raise ValueError
    keys = set(document)
    required = _PERSISTED_STATE_KEYS - _PERSISTED_OPTIONAL_STATE_KEYS
    if not required <= keys or not keys <= _PERSISTED_STATE_KEYS:
        raise ValueError
    inputs = document.get("inputs")
    if not isinstance(inputs, list):
        raise ValueError
    for item in inputs:
        if not _has_exact_keys(
            item, {"descriptor", "upload_id", "artifact_id", "finalized"}
        ) or not _has_exact_keys(
            item["descriptor"], {"kind", "mime", "size", "sha256_b64"}
        ):
            raise ValueError
    if not _has_exact_keys(document.get("stages"), _PERSISTED_STAGE_KEYS):
        raise ValueError
    response_schema_version = document.get("response_schema_version")
    if response_schema_version is not None and response_schema_version not in {
        "1.0",
        "1.1",
        "1.2",
        "1.3",
    }:
        raise ValueError
    if "runtime_status" in document:
        validate_analysis_runtime_status(document["runtime_status"])
    source_run = document.get("source_run")
    if source_run is not None and not _has_exact_keys(
        source_run, {"session_id", "run_id"}
    ):
        raise ValueError
    source_binding = document.get("source_binding")
    if source_binding is not None and not _has_exact_keys(
        source_binding,
        {
            "provider_kind",
            "agent_id",
            "workspace_id",
            "snapshot_policy",
            "validation_profile_id",
        },
    ):
        raise ValueError
    if "source_code_analysis" in document and not _has_exact_keys(
        document["source_code_analysis"], _PERSISTED_SOURCE_CODE_KEYS
    ):
        raise ValueError
    if "ai_rounds" in document:
        rounds = document["ai_rounds"]
        if not isinstance(rounds, list) or any(
            not _has_exact_keys(item, {"round", "role", "state", "attempts"})
            for item in rounds
        ):
            raise ValueError
    metadata = document.get("application_metadata")
    if metadata is not None and not _has_exact_keys(
        metadata,
        {
            "package_name",
            "version_name",
            "version_code",
            "launch_activity",
            "min_sdk",
            "target_sdk",
            "supported_abis",
            "has_native_libraries",
        },
    ):
        raise ValueError
    capture_configuration = document.get("capture_configuration")
    if capture_configuration is not None:
        if not _has_exact_keys(
            capture_configuration,
            {
                "test_type",
                "launch_mode",
                "duration_seconds",
                "package_name",
                "launch_activity",
            },
        ):
            raise ValueError
        if (
            capture_configuration.get("test_type")
            not in {"cold_start", "hot_start", "scroll"}
            or capture_configuration.get("launch_mode")
            not in {"automatic", "manual"}
            or type(capture_configuration.get("duration_seconds")) is not int
            or not 1 <= int(capture_configuration["duration_seconds"]) <= 300
        ):
            raise ValueError
    trace_test_type = document.get("trace_test_type")
    target_package_name = document.get("target_package_name")
    custom_test_name = document.get("custom_test_name")
    custom_test_description = document.get("custom_test_description")
    trace_details = (
        trace_test_type,
        target_package_name,
        custom_test_name,
        custom_test_description,
    )
    if any(value is not None for value in trace_details):
        if (
            document.get("analysis_mode") != "trace_upload"
            or trace_test_type not in {"cold_start", "hot_start", "scroll", "other"}
            or not isinstance(target_package_name, str)
            or re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+",
                target_package_name,
            )
            is None
        ):
            raise ValueError
        if trace_test_type == "other":
            if not isinstance(custom_test_name, str) or not isinstance(
                custom_test_description, str
            ):
                raise ValueError
        elif custom_test_name is not None or custom_test_description is not None:
            raise ValueError
    if document.get("remote_publication", "not_requested") not in {
        "not_requested",
        "publishing",
        "published",
    }:
        raise ValueError
    failure = document.get("failure")
    if failure is not None and not _has_exact_keys(
        failure, {"code", "message", "retryable"}
    ):
        raise ValueError
    if "smartperfetto_original" in document:
        restore_smartperfetto_original(document["smartperfetto_original"])

validate_persisted_state_shape = _validate_persisted_state_shape


@dataclass(slots=True)
class _LocalAIRound:
    number: int
    role: LocalAIRole
    state: Literal["pending", "running", "completed", "failed"] = "pending"
    attempts: int = 0


def _restore_evidence_manifest(value: object) -> dict[str, str]:
    checksum_keys = {
        "normalized_core_sha256_b64",
        "smartperfetto_report_sha256_b64",
        "projection_sha256_b64",
    }
    try:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema_version", *checksum_keys}
            or value.get("schema_version") != "1.0"
        ):
            raise ValueError
        manifest = {"schema_version": "1.0"}
        for key in checksum_keys:
            checksum = value[key]
            if not isinstance(checksum, str):
                raise ValueError
            manifest[key] = _validated_checksum(checksum)
        return manifest
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid persisted local analysis") from None


def _default_ai_rounds() -> list[_LocalAIRound]:
    return [_LocalAIRound(1, "report")]


def _source_code_analysis_document(binding: SourceBinding | None) -> dict[str, object]:
    if binding is None:
        return {
            "requested": False,
            "provider_kind": None,
            "agent_id": None,
            "workspace_id": None,
            "snapshot_policy": None,
            "validation_profile_id": None,
            "context_state": "not_requested",
            "match_summary": "none",
            "verification_state": "not_requested",
            "failure_code": None,
        }
    return {
        "requested": True,
        **_source_binding_document(binding),
        "context_state": "waiting_for_agent",
        "match_summary": "none",
        "verification_state": "not_requested",
        "failure_code": None,
    }


def _source_code_analysis_unavailable_document(
    binding: SourceBinding,
) -> dict[str, object]:
    return {
        "requested": True,
        **_source_binding_document(binding),
        "context_state": "unavailable",
        "match_summary": "none",
        "verification_state": "not_requested",
        "failure_code": "source_agent_unavailable",
    }


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


LocalAIRound = _LocalAIRound
default_ai_rounds = _default_ai_rounds
restore_ai_rounds = _restore_ai_rounds
restore_evidence_manifest = _restore_evidence_manifest
source_binding_document = _source_binding_document
source_code_analysis_document = _source_code_analysis_document
source_code_analysis_unavailable_document = _source_code_analysis_unavailable_document


__all__ = [
    "LocalAIRound",
    "default_ai_rounds",
    "restore_ai_rounds",
    "restore_evidence_manifest",
    "source_binding_document",
    "source_code_analysis_document",
    "source_code_analysis_unavailable_document",
    "validate_persisted_state_shape",
    "PersistedLocalEvidenceError",
    "compose_local_report",
    "local_source_code_document",
    "prepared_from_persisted_documents",
    "source_metadata",
    "LocalAnalysisProjectionError",
    "LocalAnalysisView",
    "from_persisted_document",
    "to_persisted_document",
    "to_public_document",
]
