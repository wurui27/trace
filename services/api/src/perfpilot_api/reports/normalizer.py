"""Deterministically project supported SmartPerfetto facts into the core report."""

from __future__ import annotations

import base64
import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.services.canonical_result_reader import LoadedCanonicalResult


_NORMALIZED_REPORT_NAMESPACE = UUID("71c6af46-884d-5b2b-9453-1e2b0e7be879")
_NORMALIZER_VERSION = "smartperfetto-normalizer-1"
_SCENARIO_ORDER = {"startup": 0, "scroll": 1, "memory_cycle": 2}
_SEVERITY = {
    "critical": "critical",
    "high": "critical",
    "warning": "warning",
    "medium": "warning",
    "low": "informational",
    "info": "informational",
}
_CONFIDENCE = {"high": 3, "medium": 2, "low": 1, "none": 0}
_PUBLIC_TEXT = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class SmartPerfettoNormalizationError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("SmartPerfetto result cannot be normalized")


@dataclass(frozen=True, slots=True)
class NormalizedTraceReport:
    document: dict[str, object] = field(repr=False)
    canonical_bytes: bytes = field(repr=False)
    sha256_b64: str = field(repr=False)


def _fail() -> SmartPerfettoNormalizationError:
    return SmartPerfettoNormalizationError()


def _stable_id(kind: str, analysis_id: UUID, source_id: str) -> UUID:
    return uuid5(_NORMALIZED_REPORT_NAMESPACE, f"{analysis_id}:{kind}:{source_id}")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _fail()
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise _fail()
    return value


