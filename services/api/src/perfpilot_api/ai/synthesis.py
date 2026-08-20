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
_NUMERIC_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
)
_SOURCE_PATH_TOKEN = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])+(?:[A-Za-z0-9_.-]+\.(?:kt|java|xml|gradle|kts))",
    re.IGNORECASE,
)
_NUMERIC_LITERAL = (
    r"[+-]?(?:[0-9０-９]+(?:[.．][0-9０-９]+)?|[.．][0-9０-９]+)"
    r"(?:[eE][+-]?[0-9０-９]+)?"
)
_MEASUREMENT_UNIT = (
    r"(?:毫秒|微秒|纳秒|分钟|小时|帧每秒|"
    r"(?:千|兆|吉|太)?字节每秒|(?:千|兆|吉|太)?字节|"
    r"(?:千|兆|吉)?赫兹|(?:兆|吉)赫|"
    r"[kmgt]i?b(?:/s)?|b(?:/s)?|[kmg]?hz|fps|msec|sec|ms|us|µs|μs|ns|s|%|％|秒)"
)
_CHINESE_INTEGER = r"[零〇一二三四五六七八九十百千万萬亿億两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]+"
_CHINESE_NUMERAL = rf"{_CHINESE_INTEGER}(?:点{_CHINESE_INTEGER})?"
_CHINESE_COUNT_UNIT = r"(?:次|条|个|项|帧|轮|倍|段|处|种|份|组|类|台|核|页|行)"
_LEXICAL_ONE_COUNT_GUARD = (
    rf"(?!一次性)(?!(?:(?<=每)|(?<=逐)|(?<=同)|(?<=另)|"
    rf"(?<=上)|(?<=下)|(?<=前)|(?<=后)|(?<=这)|(?<=某)|(?<=任)|(?<=新))"
    rf"一\s*{_CHINESE_COUNT_UNIT})"
)
_LEXICAL_ONE_MEASUREMENT_GUARD = (
    rf"(?!(?:(?<=统)|(?<=逐)|(?<=每)|(?<=同)|(?<=另)|"
    rf"(?<=上)|(?<=下)|(?<=前)|(?<=后)|(?<=这)|(?<=某)|(?<=任)|(?<=新))"
    rf"一\s*{_MEASUREMENT_UNIT})"
)
_STANDALONE_QUANTITY_PREFIX = (
    r"(?:性能|耗时|时长|延迟|占比|比例|问题数量|数量|排名|收益|吞吐|"
    r"内存|帧率|频率|CPU(?:占用)?)\s*"
    r"(?:提升|降低|增加|减少|上升|下降|为|达到|达|是)"
)
_STANDALONE_CHINESE_QUANTITY = (
    rf"{_STANDALONE_QUANTITY_PREFIX}\s*{_CHINESE_NUMERAL}(?:成)?"
    r"(?=[\s，。；！？,.!?;:]|$)"
)
_ASCII_SUFFIXED_MEASUREMENT = (
    rf"(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}"
    r"(?:x|milliseconds?|microseconds?|nanoseconds?|seconds?|percent)"
    r"(?![A-Za-z0-9_])"
)
_TOP_RANK = rf"(?i:top)\s*{_NUMERIC_LITERAL}(?![A-Za-z0-9_])"
_FREE_WRITTEN_NUMBER = re.compile(
    rf"(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}\s*{_MEASUREMENT_UNIT}(?![A-Za-z0-9_])"
    rf"|(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}(?![A-Za-z0-9_])"
    rf"|{_LEXICAL_ONE_MEASUREMENT_GUARD}{_CHINESE_NUMERAL}\s*{_MEASUREMENT_UNIT}"
    rf"(?![A-Za-z0-9_])"
    rf"|百分之{_CHINESE_NUMERAL}"
    rf"|{_LEXICAL_ONE_COUNT_GUARD}{_CHINESE_NUMERAL}\s*{_CHINESE_COUNT_UNIT}"
    rf"|{_STANDALONE_CHINESE_QUANTITY}"
    rf"|{_ASCII_SUFFIXED_MEASUREMENT}"
    rf"|{_TOP_RANK}",
    re.IGNORECASE,
)
_TECHNICAL_IDENTIFIER_WITH_DIGIT = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?:android|api|media|h|avc|hevc|vp|armv?|x86|riscv|ipv|http|tls|"
    r"wifi|jdk|java|kotlin|agp|ndk|sdk)\d+[A-Za-z0-9_.-]*"
    r"|(?=[A-Za-z0-9_.-]*[_.])(?=[A-Za-z0-9_.-]*\d)"
    r"[A-Za-z][A-Za-z0-9_.-]*"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_TECHNICAL_VERSION_WITH_DIGIT = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?:android|api|jdk|kotlin|agp|sdk)\s+\d+(?:\.\d+)*"
    r"|http/\d+(?:\.\d+)*"
    r"|tls\s+\d+(?:\.\d+)*"
    r"|wi[-‑ ]?fi\s+\d+(?:\.\d+)*"
    r"|ndk\s+r\d+(?:\.\d+)*"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_LEXICAL_NUMBER_WORD = re.compile(
    r"(?:十六进制|二进制|零拷贝|千万不要|另一方面|三方面|两方面|"
    r"一系列|一般|一旦|一部分|一次性|统一|唯一|一致|一样|一律|"
    r"一起|一直|一定|一并|一体|一经|一度|一时|一再|一贯|一向|"
    r"进一步|一方面|三方|两者|二者|第一帧|第一屏|逐一|同一|"
    rf"(?:每|另|上|下|前|后|这|某|任|新)一{_CHINESE_COUNT_UNIT})"
)
_IMPLICIT_NUMERIC_PROMISE = re.compile(r"(?:翻倍|成倍|双倍|减半)")
_EMBEDDED_ASCII_QUANTITY = re.compile(
    rf"(?:{_NUMERIC_LITERAL}\s*(?:{_MEASUREMENT_UNIT}|milliseconds?|"
    r"microseconds?|nanoseconds?|seconds?|percent|pct|mbps|frames?|calls?|"
    r"fold|millis|msecs|x|times?|倍|成)|(?:top|x)\s*"
    rf"{_NUMERIC_LITERAL})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_BARE_CHINESE_QUANTITY = re.compile(
    rf"{_CHINESE_NUMERAL}(?:成)?(?=[\s，。；！？,.!?;:]|$)"
)
_CONTEXTUAL_CHINESE_QUANTITY = re.compile(
    rf"(?:提升|降低|增加|减少|上升|下降|改善|优化|节省|降到|达到|达|为|是)"
    rf"\s*{_CHINESE_NUMERAL}(?:成)?(?=[\s，。；！？,.!?;:]|$)"
)
_HALF_MEASUREMENT = re.compile(rf"半\s*{_MEASUREMENT_UNIT}", re.IGNORECASE)
_ENGLISH_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
)
_SPELLED_ENGLISH_QUANTITY = re.compile(
    rf"\b(?:top\s+{_ENGLISH_NUMBER_WORD}|{_ENGLISH_NUMBER_WORD}\s+"
    r"(?:milliseconds?|microseconds?|nanoseconds?|seconds?|percent|frames?|"
    r"calls?|times?|fold))\b"
    r"|(?:性能|耗时|延迟|收益|占比|比例|吞吐|速度|performance|latency|"
    r"benefit|ratio|throughput|speed)\s*(?:double|half)\b",
    re.IGNORECASE,
)


