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
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    evidence: list[dict[str, object]] = []
    metrics: list[dict[str, object]] = []
    metric_names: set[str] = set()
    for envelope_index, raw in enumerate(envelopes):
        if len(evidence) >= 70 or not isinstance(raw, Mapping):
            break
        values = _rows(raw)
        if values is None:
            continue
        columns, row = values
        meta = raw.get("meta") if isinstance(raw.get("meta"), Mapping) else {}
        source_name = _text(meta.get("source"), fallback=f"envelope_{envelope_index}")
        source_slug = _slug(source_name, fallback="envelope")
        evidence_id = _stable(analysis_id, "live-envelope", f"{source_name}:{envelope_index}")
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
    return evidence, metrics


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


def _diagnostic_facts(
    analysis_id: UUID,
    artifact_id: UUID,
    diagnostics: Sequence[object],
    actions: Sequence[object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    action_by_diagnostic = {
        str(item["sourceDiagnosticId"]): _text(item.get("label"))
        for item in actions
        if isinstance(item, Mapping)
        and isinstance(item.get("sourceDiagnosticId"), str)
        and _text(item.get("label"))
    }
    evidence: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(diagnostics):
        if len(findings) >= 80 or not isinstance(raw, Mapping):
            continue
        source_id = _text(raw.get("id"), fallback=f"diagnostic_{index}", limit=128)
        if source_id in seen:
            continue
        seen.add(source_id)
        title = _text(raw.get("title"), fallback=f"性能发现 {index + 1}", limit=255)
        description = _text(raw.get("description"), fallback=title)
        detail = ""
        raw_evidence = raw.get("evidence")
        if isinstance(raw_evidence, Sequence) and not isinstance(
            raw_evidence, str | bytes | bytearray
        ):
            detail = next(
                (
                    _text(item.get("text"))
                    for item in raw_evidence
                    if isinstance(item, Mapping) and _text(item.get("text"))
                ),
                "",
            )
        evidence_id = _stable(analysis_id, "live-diagnostic-evidence", source_id)
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
        confidence = _confidence(raw.get("confidence"))
        findings.append(
            {
                "finding_id": _stable(analysis_id, "live-finding", source_id),
                "rule_id": _slug(f"smartperfetto.{source_id}", fallback="smartperfetto"),
                "kind": "root_cause",
                "status": "confirmed" if confidence in {"high", "medium"} else "suspected",
                "severity": _severity(raw.get("severity")),
                "confidence": confidence,
                "confidence_ceiling": confidence,
                "title": title,
                "summary": description,
                "evidence_ids": [evidence_id],
                "exclusions": [],
                "recommendation": _recommendation(raw, action_by_diagnostic.get(source_id)),
                "retest": "在相同设备、构建和操作场景下重新采集 Trace，并比较关键指标。",
            }
        )
    return evidence, findings


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
    envelope_evidence, metrics = _envelope_facts(
        source.analysis_id,
        source.artifact_id,
        scenario,
        envelopes,
    )
    diagnostic_evidence, findings = _diagnostic_facts(
        source.analysis_id,
        source.artifact_id,
        diagnostics,
        actions,
    )
    envelope_evidence = envelope_evidence[: max(0, 100 - len(diagnostic_evidence))]
    retained_evidence_ids = {item["evidence_id"] for item in envelope_evidence}
    metrics = [
        item
        for item in metrics
        if all(sample_id in retained_evidence_ids for sample_id in item["sample_ids"])
    ]
    if not envelope_evidence and not diagnostic_evidence:
        raise _fail()
    trace_start, trace_end = _bounds(envelopes)
    limitation_id = _stable(source.analysis_id, "live-limitation", "quality-counters")
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
            }
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
