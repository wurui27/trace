"""Validate AI synthesis candidates against an immutable analysis projection."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.privacy import reject_private_json
from perfpilot_api.reports.projection import AIProjection


DEFAULT_MAX_CANDIDATE_BYTES = 128 * 1024
_NUMERIC_TOKEN = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


class SynthesisValidationError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("AI synthesis output is invalid")


@dataclass(frozen=True, slots=True)
class AISynthesisOutput:
    canonical_bytes: bytes = field(repr=False)
    sha256_b64: str = field(repr=False)

    @property
    def document(self) -> dict[str, object]:
        document = json.loads(self.canonical_bytes)
        if not isinstance(document, dict):
            raise SynthesisValidationError
        return document


@dataclass(frozen=True, slots=True)
class _ProjectionIndex:
    evidence_ids: frozenset[str]
    finding_evidence: Mapping[str, frozenset[str]]
    finding_status: Mapping[str, str]
    scenario_metrics: Mapping[str, frozenset[str]]
    limitation_ids: frozenset[str]
    numeric_spellings: frozenset[str]


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SynthesisValidationError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise SynthesisValidationError


def parse_candidate(payload: bytes, max_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES) -> dict[str, object]:
    """Parse one bounded candidate JSON document without accepting JSON extensions."""

    if type(payload) is not bytes or type(max_bytes) is not int or not 1 <= len(payload) <= max_bytes:
        raise SynthesisValidationError
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
        return validate_contract("synthesis-output", value)
    except (UnicodeError, json.JSONDecodeError, SynthesisValidationError):
        raise SynthesisValidationError from None
    except Exception:
        raise SynthesisValidationError from None


def _candidate_document(candidate: object) -> dict[str, object]:
    if type(candidate) is bytes:
        return parse_candidate(candidate)
    try:
        return validate_contract("synthesis-output", candidate)
    except Exception:
        raise SynthesisValidationError from None


def _projection_index(projection: AIProjection) -> _ProjectionIndex:
    if not isinstance(projection, AIProjection):
        raise SynthesisValidationError
    try:
        document = validate_contract("analysis-projection", projection.document)
        scenarios = document["scenarios"]
        limitations = document["limitations"]
        if not isinstance(scenarios, list) or not isinstance(limitations, list):
            raise SynthesisValidationError

        evidence_ids: set[str] = set()
        finding_evidence: dict[str, frozenset[str]] = {}
        finding_status: dict[str, str] = {}
        scenario_metrics: dict[str, set[str]] = {}
        numeric_spellings: set[str] = set()

        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise SynthesisValidationError
            scenario_type = scenario.get("scenario_type")
            metrics = scenario.get("metrics")
            findings = scenario.get("findings")
            evidence = scenario.get("evidence")
            if (
                not isinstance(scenario_type, str)
                or not isinstance(metrics, list)
                or not isinstance(findings, list)
                or not isinstance(evidence, list)
            ):
                raise SynthesisValidationError
            scenario_metric_ids = scenario_metrics.setdefault(scenario_type, set())
            for metric in metrics:
                if not isinstance(metric, dict) or not isinstance(metric.get("metric_id"), str):
                    raise SynthesisValidationError
                metric_id = metric["metric_id"]
                if metric_id in scenario_metric_ids:
                    raise SynthesisValidationError
                scenario_metric_ids.add(metric_id)
                numeric_value = metric.get("numeric_value")
                if numeric_value is not None:
                    numeric_spellings.add(_number_spelling(numeric_value))
                threshold = metric.get("threshold")
                if threshold is not None:
                    if not isinstance(threshold, dict):
                        raise SynthesisValidationError
                    numeric_spellings.add(_number_spelling(threshold.get("value")))
            for item in evidence:
                if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
                    raise SynthesisValidationError
                evidence_id = item["evidence_id"]
                if evidence_id in evidence_ids:
                    raise SynthesisValidationError
                evidence_ids.add(evidence_id)
            for finding in findings:
                if not isinstance(finding, dict):
                    raise SynthesisValidationError
                finding_id = finding.get("finding_id")
                status = finding.get("status")
                referenced_evidence = finding.get("evidence_ids")
                if (
                    not isinstance(finding_id, str)
                    or not isinstance(status, str)
                    or not isinstance(referenced_evidence, list)
                    or finding_id in finding_evidence
                ):
                    raise SynthesisValidationError
                if not all(isinstance(item, str) for item in referenced_evidence):
                    raise SynthesisValidationError
                finding_evidence[finding_id] = frozenset(referenced_evidence)
                finding_status[finding_id] = status

        limitation_ids: set[str] = set()
        for limitation in limitations:
            if not isinstance(limitation, dict) or not isinstance(limitation.get("limitation_id"), str):
                raise SynthesisValidationError
            limitation_id = limitation["limitation_id"]
            if limitation_id in limitation_ids:
                raise SynthesisValidationError
            limitation_ids.add(limitation_id)
        for scenario in scenarios:
            if not isinstance(scenario, dict) or not isinstance(scenario.get("limitations"), list):
                raise SynthesisValidationError
            for limitation in scenario["limitations"]:
                if not isinstance(limitation, dict) or not isinstance(limitation.get("limitation_id"), str):
                    raise SynthesisValidationError
                limitation_id = limitation["limitation_id"]
                if limitation_id in limitation_ids:
                    raise SynthesisValidationError
                limitation_ids.add(limitation_id)
    except SynthesisValidationError:
        raise
    except Exception:
        raise SynthesisValidationError from None

    return _ProjectionIndex(
        evidence_ids=frozenset(evidence_ids),
        finding_evidence=MappingProxyType(finding_evidence),
        finding_status=MappingProxyType(finding_status),
        scenario_metrics=MappingProxyType(
            {scenario_type: frozenset(metric_ids) for scenario_type, metric_ids in scenario_metrics.items()}
        ),
        limitation_ids=frozenset(limitation_ids),
        numeric_spellings=frozenset(numeric_spellings),
    )


def _number_spelling(value: object) -> str:
    try:
        return canonical_json_bytes(value).decode("ascii")
    except Exception:
        raise SynthesisValidationError from None


def _known_ids(values: object, allowed: frozenset[str] | Mapping[str, object]) -> bool:
    return isinstance(values, list) and all(isinstance(value, str) and value in allowed for value in values)


def _validate_semantics(document: dict[str, object], index: _ProjectionIndex) -> None:
    top_findings = document["top_findings"]
    recommendations = document["recommendations"]
    retest_plan = document["retest_plan"]
    limitations = document["limitations"]
    if not all(isinstance(section, list) for section in (top_findings, recommendations, retest_plan, limitations)):
        raise SynthesisValidationError

    for finding in top_findings:
        if not isinstance(finding, dict):
            raise SynthesisValidationError
        finding_id = finding.get("finding_id")
        evidence_ids = finding.get("evidence_ids")
        if (
            not isinstance(finding_id, str)
            or finding_id not in index.finding_evidence
            or not _known_ids(evidence_ids, index.evidence_ids)
            or not isinstance(evidence_ids, list)
            or not set(evidence_ids).issubset(index.finding_evidence[finding_id])
        ):
            raise SynthesisValidationError

    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise SynthesisValidationError
        finding_ids = recommendation.get("finding_ids")
        evidence_ids = recommendation.get("evidence_ids")
        if not _known_ids(finding_ids, index.finding_evidence) or not _known_ids(evidence_ids, index.evidence_ids):
            raise SynthesisValidationError
        if not finding_ids or not evidence_ids or any(
            index.finding_status[finding_id] not in {"confirmed", "suspected"}
            for finding_id in finding_ids
        ):
            raise SynthesisValidationError

    for retest in retest_plan:
        if not isinstance(retest, dict):
            raise SynthesisValidationError
        scenario_type = retest.get("scenario_type")
        if not isinstance(scenario_type, str) or scenario_type not in index.scenario_metrics:
            raise SynthesisValidationError
        mode = retest.get("mode")
        if mode == "verify_metric":
            metric_ids = retest.get("metric_ids")
            if not _known_ids(metric_ids, index.scenario_metrics[scenario_type]) or not metric_ids:
                raise SynthesisValidationError
        elif mode == "collect_evidence":
            if not _known_ids(retest.get("limitation_ids"), index.limitation_ids):
                raise SynthesisValidationError
        else:
            raise SynthesisValidationError

    for limitation in limitations:
        if not isinstance(limitation, dict) or not isinstance(limitation.get("limitation_id"), str):
            raise SynthesisValidationError
        if limitation["limitation_id"] not in index.limitation_ids:
            raise SynthesisValidationError

    for text in _narrative_fields(document):
        if any(token.group(0) not in index.numeric_spellings for token in _NUMERIC_TOKEN.finditer(text)):
            raise SynthesisValidationError


def _narrative_fields(document: dict[str, object]) -> tuple[str, ...]:
    fields = [document["executive_summary"]]
    for finding in document["top_findings"]:
        fields.append(finding["user_impact"])
    for recommendation in document["recommendations"]:
        fields.extend((recommendation["title"], recommendation["action"], recommendation["expected_effect"]))
    for retest in document["retest_plan"]:
        fields.append(retest["steps"])
    for limitation in document["limitations"]:
        fields.append(limitation["summary"])
    if not all(isinstance(field, str) for field in fields):
        raise SynthesisValidationError
    return tuple(fields)


def validate_synthesis_output(
    *, projection: AIProjection, candidate: object
) -> AISynthesisOutput:
    """Return only a canonical, privacy-safe candidate grounded in ``projection``."""

    try:
        document = _candidate_document(candidate)
        reject_private_json(document)
        _validate_semantics(document, _projection_index(projection))
        payload = canonical_json_bytes(document)
    except SynthesisValidationError:
        raise
    except Exception:
        raise SynthesisValidationError from None
    return AISynthesisOutput(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
    )


ValidatedSynthesisOutput = AISynthesisOutput

__all__ = [
    "AISynthesisOutput",
    "DEFAULT_MAX_CANDIDATE_BYTES",
    "SynthesisValidationError",
    "ValidatedSynthesisOutput",
    "parse_candidate",
    "validate_synthesis_output",
]