def _text(value: object, *, public: bool = False, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise _fail()
    if public and _PUBLIC_TEXT.fullmatch(value) is None:
        raise _fail()
    return value


def _finite(value: object, *, nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    if type(value) not in {int, float} or not math.isfinite(value):
        raise _fail()
    return value


def _id(value: object) -> str:
    return _text(value, public=True)  # type: ignore[return-value]


def _sort(items: list[dict[str, object]], field: str) -> list[dict[str, object]]:
    return sorted(items, key=lambda item: str(item[field]))


def _health(value: object) -> dict[str, object]:
    source = _mapping(value)
    target = _mapping(source.get("targetResolution"))
    window = _mapping(source.get("measurementWindow"))
    loss = _mapping(source.get("dataLoss"))
    parse_status = source.get("parseStatus")
    coverage = window.get("coverage")
    if (
        parse_status not in {"parsed", "failed", "not_applicable"}
        or coverage not in {"complete", "partial", "missing"}
        or any(source.get(name) not in {"available", "unavailable", "insufficient_data", "not_applicable"} for name in ("frameTimelineCoverage", "targetDisplayCoverage", "refreshModeCoverage"))
    ):
        raise _fail()
    def integer_or_none(item: object) -> int | None:
        if item is None:
            return None
        if type(item) is int:
            return item
        raise _fail()
    return {
        "parse_status": parse_status,
        "trace_start_ns": integer_or_none(source.get("traceStartNs")),
        "trace_end_ns": integer_or_none(source.get("traceEndNs")),
        "target_resolution": {
            "package_name": _text(target.get("packageName"), nullable=True),
            "process_name": _text(target.get("processName"), nullable=True),
            "upid": integer_or_none(target.get("upid")),
            "pid": integer_or_none(target.get("pid")),
            "main_thread_id": integer_or_none(target.get("mainThreadId")),
        },
        "measurement_window": {
            "start_ns": integer_or_none(window.get("startNs")),
            "end_ns": integer_or_none(window.get("endNs")),
            "coverage": coverage,
        },
        "data_loss": {
            "buffer_overruns": _nonnegative(loss.get("bufferOverruns")),
            "ftrace_events_lost": _nonnegative(loss.get("ftraceEventsLost")),
            "traced_buf_patches_failed": _nonnegative(loss.get("tracedBufPatchesFailed")),
            "incomplete_slices": _nonnegative(loss.get("incompleteSlices")),
            "boundary_truncations": _nonnegative(loss.get("boundaryTruncations")),
        },
        "frame_timeline_coverage": source["frameTimelineCoverage"],
        "target_display_coverage": source["targetDisplayCoverage"],
        "refresh_mode_coverage": source["refreshModeCoverage"],
    }


def _nonnegative(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _fail()
    return value


def _claims(report: Mapping[str, object]) -> dict[str, dict[str, object]]:
    accepted: dict[str, dict[str, object]] = {}
    for item in _sequence(report.get("claimVerificationResult")):
        claim = _mapping(item)
        source_id = _id(claim.get("claimId"))
        if source_id in accepted:
            raise _fail()
        verified, partial = claim.get("verified"), claim.get("partial", False)
        confidence = claim.get("confidence")
        evidence = [_id(item) for item in _sequence(claim.get("evidenceIds"))]
        if len(evidence) != len(set(evidence)) or confidence not in _CONFIDENCE or type(verified) is not bool or type(partial) is not bool:
            raise _fail()
        accepted[source_id] = {
            "accepted": verified or partial,
            "verified": verified,
            "confidence": confidence,
            "evidence": evidence,
        }
    return accepted


def _build_core(source: LoadedCanonicalResult) -> dict[str, object]:
    document = _mapping(source.document)
    engine, result = _mapping(document.get("engine")), _mapping(document.get("result"))
    if (
        document.get("schema_version") != "1.0"
        or document.get("result_type") != "canonical-engine-result"
        or document.get("analysis_id") != str(source.analysis_id)
        or document.get("execution_id") != str(source.execution_id)
        or document.get("artifact_id") != str(source.artifact_id)
        or document.get("tenant_resource_version") != source.tenant_resource_version
        or engine.get("engine_id") != "smartperfetto"
        or engine.get("source_contract") != "workspace-agent-v1"
        or result.get("state") not in {"completed", "insufficient_data"}
    ):
        raise _fail()
    payload = _mapping(result.get("payload"))
    report = _mapping(payload.get("report"))
    contract = _mapping(report.get("resultContract"))
    if (
        contract.get("version") != "1.0.0"
        or _mapping(report.get("identityResolutions")).get("contract")
        != "identity_contract@1"
    ):
        raise _fail()
    claims = _claims(report)
    envelopes: dict[str, Mapping[str, object]] = {}
    evidence_by_id: dict[str, Mapping[str, object]] = {}
    for raw in _sequence(report.get("dataEnvelopes")):
        envelope = _mapping(raw)
        scenario = envelope.get("scenario")
        source_id = _id(envelope.get("id"))
        if envelope.get("type") != "data-envelope@1" or scenario not in _SCENARIO_ORDER or scenario in envelopes:
            raise _fail()
        envelopes[scenario] = envelope
        for raw_evidence in _sequence(envelope.get("evidence")):
            evidence = _mapping(raw_evidence)
            evidence_id = _id(evidence.get("id"))
            if evidence_id in evidence_by_id:
                raise _fail()
            evidence_by_id[evidence_id] = evidence
    permitted_evidence = {item for claim in claims.values() if claim["accepted"] for item in claim["evidence"]}  # type: ignore[misc]
    if not permitted_evidence.issubset(evidence_by_id):
        raise _fail()
    scenario_reports: list[dict[str, object]] = []
    for scenario_type, envelope in envelopes.items():
        evidence = [_evidence(source.analysis_id, evidence_by_id[item], item, source.artifact_id) for item in permitted_evidence if item in { _id(value.get("id")) for value in _sequence(envelope.get("evidence")) }]
        metric_items = _metrics(source.analysis_id, envelope, evidence_by_id, permitted_evidence)
        scenario_reports.append({
            "scenario_id": str(_stable_id("scenario", source.analysis_id, str(envelope["id"]))),
            "scenario_type": scenario_type,
            "core_state": "partial" if result["state"] == "insufficient_data" or any(metric["status"] != "available" for metric in metric_items) else "complete",
            "metrics": _sort(metric_items, "metric_id"),
            "findings": [],
            "evidence": _sort(evidence, "evidence_id"),
            "trace_health": _health(envelope.get("traceHealth")),
            "trace_capabilities": _capabilities(envelope.get("capabilities")),
        })
    if not scenario_reports:
        raise _fail()
    limitations: list[dict[str, object]] = []
    by_scenario = {str(item["scenario_type"]): item for item in scenario_reports}
    diagnostic_ids: set[str] = set()
    for raw in _sequence(report.get("diagnostics")):
        diagnostic = _mapping(raw)
        source_id, scenario = _id(diagnostic.get("id")), diagnostic.get("scenario")
        claim_id = _id(diagnostic.get("claimRef"))
        if source_id in diagnostic_ids or scenario not in by_scenario:
            raise _fail()
        diagnostic_ids.add(source_id)
        claim = claims.get(claim_id)
        if claim is None or not claim["accepted"]:
            limitations.append(_limitation(source.analysis_id, diagnostic, source_id))
            continue
        finding = _finding(source.analysis_id, diagnostic, source_id, claim, source.artifact_id)
        by_scenario[scenario]["findings"].append(finding)  # type: ignore[index]
    for scenario in scenario_reports:
        scenario["findings"] = _sort(scenario["findings"], "finding_id")  # type: ignore[arg-type]
    scenario_reports.sort(key=lambda item: _SCENARIO_ORDER[str(item["scenario_type"])])
    return {
        "schema_version": "1.0",
        "analysis_id": str(source.analysis_id),
        "analysis_mode": "trace_upload",
        "core_state": "partial" if result["state"] == "insufficient_data" or limitations or any(item["core_state"] == "partial" for item in scenario_reports) else "complete",
        "scenario_reports": scenario_reports,
        "limitations": _sort(limitations, "limitation_id"),
        "provenance": {
            "engine_id": "smartperfetto", "adapter_version": engine["adapter_version"], "engine_commit_sha": engine["source_commit_sha"], "engine_image_digest": engine["image_digest"], "source_contract": "workspace-agent-v1", "result_contract_version": "1.0.0", "canonical_artifact_id": str(source.artifact_id), "canonical_sha256_b64": source.sha256_b64, "normalizer_version": _NORMALIZER_VERSION,
        },
    }


def _evidence(analysis_id: UUID, raw: Mapping[str, object], source_id: str, artifact_id: UUID) -> dict[str, object]:
    fields = _mapping(raw.get("fields"))
    copied: dict[str, object] = {}
    for key, value in fields.items():
        if type(value) in {int, float}:
            copied[_id(key)] = _finite(value)
        elif value is None or isinstance(value, str):
            copied[_id(key)] = _text(value, nullable=True)
        else:
            raise _fail()
    return {"evidence_id": str(_stable_id("evidence", analysis_id, source_id)), "source": _id(raw.get("source")), "query_id": _text(raw.get("queryId"), public=True, nullable=True), "interval_start_ns": _finite(raw.get("intervalStartNs"), nullable=True), "interval_end_ns": _finite(raw.get("intervalEndNs"), nullable=True), "artifact_id": str(artifact_id), "fields": copied}


def _metrics(analysis_id: UUID, envelope: Mapping[str, object], evidence: Mapping[str, object], permitted: set[str]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in _sequence(envelope.get("columns")):
        column, source_id = _mapping(raw), _id(_mapping(raw).get("id"))
        evidence_id = _id(column.get("evidenceId"))
        if column.get("type") != "metric@1" or source_id in seen or evidence_id not in evidence or evidence_id not in permitted:
            raise _fail()
        seen.add(source_id)
        value = _finite(column.get("value"), nullable=True)
        unit = _text(column.get("unit"), nullable=True)
        if value is not None and unit is None:
            raise _fail()
        threshold_raw = column.get("threshold")
        threshold = None if threshold_raw is None else _threshold(threshold_raw)
        items.append({"metric_id": str(_stable_id("metric", analysis_id, source_id)), "name": _text(column.get("name"), public=True), "status": "available" if value is not None else "insufficient_data", "numeric_value": value, "unit": unit, "definition": _text(column.get("definition")), "threshold": threshold, "sample_ids": [str(_stable_id("evidence", analysis_id, evidence_id))]})
    return items


def _threshold(raw: object) -> dict[str, object]:
    value = _mapping(raw)
    if value.get("operator") not in {"gt", "gte", "lt", "lte", "eq"}:
        raise _fail()
    return {"operator": value["operator"], "value": _finite(value.get("value")), "unit": _text(value.get("unit"))}


def _capabilities(raw: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in _sequence(raw):
        value = _mapping(item)
        if type(value.get("required")) is not bool or value.get("status") not in {"available", "unavailable", "insufficient_data"}:
            raise _fail()
        result.append({"name": _text(value.get("name"), public=True), "required": value["required"], "status": value["status"], "reason": _text(value.get("reason"), nullable=True)})
    return sorted(result, key=lambda item: str(item["name"]))


def _finding(analysis_id: UUID, value: Mapping[str, object], source_id: str, claim: Mapping[str, object], artifact_id: UUID) -> dict[str, object]:
    status = value.get("status")
    if status not in {"confirmed", "suspected", "insufficient_data", "invalid_capture"} or value.get("kind") not in {"symptom", "root_cause"}:
        raise _fail()
    source_confidence = value.get("confidence")
    if source_confidence not in _CONFIDENCE or claim["confidence"] not in _CONFIDENCE:
        raise _fail()
    ceiling = min(_CONFIDENCE[source_confidence], _CONFIDENCE[claim["confidence"]])  # type: ignore[index]
    confidence = next(name for name, rank in _CONFIDENCE.items() if rank == ceiling)
    evidence_ids = sorted(str(_stable_id("evidence", analysis_id, item)) for item in claim["evidence"])  # type: ignore[arg-type]
    return {"finding_id": str(_stable_id("finding", analysis_id, source_id)), "rule_id": _text(value.get("ruleId"), public=True), "kind": value["kind"], "status": status, "severity": _SEVERITY.get(str(value.get("severity")), "informational"), "confidence": confidence, "confidence_ceiling": confidence, "title": _text(value.get("title")), "summary": _text(value.get("summary")), "evidence_ids": evidence_ids, "exclusions": [], "recommendation": _text(value.get("recommendation"), nullable=True), "retest": _text(value.get("retest"), nullable=True)}


def _limitation(analysis_id: UUID, value: Mapping[str, object], source_id: str) -> dict[str, object]:
    return {"limitation_id": str(_stable_id("limitation", analysis_id, source_id)), "code": _text(value.get("ruleId"), public=True), "summary": _text(value.get("summary")), "evidence_ids": []}


def normalize_smartperfetto_result(source: LoadedCanonicalResult) -> NormalizedTraceReport:
    try:
        document = _build_core(source)
        validated = validate_contract("normalized-trace-report", document)
        payload = canonical_json_bytes(validated)
        return NormalizedTraceReport(
            document=validated,
            canonical_bytes=payload,
            sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
        )
    except SmartPerfettoNormalizationError:
        raise
    except Exception:
        raise SmartPerfettoNormalizationError from None


__all__ = ["NormalizedTraceReport", "SmartPerfettoNormalizationError", "normalize_smartperfetto_result"]