def _contains_free_written_number(text: str) -> bool:
    """Reject prose quantities without treating ordinary numbered words as metrics."""

    if (
        _IMPLICIT_NUMERIC_PROMISE.search(text)
        or _EMBEDDED_ASCII_QUANTITY.search(text)
        or _HALF_MEASUREMENT.search(text)
        or _SPELLED_ENGLISH_QUANTITY.search(text)
    ):
        return True
    masked = _TECHNICAL_VERSION_WITH_DIGIT.sub("", text)
    masked = _TECHNICAL_IDENTIFIER_WITH_DIGIT.sub("", masked)
    masked = _LEXICAL_NUMBER_WORD.sub("", masked)
    return bool(
        _FREE_WRITTEN_NUMBER.search(masked)
        or _BARE_CHINESE_QUANTITY.search(masked)
        or _CONTEXTUAL_CHINESE_QUANTITY.search(masked)
    )
_CONFIRMED_CAUSAL_LANGUAGE = re.compile(r"(?:已经|已)?确认(?:根因|原因|为)|确定为|根因是|直接导致|必然")


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
    schema_version: str
    evidence_ids: frozenset[str]
    finding_evidence: Mapping[str, frozenset[str]]
    finding_status: Mapping[str, str]
    scenario_metrics: Mapping[str, frozenset[str]]
    limitation_ids: frozenset[str]
    metric_ids: frozenset[str]
    numeric_spellings: frozenset[str]
    source_match: str
    source_refs: Mapping[str, Mapping[str, object]]
    workbench_finding_order: tuple[str, ...]
    workbench_finding_evidence: Mapping[str, tuple[str, ...]]
    workbench_finding_metrics: Mapping[str, tuple[str, ...]]
    workbench_finding_sources: Mapping[str, tuple[str, ...]]
    workbench_finding_status: Mapping[str, str]
    workbench_finding_ceiling: Mapping[str, str]
    workbench_evidence_ids: frozenset[str]
    workbench_metric_ids: frozenset[str]
    critical_path_evidence_ids: frozenset[str]


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
        metric_ids: set[str] = set()

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
                metric_ids.add(metric_id)
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
        source_context = document.get("source_context")
        source_match = "none"
        source_refs: dict[str, Mapping[str, object]] = {}
        if isinstance(source_context, dict):
            source_match = str(source_context.get("match_summary"))
            fragments = source_context.get("fragments")
            if not isinstance(fragments, list):
                raise SynthesisValidationError
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    raise SynthesisValidationError
                source_ref_id = fragment.get("source_ref_id")
                if not isinstance(source_ref_id, str) or source_ref_id in source_refs:
                    raise SynthesisValidationError
                source_refs[source_ref_id] = MappingProxyType(dict(fragment))

        workbench_finding_order: tuple[str, ...] = ()
        workbench_finding_evidence: dict[str, tuple[str, ...]] = {}
        workbench_finding_metrics: dict[str, tuple[str, ...]] = {}
        workbench_finding_sources: dict[str, tuple[str, ...]] = {}
        workbench_finding_status: dict[str, str] = {}
        workbench_finding_ceiling: dict[str, str] = {}
        workbench_evidence_ids: set[str] = set()
        workbench_metric_ids: set[str] = set()
        critical_path_evidence_ids: set[str] = set()
        if document.get("schema_version") == "2.1":
            workbench = document.get("workbench")
            if not isinstance(workbench, dict):
                raise SynthesisValidationError
            findings = workbench.get("findings")
            metrics = workbench.get("metrics")
            evidence = workbench.get("evidence")
            critical_path = workbench.get("critical_path")
            if not all(isinstance(value, list) for value in (findings, metrics, evidence, critical_path)):
                raise SynthesisValidationError
            order: list[str] = []
            for finding in findings:
                if not isinstance(finding, dict):
                    raise SynthesisValidationError
                finding_id = finding.get("finding_id")
                evidence_refs = finding.get("evidence_ids")
                metric_refs = finding.get("metric_ids")
                source_refs_for_finding = finding.get("source_ref_ids")
                if (
                    not isinstance(finding_id, str)
                    or finding_id in workbench_finding_evidence
                    or not all(
                        isinstance(value, list)
                        and all(isinstance(item, str) for item in value)
                        for value in (evidence_refs, metric_refs, source_refs_for_finding)
                    )
                    or not isinstance(finding.get("status"), str)
                    or not isinstance(finding.get("confidence_ceiling"), str)
                ):
                    raise SynthesisValidationError
                order.append(finding_id)
                workbench_finding_evidence[finding_id] = tuple(evidence_refs)
                workbench_finding_metrics[finding_id] = tuple(metric_refs)
                workbench_finding_sources[finding_id] = tuple(source_refs_for_finding)
                workbench_finding_status[finding_id] = str(finding["status"])
                workbench_finding_ceiling[finding_id] = str(finding["confidence_ceiling"])
            workbench_finding_order = tuple(order)
            for metric in metrics:
                if not isinstance(metric, dict) or not isinstance(metric.get("metric_id"), str):
                    raise SynthesisValidationError
                workbench_metric_ids.add(metric["metric_id"])
            for item in evidence:
                if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
                    raise SynthesisValidationError
                workbench_evidence_ids.add(item["evidence_id"])
            for segment in critical_path:
                if not isinstance(segment, dict) or not isinstance(segment.get("evidence_ids"), list):
                    raise SynthesisValidationError
                if not all(isinstance(item, str) for item in segment["evidence_ids"]):
                    raise SynthesisValidationError
                critical_path_evidence_ids.update(segment["evidence_ids"])
    except SynthesisValidationError:
        raise
    except Exception:
        raise SynthesisValidationError from None

    return _ProjectionIndex(
        schema_version=str(document["schema_version"]),
        evidence_ids=frozenset(evidence_ids),
        finding_evidence=MappingProxyType(finding_evidence),
        finding_status=MappingProxyType(finding_status),
        scenario_metrics=MappingProxyType(
            {scenario_type: frozenset(metric_ids) for scenario_type, metric_ids in scenario_metrics.items()}
        ),
        limitation_ids=frozenset(limitation_ids),
        metric_ids=frozenset(metric_ids),
        numeric_spellings=frozenset(numeric_spellings),
        source_match=source_match,
        source_refs=MappingProxyType(source_refs),
        workbench_finding_order=workbench_finding_order,
        workbench_finding_evidence=MappingProxyType(workbench_finding_evidence),
        workbench_finding_metrics=MappingProxyType(workbench_finding_metrics),
        workbench_finding_sources=MappingProxyType(workbench_finding_sources),
        workbench_finding_status=MappingProxyType(workbench_finding_status),
        workbench_finding_ceiling=MappingProxyType(workbench_finding_ceiling),
        workbench_evidence_ids=frozenset(workbench_evidence_ids),
        workbench_metric_ids=frozenset(workbench_metric_ids),
        critical_path_evidence_ids=frozenset(critical_path_evidence_ids),
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
    if index.schema_version == "2.0":
        if document.get("schema_version") != "2.0" or not _known_ids(
            document.get("key_metric_ids"), index.metric_ids
        ):
            raise SynthesisValidationError
        _validate_conclusions(document, index)
        _validate_source_fixes(document, index)
    elif index.schema_version == "2.1":
        if document.get("schema_version") != "2.1" or not _known_ids(
            document.get("key_metric_ids"), index.workbench_metric_ids
        ):
            raise SynthesisValidationError
        _validate_v21_conclusions(document, index)
        _validate_source_fixes(document, index)

    allowed_finding_evidence: Mapping[str, tuple[str, ...] | frozenset[str]] = (
        index.workbench_finding_evidence
        if index.schema_version == "2.1"
        else index.finding_evidence
    )
    allowed_evidence_ids = (
        index.workbench_evidence_ids
        if index.schema_version == "2.1"
        else index.evidence_ids
    )

    for finding in top_findings:
        if not isinstance(finding, dict):
            raise SynthesisValidationError
        finding_id = finding.get("finding_id")
        evidence_ids = finding.get("evidence_ids")
        if (
            not isinstance(finding_id, str)
            or finding_id not in allowed_finding_evidence
            or not _known_ids(evidence_ids, allowed_evidence_ids)
            or not isinstance(evidence_ids, list)
            or not set(evidence_ids).issubset(allowed_finding_evidence[finding_id])
        ):
            raise SynthesisValidationError

    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise SynthesisValidationError
        finding_ids = recommendation.get("finding_ids")
        evidence_ids = recommendation.get("evidence_ids")
        if not _known_ids(finding_ids, allowed_finding_evidence) or not _known_ids(
            evidence_ids, allowed_evidence_ids
        ):
            raise SynthesisValidationError
        if not finding_ids or not evidence_ids or any(
            (
                index.workbench_finding_status[finding_id]
                if index.schema_version == "2.1"
                else index.finding_status[finding_id]
            )
            not in {"confirmed", "suspected", "hypothesis"}
            for finding_id in finding_ids
        ):
            raise SynthesisValidationError
        recommendation_evidence = set(evidence_ids)
        supported_evidence = set().union(
            *(allowed_finding_evidence[finding_id] for finding_id in finding_ids)
        )
        if not recommendation_evidence.issubset(supported_evidence) or any(
            not recommendation_evidence.intersection(
                allowed_finding_evidence[finding_id]
            )
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
        if index.schema_version == "2.1" and _contains_free_written_number(text):
            raise SynthesisValidationError
        if index.schema_version != "2.1" and any(
            token.group(0) not in index.numeric_spellings
            for token in _NUMERIC_TOKEN.finditer(text)
        ):
            raise SynthesisValidationError
        if index.source_match != "strong":
            folded = text.casefold()
            if _SOURCE_PATH_TOKEN.search(text) or any(
                term and term in folded for term in _unverified_source_terms(index)
            ):
                raise SynthesisValidationError


def _validate_v21_conclusions(
    document: dict[str, object], index: _ProjectionIndex
) -> None:
    conclusions = document.get("conclusions")
    if not isinstance(conclusions, list):
        raise SynthesisValidationError
    conclusion_ids = tuple(
        item.get("finding_id") if isinstance(item, dict) else None
        for item in conclusions
    )
    if conclusion_ids != index.workbench_finding_order:
        raise SynthesisValidationError
    for conclusion in conclusions:
        if not isinstance(conclusion, dict):
            raise SynthesisValidationError
        finding_id = conclusion["finding_id"]
        evidence_ids = conclusion.get("evidence_ids")
        source_ref_ids = conclusion.get("source_ref_ids")
        claim_refs = conclusion.get("claim_refs")
        if (
            not isinstance(finding_id, str)
            or not isinstance(evidence_ids, list)
            or tuple(evidence_ids) != index.workbench_finding_evidence[finding_id]
            or not isinstance(source_ref_ids, list)
            or tuple(source_ref_ids) != index.workbench_finding_sources[finding_id]
            or not isinstance(claim_refs, list)
            or not claim_refs
        ):
            raise SynthesisValidationError
        for claim in claim_refs:
            if not isinstance(claim, dict):
                raise SynthesisValidationError
            claim_type = claim.get("claim_type")
            metric_id = claim.get("metric_id")
            evidence_id = claim.get("evidence_id")
            if metric_id is not None:
                if (
                    claim_type not in {"metric_over_threshold", "metric_observed"}
                    or metric_id not in index.workbench_metric_ids
                    or metric_id not in index.workbench_finding_metrics[finding_id]
                    or evidence_id is not None
                ):
                    raise SynthesisValidationError
            elif (
                claim_type not in {"evidence_on_critical_path", "evidence_supports_mechanism"}
                or evidence_id not in index.workbench_evidence_ids
                or evidence_id not in index.workbench_finding_evidence[finding_id]
            ):
                raise SynthesisValidationError
            if (
                claim_type == "evidence_on_critical_path"
                and evidence_id not in index.critical_path_evidence_ids
            ):
                raise SynthesisValidationError
        if (
            index.workbench_finding_status[finding_id] != "confirmed"
            or index.workbench_finding_ceiling[finding_id] in {"low", "none"}
        ) and any(
            _CONFIRMED_CAUSAL_LANGUAGE.search(str(conclusion[field]))
            for field in ("problem", "cause", "source_root_cause", "recommendation")
        ):
            raise SynthesisValidationError


def _validate_conclusions(
    document: dict[str, object], index: _ProjectionIndex
) -> None:
    conclusions = document.get("conclusions")
    if not isinstance(conclusions, list):
        raise SynthesisValidationError
    expected = {
        finding_id
        for finding_id, state in index.finding_status.items()
        if state in {"confirmed", "suspected"}
        and index.finding_evidence[finding_id]
    }
    selected: set[str] = set()
    for conclusion in conclusions:
        if not isinstance(conclusion, dict):
            raise SynthesisValidationError
        finding_id = conclusion.get("finding_id")
        evidence_ids = conclusion.get("evidence_ids")
        source_ref_ids = conclusion.get("source_ref_ids")
        if (
            not isinstance(finding_id, str)
            or finding_id in selected
            or finding_id not in expected
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or not _known_ids(evidence_ids, index.evidence_ids)
            or not set(evidence_ids).issubset(index.finding_evidence[finding_id])
            or not isinstance(source_ref_ids, list)
            or not _known_ids(source_ref_ids, index.source_refs)
            or index.source_match != "strong"
            and source_ref_ids
        ):
            raise SynthesisValidationError
        eligible_source_refs = {
            source_ref_id
            for source_ref_id, source_ref in index.source_refs.items()
            if source_ref.get("match_grade") == "strong"
            and finding_id in source_ref.get("finding_ids", [])
            and set(evidence_ids).issubset(source_ref.get("evidence_ids", []))
        }
        if eligible_source_refs and not source_ref_ids:
            raise SynthesisValidationError
        for source_ref_id in source_ref_ids:
            source_ref = index.source_refs[source_ref_id]
            if (
                source_ref.get("match_grade") != "strong"
                or finding_id not in source_ref.get("finding_ids", [])
                or not set(evidence_ids).issubset(source_ref.get("evidence_ids", []))
            ):
                raise SynthesisValidationError
        selected.add(finding_id)
    if selected != expected:
        raise SynthesisValidationError


def _unverified_source_terms(index: _ProjectionIndex) -> frozenset[str]:
    terms: set[str] = set()
    for source_ref in index.source_refs.values():
        relative_path = source_ref.get("relative_path")
        symbol = source_ref.get("symbol")
        if isinstance(relative_path, str) and relative_path:
            terms.add(relative_path.casefold())
            terms.add(
                relative_path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            )
        if isinstance(symbol, str) and symbol:
            terms.add(symbol.casefold())
    return frozenset(terms)


def _validate_source_fixes(
    document: dict[str, object], index: _ProjectionIndex
) -> None:
    source_fixes = document.get("source_fixes")
    recommendations = document.get("recommendations")
    if not isinstance(source_fixes, list) or not isinstance(recommendations, list):
        raise SynthesisValidationError
    if index.source_match != "strong" and source_fixes:
        raise SynthesisValidationError
    recommendation_by_priority = {
        item.get("priority"): item for item in recommendations if isinstance(item, dict)
    }
    fix_ids: set[str] = set()
    fix_bindings: set[tuple[str, str, str | None]] = set()
    for fix in source_fixes:
        if not isinstance(fix, dict):
            raise SynthesisValidationError
        fix_id = fix.get("fix_id")
        finding_id = fix.get("finding_id")
        evidence_ids = fix.get("evidence_ids")
        source_ref_ids = fix.get("source_ref_ids")
        rule_id = fix.get("rule_id")
        priority = fix.get("recommendation_priority")
        if (
            not isinstance(fix_id, str)
            or fix_id in fix_ids
            or not isinstance(finding_id, str)
            or finding_id not in index.finding_evidence
            or not _known_ids(evidence_ids, index.evidence_ids)
            or not isinstance(evidence_ids, list)
            or not isinstance(source_ref_ids, list)
            or not source_ref_ids
            or not all(ref_id in index.source_refs for ref_id in source_ref_ids)
            or not isinstance(rule_id, str)
            or priority not in recommendation_by_priority
            or fix.get("validation_profile_id") is not None
        ):
            raise SynthesisValidationError
        relative_path = fix.get("relative_path")
        symbol = fix.get("symbol")
        if not isinstance(relative_path, str) or not (
            isinstance(symbol, str) or symbol is None
        ):
            raise SynthesisValidationError
        binding = (finding_id, relative_path, symbol)
        if binding in fix_bindings:
            raise SynthesisValidationError
        fix_bindings.add(binding)
        fix_ids.add(fix_id)
        recommendation = recommendation_by_priority[priority]
        if (
            not isinstance(recommendation, dict)
            or finding_id not in recommendation.get("finding_ids", [])
            or not set(evidence_ids).issubset(recommendation.get("evidence_ids", []))
        ):
            raise SynthesisValidationError
        for ref_id in source_ref_ids:
            source_ref = index.source_refs[ref_id]
            if (
                source_ref.get("match_grade") != "strong"
                or source_ref.get("relative_path") != fix.get("relative_path")
                or source_ref.get("symbol") != fix.get("symbol")
                or finding_id not in source_ref.get("finding_ids", [])
                or not set(evidence_ids).issubset(source_ref.get("evidence_ids", []))
                or rule_id not in source_ref.get("rule_ids", [])
            ):
                raise SynthesisValidationError


def _narrative_fields(document: dict[str, object]) -> tuple[str, ...]:
    fields = [document["executive_summary"]]
    if document.get("schema_version") in {"2.0", "2.1"}:
        fields.append(document["verdict"])
        for conclusion in document["conclusions"]:
            fields.extend(
                (
                    conclusion["problem"],
                    conclusion["cause"],
                    conclusion["source_root_cause"],
                    conclusion["recommendation"],
                )
            )
        for fix in document["source_fixes"]:
            fields.extend((fix["diagnosis"], fix["retest_target"]))
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


def salvage_synthesis_source_fixes(
    *, projection: AIProjection, candidate: object
) -> AISynthesisOutput:
    """Restore safe source bindings and keep only independently valid source fixes."""

    try:
        if type(candidate) is bytes:
            if not 1 <= len(candidate) <= DEFAULT_MAX_CANDIDATE_BYTES:
                raise SynthesisValidationError
            document = json.loads(
                candidate.decode("utf-8", errors="strict"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        else:
            document = json.loads(canonical_json_bytes(candidate))
        if not isinstance(document, dict) or document.get("schema_version") != "2.0":
            raise SynthesisValidationError
        reject_private_json(document)
        index = _projection_index(projection)
        conclusions = document.get("conclusions")
        if not isinstance(conclusions, list):
            raise SynthesisValidationError
        repaired_conclusions: list[object] = []
        for conclusion in conclusions:
            if not isinstance(conclusion, dict):
                raise SynthesisValidationError
            repaired = dict(conclusion)
            if repaired.get("source_ref_ids") == []:
                finding_id = repaired.get("finding_id")
                evidence_ids = repaired.get("evidence_ids")
                if isinstance(finding_id, str) and isinstance(evidence_ids, list):
                    eligible = next(
                        (
                            source_ref_id
                            for source_ref_id, source_ref in index.source_refs.items()
                            if source_ref.get("match_grade") == "strong"
                            and finding_id in source_ref.get("finding_ids", [])
                            and set(evidence_ids).issubset(
                                source_ref.get("evidence_ids", [])
                            )
                        ),
                        None,
                    )
                    if eligible is not None:
                        repaired["source_ref_ids"] = [eligible]
            repaired_conclusions.append(repaired)
        document["conclusions"] = repaired_conclusions
        source_fixes = document.get("source_fixes")
        if not isinstance(source_fixes, list):
            raise SynthesisValidationError
        base = dict(document)
        base["source_fixes"] = []
        result = validate_synthesis_output(projection=projection, candidate=base)
        accepted: list[object] = []
        for source_fix in source_fixes:
            attempted = dict(base)
            attempted["source_fixes"] = [*accepted, source_fix]
            try:
                result = validate_synthesis_output(
                    projection=projection,
                    candidate=attempted,
                )
            except SynthesisValidationError:
                continue
            accepted.append(source_fix)
        return result
    except SynthesisValidationError:
        raise
    except Exception:
        raise SynthesisValidationError from None


ValidatedSynthesisOutput = AISynthesisOutput

__all__ = [
    "AISynthesisOutput",
    "DEFAULT_MAX_CANDIDATE_BYTES",
    "SynthesisValidationError",
    "ValidatedSynthesisOutput",
    "parse_candidate",
    "salvage_synthesis_source_fixes",
    "validate_synthesis_output",
]
