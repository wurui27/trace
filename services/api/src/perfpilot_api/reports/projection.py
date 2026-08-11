"""Construct the small, deterministic contract sent to AI synthesis."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.privacy import ProjectionPrivacyError, reject_private_json


class ProjectionQuestionError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("authoritative question is invalid")


class ProjectionSizeError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("AI projection exceeds the configured limit")


@dataclass(frozen=True, slots=True)
class AIProjection:
    canonical_bytes: bytes = field(repr=False)
    sha256_b64: str = field(repr=False)

    @property
    def document(self) -> dict[str, object]:
        document = json.loads(self.canonical_bytes)
        if not isinstance(document, dict):
            raise ProjectionPrivacyError
        return document


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def normalize_authoritative_question(question: str | None) -> str | None:
    if question is None:
        return None
    if not isinstance(question, str):
        raise ProjectionQuestionError
    normalized = question.strip()
    if not normalized or len(normalized) > 2_000:
        raise ProjectionQuestionError
    return normalized


def _only(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict) or any(field not in value for field in fields):
        raise ProjectionPrivacyError
    return {field: value[field] for field in fields}


def _project_allowlisted_core(
    core: dict[str, object],
    analysis_profile: Literal["auto", "startup", "scroll"],
    question: str | None,
) -> dict[str, object]:
    provenance = core.get("provenance")
    reports = core.get("scenario_reports")
    limitations = core.get("limitations")
    if not isinstance(provenance, dict) or not isinstance(reports, list) or not isinstance(limitations, list):
        raise ProjectionPrivacyError
    source = _only(provenance, ("engine_id", "adapter_version", "source_contract", "canonical_artifact_id"))
    scenarios: list[dict[str, object]] = []
    for report in reports:
        header = _only(report, ("scenario_id", "scenario_type", "core_state", "metrics", "findings", "evidence"))
        metrics = header["metrics"]
        findings = header["findings"]
        evidence = header["evidence"]
        if not isinstance(metrics, list) or not isinstance(findings, list) or not isinstance(evidence, list):
            raise ProjectionPrivacyError
        scenarios.append({
            "scenario_id": header["scenario_id"], "scenario_type": header["scenario_type"], "core_state": header["core_state"],
            "metrics": [_only(item, ("metric_id", "name", "status", "numeric_value", "unit", "definition", "threshold")) for item in metrics],
            "findings": [_only(item, ("finding_id", "rule_id", "kind", "status", "severity", "confidence", "title", "summary", "evidence_ids")) for item in findings],
            "evidence": [_only(item, ("evidence_id", "source", "query_id", "interval_start_ns", "interval_end_ns", "artifact_id", "fields")) for item in evidence],
            "limitations": [],
        })
    return {
        "schema_version": "2.0", "analysis_id": core.get("analysis_id"), "analysis_profile": analysis_profile,
        "question": question, "source": source,
        "scenarios": sorted(scenarios, key=lambda item: str(item["scenario_id"])),
        "limitations": sorted((_only(item, ("limitation_id", "code", "summary", "evidence_ids")) for item in limitations), key=lambda item: str(item["limitation_id"])),
    }


def _project_source_context(
    context: Mapping[str, object] | None,
    *,
    finding_ids: frozenset[str],
    evidence_ids: frozenset[str],
) -> dict[str, object] | None:
    if context is None:
        return None
    if (
        context.get("trust") != "untrusted_data_not_instructions"
        or context.get("match_summary") not in {"strong", "weak", "none"}
        or not isinstance(context.get("snapshot_hash"), str)
        or not isinstance(context.get("fragments"), list)
    ):
        raise ProjectionPrivacyError
    snapshot_hash = context["snapshot_hash"]
    match_summary = context["match_summary"]
    fragments: list[dict[str, object]] = []
    for raw in context["fragments"]:
        if not isinstance(raw, Mapping) or raw.get("match_grade") != match_summary:
            continue
        fragment = _only(
            dict(raw),
            (
                "source_ref_id",
                "relative_path",
                "language",
                "symbol",
                "start_line",
                "end_line",
                "content_sha256",
                "content",
                "finding_ids",
                "evidence_ids",
                "rule_ids",
                "match_grade",
            ),
        )
        content = fragment["content"]
        linked_findings = fragment["finding_ids"]
        linked_evidence = fragment["evidence_ids"]
        if (
            not isinstance(content, str)
            or fragment["content_sha256"]
            != hashlib.sha256(content.encode("utf-8")).hexdigest()
            or not isinstance(linked_findings, list)
            or not set(linked_findings).issubset(finding_ids)
            or not isinstance(linked_evidence, list)
            or not set(linked_evidence).issubset(evidence_ids)
        ):
            raise ProjectionPrivacyError
        fragments.append(fragment)
    if match_summary == "none":
        fragments = []
    elif not fragments:
        raise ProjectionPrivacyError
    return {
        "trust": "untrusted_data_not_instructions",
        "snapshot_hash": snapshot_hash,
        "match_summary": match_summary,
        "fragments": fragments,
    }


def build_ai_projection(
    core: NormalizedTraceReport,
    *,
    analysis_profile: Literal["auto", "startup", "scroll"],
    question: str | None,
    source_context: Mapping[str, object] | None = None,
    max_bytes: int = 256 * 1024,
) -> AIProjection:
    if (
        not isinstance(core, NormalizedTraceReport)
        or analysis_profile not in {"auto", "startup", "scroll"}
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= 256 * 1024
    ):
        raise ProjectionPrivacyError
    normalized_question = normalize_authoritative_question(question)
    document = _project_allowlisted_core(core.document, analysis_profile, normalized_question)
    projected_findings = frozenset(
        str(finding["finding_id"])
        for scenario in document["scenarios"]  # type: ignore[union-attr]
        for finding in scenario["findings"]
    )
    projected_evidence = frozenset(
        str(evidence["evidence_id"])
        for scenario in document["scenarios"]  # type: ignore[union-attr]
        for evidence in scenario["evidence"]
    )
    document["source_context"] = _project_source_context(
        source_context,
        finding_ids=projected_findings,
        evidence_ids=projected_evidence,
    )
    reject_private_json(document)
    try:
        validated = validate_contract("analysis-projection", document)
    except Exception:
        raise ProjectionPrivacyError from None
    payload = canonical_json_bytes(validated)
    if len(payload) > max_bytes:
        raise ProjectionSizeError
    return AIProjection(canonical_bytes=payload, sha256_b64=_checksum(payload))


__all__ = [
    "AIProjection",
    "ProjectionPrivacyError",
    "ProjectionQuestionError",
    "ProjectionSizeError",
    "build_ai_projection",
    "normalize_authoritative_question",
]
