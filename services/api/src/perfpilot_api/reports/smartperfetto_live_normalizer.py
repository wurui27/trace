"""Project SmartPerfetto's live result contract into PerfPilot's core report.

SmartPerfetto keeps its own independently versioned envelope format. The strict
normalizer handles PerfPilot's narrow typed exchange contract; this compatibility
normalizer handles the live ``resultContract`` currently returned by an upstream
SmartPerfetto checkout without requiring changes in that checkout.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.normalizer import (
    NormalizedTraceReport,
    SmartPerfettoNormalizationError,
)
from perfpilot_api.services.canonical_result_reader import (
    LoadedCanonicalResult,
    validated_canonical_document,
)


_NAMESPACE = UUID("adfc4e83-e6b8-5b1a-84ac-a42f6027cd88")
_NORMALIZER_VERSION = "smartperfetto-live-normalizer-1"
_METRIC_HINT = re.compile(
    r"(?:^|_)(?:dur|duration|ttid|ttfd|latency|wait|cpu|freq|percent|pct|count|"
    r"jank|frame|time|value|score|rate)(?:_|$)"
)
_RECOMMENDATION = re.compile(r"\*{0,2}建议\*{0,2}[：:]\s*", re.IGNORECASE)
_INTERNAL_DIAGNOSTIC_LABEL = re.compile(r"(?<![A-Za-z0-9_.-])SR[0-9]+(?![A-Za-z0-9_.-])", re.IGNORECASE)
_UNCERTAIN_DIAGNOSTIC_MARKERS = (
    "疑似",
    "相关性",
    "聚合口径",
    "口径差异",
    "尚未证明",
    "候选",
    "需复核",
    "推测",
    "或许",
    "是否",
    "不作为独立根因",
)
_LIMITATION_TITLE_MARKERS = (
    "能力边界",
    "数据限制",
    "证据限制",
    "采集限制",
    "分析限制",
)


def _public_diagnostic_title(value: object, *, fallback: str) -> str:
    title = _text(value, fallback=fallback, limit=255)
    title = _INTERNAL_DIAGNOSTIC_LABEL.sub("", title)
    title = re.sub(r"\s*[—–-]\s*[：:]\s*", "：", title)
    title = re.sub(r"\s*[—–-]\s*(?=[（(])", "", title)
    title = re.sub(r"\s*[—–-]\s*(?:[/／|]\s*)*$", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" —–-/／|")
    return title or fallback


def _fail() -> SmartPerfettoNormalizationError:
    return SmartPerfettoNormalizationError()


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _fail()
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise _fail()
    return value


def _slug(value: object, *, fallback: str) -> str:
    rendered = re.sub(r"[^a-z0-9_.]+", "_", str(value).casefold()).strip("._")
    if not rendered or not rendered[0].isalpha():
        rendered = f"{fallback}_{rendered}".rstrip("_")
    return rendered[:128].rstrip("._") or fallback


def _text(value: object, *, fallback: str = "", limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.strip()[:limit]


def _stable(analysis_id: UUID, kind: str, source_id: str) -> str:
    return str(uuid5(_NAMESPACE, f"{analysis_id}:{kind}:{source_id}"))


def _finite(value: object) -> int | float | None:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _scenario_type(envelopes: Sequence[object]) -> str:
    sources = " ".join(
        _text(_mapping(item).get("meta") and _mapping(_mapping(item)["meta"]).get("source"))
        for item in envelopes
        if isinstance(item, Mapping) and isinstance(item.get("meta"), Mapping)
    ).casefold()
    if "startup" in sources or "launch" in sources:
        return "startup"
    if "scroll" in sources or "jank" in sources or "frame" in sources:
        return "scroll"
    return "startup"


def _identity(
    report: Mapping[str, object],
    envelopes: Sequence[object],
) -> dict[str, object]:
    candidates: list[Mapping[str, object]] = []
    resolutions = report.get("identityResolutions")
    if isinstance(resolutions, Sequence) and not isinstance(resolutions, str | bytes | bytearray):
        candidates.extend(item for item in resolutions if isinstance(item, Mapping))
    for raw in envelopes:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("meta"), Mapping):
            continue
        resolution = raw["meta"].get("identityResolution")
        if isinstance(resolution, Mapping):
            candidates.append(resolution)
    selected = next(
        (item for item in candidates if item.get("status") == "verified"),
        candidates[0] if candidates else {},
    )
    target = selected.get("target") if isinstance(selected.get("target"), Mapping) else {}
    processes = selected.get("processes")
    process = (
        next((item for item in processes if isinstance(item, Mapping)), {})
        if isinstance(processes, Sequence) and not isinstance(processes, str | bytes | bytearray)
        else {}
    )
    threads = selected.get("threads")
    thread_values = (
        [item for item in threads if isinstance(item, Mapping)]
        if isinstance(threads, Sequence) and not isinstance(threads, str | bytes | bytearray)
        else []
    )
    thread = next(
        (item for item in thread_values if item.get("role") == "app_main"),
        thread_values[0] if thread_values else {},
    )

    def integer(value: object) -> int | None:
        return value if type(value) is int else None

    return {
        "package_name": _text(target.get("packageName"), fallback="") or None,
        "process_name": _text(target.get("processName"), fallback="") or None,
        "upid": integer(process.get("upid")),
        "pid": integer(process.get("pid")),
        "main_thread_id": integer(thread.get("tid")),
    }


def _rows(envelope: Mapping[str, object]) -> tuple[list[str], list[object]] | None:
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return None
    columns = data.get("columns")
    rows = data.get("rows")
    if (
        not isinstance(columns, Sequence)
        or isinstance(columns, str | bytes | bytearray)
        or not isinstance(rows, Sequence)
        or isinstance(rows, str | bytes | bytearray)
        or not rows
        or not isinstance(rows[0], Sequence)
        or isinstance(rows[0], str | bytes | bytearray)
    ):
        return None
    names = [item for item in columns if isinstance(item, str)]
    first = list(rows[0])
    if len(names) != len(columns) or len(first) != len(names):
        return None
    return names, first


def _bounds(envelopes: Sequence[object]) -> tuple[int | None, int | None]:
    starts: list[int] = []
    ends: list[int] = []
    for raw in envelopes:
        if not isinstance(raw, Mapping):
            continue
        values = _rows(raw)
        if values is None:
            continue
        columns, row = values
        by_name = dict(zip(columns, row, strict=True))
        for name in ("start_ts", "perfetto_start", "ts"):
            value = by_name.get(name)
            if type(value) is int and value >= 0:
                starts.append(value)
                break
        for name in ("end_ts", "perfetto_end"):
            value = by_name.get(name)
            if type(value) is int and value >= 0:
                ends.append(value)
                break
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    if start is not None and end is not None and end < start:
        return None, None
    return start, end


def _display_columns(envelope: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    display = envelope.get("display")
    if not isinstance(display, Mapping):
        return {}
    raw = display.get("columns")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
        return {}
    return {
        str(item["name"]): item
        for item in raw
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }


def _unit(name: str, display: Mapping[str, object]) -> str:
    rendered = _text(display.get("unit"), limit=64)
    if rendered:
        return rendered
    if name.endswith("_ms"):
        return "ms"
    if name.endswith("_ns"):
        return "ns"
    if name.endswith("_pct") or "percent" in name:
        return "%"
    if name.endswith("count") or name.startswith("count"):
        return "count"
    if "freq" in name:
        return "MHz"
    return "value"


def _metric_candidate(name: str, value: object) -> bool:
    if _finite(value) is None:
        return False
    lowered = name.casefold()
    if lowered in {"ts", "start_ts", "end_ts", "dur_ns", "upid", "pid", "tid"}:
        return False
    if lowered.endswith("_id") or lowered == "id":
        return False
    return _METRIC_HINT.search(lowered) is not None


def _envelope_facts(
    analysis_id: UUID,
    artifact_id: UUID,
    scenario: str,
    envelopes: Sequence[object],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, list[str]],
    set[str],
    set[str],
]:
    evidence: list[dict[str, object]] = []
    metrics: list[dict[str, object]] = []
    evidence_by_artifact: dict[str, list[str]] = {}
    artifact_level_evidence_ids: set[str] = set()
    empty_artifact_ids: set[str] = set()
    metric_names: set[str] = set()

    def add_artifact_evidence(
        *,
        envelope_index: int,
        source_name: str,
        source_artifact_id: str,
        row_count: int,
    ) -> None:
        evidence_id = _stable(
            analysis_id,
            "live-envelope-artifact",
            f"{scenario}:{source_name}:{envelope_index}:{source_artifact_id}",
        )
        evidence.append(
            {
                "evidence_id": evidence_id,
                "source": "smartperfetto.live_envelope_artifact",
                "query_id": _slug(source_name, fallback="envelope"),
                "interval_start_ns": None,
                "interval_end_ns": None,
                "artifact_id": str(artifact_id),
                "fields": {
                    "source_artifact_id": source_artifact_id,
                    "row_count": row_count,
                },
            }
        )
        if row_count > 0:
            evidence_by_artifact.setdefault(source_artifact_id.casefold(), []).append(
                evidence_id
            )
            artifact_level_evidence_ids.add(evidence_id)
        else:
            empty_artifact_ids.add(source_artifact_id.casefold())

    for envelope_index, raw in enumerate(envelopes):
        if not isinstance(raw, Mapping):
            continue
        meta = raw.get("meta") if isinstance(raw.get("meta"), Mapping) else {}
        source_name = _text(meta.get("source"), fallback=f"envelope_{envelope_index}")
        source_artifact_id = _text(meta.get("artifactId"), limit=128)
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        raw_rows = data.get("rows")
        row_count = (
            len(raw_rows)
            if isinstance(raw_rows, Sequence)
            and not isinstance(raw_rows, str | bytes | bytearray)
            else None
        )
        values = _rows(raw)
        if values is None:
            if source_artifact_id and row_count is not None:
                add_artifact_evidence(
                    envelope_index=envelope_index,
                    source_name=source_name,
                    source_artifact_id=source_artifact_id,
                    row_count=row_count,
                )
            continue
        columns, row = values
        source_slug = _slug(source_name, fallback="envelope")
        evidence_id = _stable(
            analysis_id,
            "live-envelope",
            f"{scenario}:{source_name}:{envelope_index}",
        )
        fields: dict[str, object] = {}
        for column, value in zip(columns, row, strict=True):
            field_name = _slug(column, fallback="field")
            if field_name in fields or len(fields) >= 20:
                continue
            if value is None or isinstance(value, bool | int | float | str):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                fields[field_name] = value[:2000] if isinstance(value, str) else value
        if not fields:
            continue
        by_name = dict(zip(columns, row, strict=True))
        start = by_name.get("start_ts")
        end = by_name.get("end_ts")
        evidence.append(
            {
                "evidence_id": evidence_id,
                "source": "smartperfetto.live_envelope",
                "query_id": _slug(meta.get("stepId") or source_name, fallback="query"),
                "interval_start_ns": start if type(start) is int else None,
                "interval_end_ns": end if type(end) is int else None,
                "artifact_id": str(artifact_id),
                "fields": fields,
            }
        )
        if source_artifact_id:
            if row_count == 1:
                evidence_by_artifact.setdefault(
                    source_artifact_id.casefold(), []
                ).append(evidence_id)
            else:
                add_artifact_evidence(
                    envelope_index=envelope_index,
                    source_name=source_name,
                    source_artifact_id=source_artifact_id,
                    row_count=row_count or 0,
                )
        if row_count != 1:
            continue
        display_columns = _display_columns(raw)
        display = raw.get("display") if isinstance(raw.get("display"), Mapping) else {}
        title = _text(display.get("title"), fallback=source_name, limit=500)
        added = 0
        for column, value in zip(columns, row, strict=True):
            if len(metrics) >= 80 or added >= 4 or not _metric_candidate(column, value):
                continue
            column_slug = _slug(column, fallback="metric")
            name = _slug(f"{scenario}.{source_slug}.{column_slug}", fallback="metric")
            if name in metric_names:
                continue
            metric_names.add(name)
            column_display = display_columns.get(column, {})
            label = _text(column_display.get("label"), fallback=column, limit=200)
            metrics.append(
                {
                    "metric_id": _stable(analysis_id, "live-metric", name),
                    "name": name,
                    "status": "available",
                    "numeric_value": _finite(value),
                    "unit": _unit(column.casefold(), column_display),
                    "definition": f"{title}：{label}"[:1000],
                    "threshold": None,
                    "sample_ids": [evidence_id],
                }
            )
            added += 1
    return (
        evidence,
        metrics,
        evidence_by_artifact,
        artifact_level_evidence_ids,
        empty_artifact_ids,
    )


def _confidence(value: object) -> str:
    numeric = _finite(value)
    if numeric is None:
        return "medium"
    if numeric >= 0.8:
        return "high"
    if numeric >= 0.55:
        return "medium"
    if numeric > 0:
        return "low"
    return "none"


def _severity(value: object) -> str:
    return {
        "critical": "critical",
        "high": "critical",
        "warning": "warning",
        "medium": "warning",
        "low": "informational",
        "info": "informational",
    }.get(str(value).casefold(), "informational")


def _recommendation(diagnostic: Mapping[str, object], fallback: str | None) -> str | None:
    raw_evidence = diagnostic.get("evidence")
    if isinstance(raw_evidence, Sequence) and not isinstance(
        raw_evidence, str | bytes | bytearray
    ):
        for item in raw_evidence:
            if not isinstance(item, Mapping):
                continue
            text = _text(item.get("text"))
            match = _RECOMMENDATION.search(text)
            if match is not None:
                return text[match.end() :].strip()[:2000] or fallback
    if fallback and fallback.casefold().startswith("investigate:"):
        return f"排查并优化：{fallback.split(':', 1)[1].strip()}"[:2000]
    return fallback[:2000] if fallback else None


def _referenced_envelope_evidence_ids(
    text: str,
    evidence_by_artifact: Mapping[str, Sequence[str]],
) -> list[str]:
    referenced: list[str] = []
    for artifact_id, evidence_ids in evidence_by_artifact.items():
        if re.search(
            rf"(?<![a-z0-9_.-]){re.escape(artifact_id)}(?![a-z0-9_.-])",
            text,
            re.IGNORECASE,
        ) is None:
            continue
        for evidence_id in evidence_ids:
            if evidence_id not in referenced:
                referenced.append(evidence_id)
    return referenced[:20]


def _is_limitation_diagnostic(
    diagnostic: Mapping[str, object],
    *,
    title: str,
    description: str,
) -> bool:
    severity = str(diagnostic.get("severity", "")).casefold()
    text = f"{title}\n{description}"
    resolved = re.search(
        r"(?:已排除|排除).{0,12}(?:边界|限制)"
        r"|(?:边界|限制).{0,16}(?:(?:已经|现已|已).{0,6})?"
        r"(?:解除|消除|排除|不存在)"
        r"|(?:已经|现已|已)(?:证明)?不存在.{0,16}(?:边界|限制)",
        text,
    )
    if resolved is not None:
        return False
    if severity in {"info", "informational"} and (
        any(marker in title for marker in ("诊断说明", "采集说明", "采集状态"))
        or any(
            marker in description
            for marker in ("仅用于说明", "仅供说明", "说明采集状态")
        )
    ):
        return True
    if any(
        marker in title for marker in _LIMITATION_TITLE_MARKERS
    ):
        return True
    capability = (
        r"(?:thread-state|blocked_function|数据|指标|证据|字段|调用栈|轨迹|trace)"
    )
    return (
        re.search(rf"{capability}.{{0,24}}未采集", text, re.IGNORECASE) is not None
        or re.search(rf"未采集.{{0,24}}{capability}", text, re.IGNORECASE)
        is not None
    )


def _is_uncertain_diagnostic(text: str) -> bool:
    resolved_patterns = (
        r"(?:已排除|排除).*(?:相关性|疑似|候选|可能)",
        r"(?:聚合口径|口径差异).*(?:已校正|已核对|已验证|已确认)",
        r"无需复核",
        r"(?:不是|并非).*(?:候选|疑似|可能)",
        r"不再(?:是)?(?:疑似|候选|可能)",
        r"(?:相关性|聚合口径|口径差异|疑似|候选|需复核|可能)"
        r".{0,40}(?:已经|现已|已).{0,20}(?:确认|证明|排除|复核|校正)",
        r"不可能(?:是|由|导致|成为|影响)",
        r"不提示",
        r"(?:相关性并不存在|无相关性)",
        r"提示[：:].*(?:已确认|根因)",
    )
    title, _, description = text.partition("\n")

    def mechanisms(value: str) -> set[str]:
        folded = value.casefold()
        groups = {
            "binder": ("binder", "同步 ipc"),
            "jit": ("jit", "baseline profile"),
            "native": ("native", "dlopen", ".so"),
            "sdk": ("sdk",),
            "frame": ("首帧", "doframe", "compose", "measure"),
            "fork": ("fork", "调度延迟"),
            "system": ("system_server", "surfaceflinger", "系统层", "系统侧"),
        }
        return {
            name
            for name, markers in groups.items()
            if any(marker in folded for marker in markers)
        }

    title_mechanisms = mechanisms(title)
    description_mechanisms = mechanisms(description)
    confirmation_matches_title = (
        not title_mechanisms
        or not description_mechanisms
        or bool(title_mechanisms & description_mechanisms)
    )
    if (
        any(marker in title for marker in _UNCERTAIN_DIAGNOSTIC_MARKERS)
        and re.search(
            r"(?:(?:已经|现已|已).{0,12}(?:确认|证明|复核)|复核并确认|是根因)",
            description,
            re.IGNORECASE,
        )
        and not any(marker in description for marker in _UNCERTAIN_DIAGNOSTIC_MARKERS)
        and confirmation_matches_title
    ):
        return False
    for clause in re.split(
        r"[，。；！？\n]|(?:但是|然而|不过|同时|但|而|且)|提示[：:]",
        text,
    ):
        if not clause or any(re.search(pattern, clause) for pattern in resolved_patterns):
            continue
        if any(marker in clause for marker in _UNCERTAIN_DIAGNOSTIC_MARKERS):
            return True
        if re.search(
            r"(?:证据|trace|数据|结果|现象)(?:仅)?提示", clause, re.IGNORECASE
        ):
            return True
        if re.search(
            r"提示(?!用户|操作|页面|界面|文案|信息|弹窗|音|框)",
            clause,
        ):
            return True
        if re.search(
            r"(?:聚合|后台线程|并行|跨窗口).{0,16}口径"
            r"|口径.{0,16}(?:聚合|后台线程|并行|跨窗口)",
            clause,
            re.IGNORECASE,
        ):
            return True
        if re.search(r"可能(?:与|是|存在|影响|导致|造成|引发)", clause):
            return True
    return False


def _safe_multirow_diagnostic_narrative(
    *,
    title: str,
    description: str,
    has_artifact_level_evidence: bool,
) -> tuple[str, str]:
    if not has_artifact_level_evidence:
        return title, description
    text = f"{title}\n{description}".casefold()
    if "sdk" not in text or not any(
        marker in text
        for marker in ("application.oncreate", "bindapplication", "performcreate")
    ):
        return title, description
    safe_title = "外部 SDK 在启动主线程同步初始化"
    if (
        "qqmusicsdkadapter.doinit" in text
        and "xeaglebtadapter.start" in text
    ):
        return (
            safe_title,
            "Trace 显示 QQMusicSdkAdapter.doInit 与 XeagleBtAdapter.start "
            "都占用启动主线程；相关调用的生命周期入口必须分别定位。",
        )
    return (
        safe_title,
        "Trace 汇总支持外部 SDK 在启动主线程同步执行；"
        "具体调用的生命周期入口仍需分别核对。",
    )


def _diagnostic_facts(
    analysis_id: UUID,
    artifact_id: UUID,
    scenario: str,
    diagnostics: Sequence[object],
    actions: Sequence[object],
    evidence_by_artifact: Mapping[str, Sequence[str]],
    artifact_level_evidence_ids: set[str],
    empty_artifact_ids: set[str],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    action_by_diagnostic = {
        str(item["sourceDiagnosticId"]): _text(item.get("label"))
        for item in actions
        if isinstance(item, Mapping)
        and isinstance(item.get("sourceDiagnosticId"), str)
        and _text(item.get("label"))
    }
    evidence: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    limitations: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(diagnostics):
        if not isinstance(raw, Mapping):
            continue
        source_id = _text(raw.get("id"), fallback=f"diagnostic_{index}", limit=128)
        if source_id in seen:
            continue
        seen.add(source_id)
        title = _public_diagnostic_title(
            raw.get("title"), fallback=f"性能发现 {index + 1}"
        )
        description = _text(raw.get("description"), fallback=title)
        detail = ""
        evidence_texts: list[str] = []
        raw_evidence = raw.get("evidence")
        if isinstance(raw_evidence, Sequence) and not isinstance(
            raw_evidence, str | bytes | bytearray
        ):
            evidence_texts = [
                _text(item.get("text"))
                for item in raw_evidence
                if isinstance(item, Mapping) and _text(item.get("text"))
            ]
            detail = evidence_texts[0] if evidence_texts else ""
        diagnostic_text = "\n".join((title, description, *evidence_texts))
        evidence_ids = _referenced_envelope_evidence_ids(
            diagnostic_text,
            evidence_by_artifact,
        )
        references_empty_artifact = any(
            re.search(
                rf"(?<![a-z0-9_.-]){re.escape(source_artifact_id)}(?![a-z0-9_.-])",
                diagnostic_text,
                re.IGNORECASE,
            )
            is not None
            for source_artifact_id in empty_artifact_ids
        )
        title, summary = _safe_multirow_diagnostic_narrative(
            title=title,
            description=description,
            has_artifact_level_evidence=any(
                evidence_id in artifact_level_evidence_ids for evidence_id in evidence_ids
            ),
        )
        is_limitation = _is_limitation_diagnostic(
            raw,
            title=title,
            description=description,
        )
        if not evidence_ids:
            evidence_id = _stable(
                analysis_id,
                "live-diagnostic-evidence",
                f"{scenario}:{source_id}",
            )
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "source": "smartperfetto.diagnostic",
                    "query_id": _slug(source_id, fallback="diagnostic"),
                    "interval_start_ns": None,
                    "interval_end_ns": None,
                    "artifact_id": str(artifact_id),
                    "fields": {
                        "title": title,
                        "description": description,
                        "detail": detail,
                    },
                }
            )
            evidence_ids = [evidence_id]
        if is_limitation:
            limitations.append(
                {
                    "limitation_id": _stable(
                        analysis_id,
                        "live-diagnostic-limitation",
                        f"{scenario}:{source_id}",
                    ),
                    "code": _slug(
                        f"smartperfetto.diagnostic.{source_id}",
                        fallback="smartperfetto.diagnostic",
                    ),
                    "summary": summary,
                    "evidence_ids": evidence_ids,
                }
            )
            continue
        confidence = _confidence(raw.get("confidence"))
        uncertain = _is_uncertain_diagnostic(diagnostic_text) or references_empty_artifact
        if uncertain and confidence == "high":
            confidence = "medium"
        findings.append(
            {
                "finding_id": _stable(
                    analysis_id,
                    "live-finding",
                    f"{scenario}:{source_id}",
                ),
                "rule_id": _slug(f"smartperfetto.{source_id}", fallback="smartperfetto"),
                "kind": "symptom" if uncertain else "root_cause",
                "status": (
                    "suspected"
                    if uncertain
                    else "confirmed"
                    if confidence in {"high", "medium"}
                    else "suspected"
                ),
                "severity": _severity(raw.get("severity")),
                "confidence": confidence,
                "confidence_ceiling": confidence,
                "title": title,
                "summary": summary,
                "evidence_ids": evidence_ids,
                "exclusions": [],
                "recommendation": _recommendation(raw, action_by_diagnostic.get(source_id)),
                "retest": "在相同设备、构建和操作场景下重新采集 Trace，并比较关键指标。",
            }
        )
    return evidence, findings, limitations


def _build_core(
    source: LoadedCanonicalResult,
    *,
    analysis_mode: Literal["trace_upload", "device"],
) -> dict[str, object]:
    document = _mapping(validated_canonical_document(source))
    engine = _mapping(document.get("engine"))
    result = _mapping(document.get("result"))
    payload = _mapping(result.get("payload"))
    report = _mapping(payload.get("report"))
    contract = _mapping(report.get("resultContract"))
    if (
        document.get("schema_version") != "1.0"
        or document.get("analysis_id") != str(source.analysis_id)
        or document.get("artifact_id") != str(source.artifact_id)
        or engine.get("engine_id") != "smartperfetto"
        or engine.get("source_contract") != "workspace-agent-v1"
        or result.get("state") not in {"completed", "insufficient_data"}
        or contract.get("version") != "1.0.0"
    ):
        raise _fail()
    envelopes = _sequence(contract.get("dataEnvelopes"))
    diagnostics = _sequence(contract.get("diagnostics"))
    actions = _sequence(contract.get("actions"))
    if not envelopes:
        raise _fail()
    scenario = _scenario_type(envelopes)
    (
        envelope_evidence,
        metrics,
        evidence_by_artifact,
        artifact_level_evidence_ids,
        empty_artifact_ids,
    ) = _envelope_facts(
        source.analysis_id,
        source.artifact_id,
        scenario,
        envelopes,
    )
    diagnostic_evidence, findings, diagnostic_limitations = _diagnostic_facts(
        source.analysis_id,
        source.artifact_id,
        scenario,
        diagnostics,
        actions,
        evidence_by_artifact,
        artifact_level_evidence_ids,
        empty_artifact_ids,
    )
    envelope_evidence_by_id = {
        item["evidence_id"]: item for item in envelope_evidence
    }
    diagnostic_evidence_by_id = {
        item["evidence_id"]: item for item in diagnostic_evidence
    }
    available_evidence_ids = (
        envelope_evidence_by_id.keys() | diagnostic_evidence_by_id.keys()
    )
    retained_evidence_ids: set[str] = set()

    severity_rank = {"critical": 0, "warning": 1, "informational": 2}
    confidence_rank = {"high": 0, "medium": 1, "low": 2, "none": 3}
    ranked_findings = sorted(
        enumerate(findings),
        key=lambda pair: (
            severity_rank.get(str(pair[1].get("severity")), 4),
            confidence_rank.get(str(pair[1].get("confidence")), 4),
            0 if pair[1].get("status") == "confirmed" else 1,
            pair[0],
        ),
    )
    retained_finding_indexes: set[int] = set()
    for index, item in ranked_findings:
        if len(retained_finding_indexes) >= 80:
            break
        evidence_ids = set(item["evidence_ids"])  # type: ignore[arg-type]
        if not evidence_ids.issubset(available_evidence_ids):
            continue
        if len(retained_evidence_ids | evidence_ids) > 100:
            continue
        retained_evidence_ids.update(evidence_ids)
        retained_finding_indexes.add(index)
    findings = [
        item for index, item in enumerate(findings) if index in retained_finding_indexes
    ]

    retained_limitation_indexes: set[int] = set()
    for index, item in enumerate(diagnostic_limitations[:19]):
        evidence_ids = set(item["evidence_ids"])  # type: ignore[arg-type]
        if not evidence_ids.issubset(available_evidence_ids):
            continue
        if len(retained_evidence_ids | evidence_ids) > 100:
            continue
        retained_evidence_ids.update(evidence_ids)
        retained_limitation_indexes.add(index)
    diagnostic_limitations = [
        item
        for index, item in enumerate(diagnostic_limitations)
        if index in retained_limitation_indexes
    ]

    for item in envelope_evidence:
        if len(retained_evidence_ids) >= 100:
            break
        retained_evidence_ids.add(item["evidence_id"])

    diagnostic_evidence = [
        item
        for item in diagnostic_evidence
        if item["evidence_id"] in retained_evidence_ids
    ]
    envelope_evidence = [
        item
        for item in envelope_evidence
        if item["evidence_id"] in retained_evidence_ids
    ]
    metrics = [
        item
        for item in metrics
        if all(sample_id in retained_evidence_ids for sample_id in item["sample_ids"])
    ]
    if not envelope_evidence and not diagnostic_evidence:
        raise _fail()
    trace_start, trace_end = _bounds(envelopes)
    limitation_id = _stable(
        source.analysis_id,
        "live-limitation",
        f"{scenario}:quality-counters",
    )
    return {
        "schema_version": "1.0",
        "analysis_id": str(source.analysis_id),
        "analysis_mode": analysis_mode,
        "core_state": "partial",
        "scenario_reports": [
            {
                "scenario_id": _stable(source.analysis_id, "live-scenario", scenario),
                "scenario_type": scenario,
                "core_state": "partial",
                "metrics": metrics,
                "findings": findings,
                "evidence": diagnostic_evidence + envelope_evidence,
                "trace_health": {
                    "parse_status": "parsed",
                    "trace_start_ns": trace_start,
                    "trace_end_ns": trace_end,
                    "target_resolution": _identity(report, envelopes),
                    "measurement_window": {
                        "start_ns": trace_start,
                        "end_ns": trace_end,
                        "coverage": (
                            "complete"
                            if trace_start is not None and trace_end is not None
                            else "partial"
                        ),
                    },
                    "data_loss": {
                        "buffer_overruns": 0,
                        "ftrace_events_lost": 0,
                        "traced_buf_patches_failed": 0,
                        "incomplete_slices": 0,
                        "boundary_truncations": 0,
                    },
                    "frame_timeline_coverage": "insufficient_data",
                    "target_display_coverage": "insufficient_data",
                    "refresh_mode_coverage": "insufficient_data",
                },
                "trace_capabilities": [
                    {
                        "name": "smartperfetto_result_contract",
                        "required": True,
                        "status": "available",
                        "reason": None,
                    },
                    {
                        "name": "trace_quality_counters",
                        "required": False,
                        "status": "insufficient_data",
                        "reason": "当前实时报告未单独暴露 Trace 数据丢失计数。",
                    },
                ],
            }
        ],
        "limitations": [
            {
                "limitation_id": limitation_id,
                "code": "smartperfetto.live_contract_projection",
                "summary": (
                    "当前 SmartPerfetto 实时报告未单独暴露 Trace 数据丢失计数；"
                    "相关零值是协议占位，不能解释为已证明没有数据丢失。"
                ),
                "evidence_ids": [],
            },
            *diagnostic_limitations,
        ],
        "provenance": {
            "engine_id": "smartperfetto",
            "adapter_version": engine["adapter_version"],
            "engine_commit_sha": engine["source_commit_sha"],
            "engine_image_digest": engine["image_digest"],
            "source_contract": "workspace-agent-v1",
            "result_contract_version": "1.0.0",
            "canonical_artifact_id": str(source.artifact_id),
            "canonical_sha256_b64": source.sha256_b64,
            "normalizer_version": _NORMALIZER_VERSION,
        },
    }


def normalize_live_smartperfetto_result(
    source: LoadedCanonicalResult,
    *,
    analysis_mode: Literal["trace_upload", "device"] = "trace_upload",
) -> NormalizedTraceReport:
    try:
        validated = validate_contract(
            "normalized-trace-report",
            _build_core(source, analysis_mode=analysis_mode),
        )
        payload = canonical_json_bytes(validated)
        return NormalizedTraceReport(
            canonical_bytes=payload,
            sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
        )
    except SmartPerfettoNormalizationError:
        raise
    except Exception:
        raise SmartPerfettoNormalizationError from None


__all__ = ["normalize_live_smartperfetto_result"]
