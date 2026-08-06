"""Join allowlisted Android Memory facts into a SmartPerfetto core report."""

from __future__ import annotations

import base64
import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from perfpilot_api.engines.android_memory_contracts import AndroidMemoryContext
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.services.canonical_result_reader import (
    LoadedCanonicalResult,
    validated_canonical_document,
)


_JOIN_NAMESPACE = UUID("8eb858c8-f97b-5c92-b710-f2ab6c06c1cb")
_SUPPORT_LEVELS = frozenset({"insufficient", "limited", "supported", "strong"})
_COMPLETE_SUPPORT = frozenset({"supported", "strong"})
_INTENTS = frozenset(
    {
        "auto",
        "graphics",
        "java-leak",
        "native-memory",
        "quick-triage",
        "regression",
        "system-pressure",
    }
)
_UNAVAILABLE_REASONS = frozenset(
    {"execution_failed", "execution_canceled", "result_invalid", "result_unavailable"}
)
_ARTIFACT_TYPES = frozenset(
    {
        "analysis_report",
        "android_log",
        "comparison_report",
        "device_context",
        "dmabuf",
        "exit_info",
        "gfxinfo",
        "hprof",
        "meminfo",
        "native_heap_profile",
        "perfetto_trace",
        "phase_metadata",
        "pressure_memory",
        "previous_ai_context",
        "previous_analysis_report",
        "proc_meminfo",
        "qa_screenshot",
        "showmap",
        "smaps",
        "zram",
    }
)
_LEDGER_ROWS = {
    "TOTAL": "total",
    "Native Heap": "native_heap",
    "Dalvik Heap": "dalvik_heap",
    "Graphics": "graphics",
    "Code": "code",
    "Stack": "stack",
}
_LEDGER_FIELDS = {
    "pss_total_kb": ("pss_kb", "PSS"),
    "private_dirty_kb": ("private_dirty_kb", "Private Dirty"),
    "private_clean_kb": ("private_clean_kb", "Private Clean"),
    "swap_pss_kb": ("swap_pss_kb", "SwapPss"),
    "rss_total_kb": ("rss_kb", "RSS"),
}
_LEDGER_STATES = frozenset({"available", "ambiguous", "invalid", "unavailable"})
MemoryUnavailableReason = Literal[
    "execution_failed",
    "execution_canceled",
    "result_invalid",
    "result_unavailable",
]


class AndroidMemoryNormalizationError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("Android memory result cannot be normalized")


def _fail() -> AndroidMemoryNormalizationError:
    return AndroidMemoryNormalizationError()


def _stable_id(kind: str, analysis_id: UUID, source_id: str) -> str:
    return str(uuid5(_JOIN_NAMESPACE, f"{analysis_id}:{kind}:{source_id}"))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _fail()
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise _fail()
    return value


def _artifact_types(value: object) -> list[str]:
    result: set[str] = set()
    for item in _sequence(value):
        if not isinstance(item, str) or item not in _ARTIFACT_TYPES:
            raise _fail()
        result.add(item)
    return sorted(result)


def _nonnegative_number(value: object) -> int | float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise _fail()
    return value


def _trace_health() -> dict[str, object]:
    return {
        "parse_status": "not_applicable",
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
        "frame_timeline_coverage": "not_applicable",
        "target_display_coverage": "not_applicable",
        "refresh_mode_coverage": "not_applicable",
    }


def _loaded_memory_payload(
    source: LoadedCanonicalResult,
    *,
    analysis_id: UUID,
) -> tuple[dict[str, object], str]:
    if not isinstance(source, LoadedCanonicalResult) or source.analysis_id != analysis_id:
        raise _fail()
    document = _mapping(validated_canonical_document(source))
    engine = _mapping(document.get("engine"))
    result = _mapping(document.get("result"))
    if (
        engine.get("engine_id") != "android_memory"
        or engine.get("source_contract") != "android-memory-ai-context-1.2"
        or result.get("state") not in {"completed", "insufficient_data"}
    ):
        raise _fail()
    payload = _mapping(result.get("payload"))
    validated = AndroidMemoryContext.model_validate(payload, strict=True).model_dump(mode="json")
    if not isinstance(validated, dict):
        raise _fail()
    return validated, str(result["state"])


def _coverage_fields(payload: Mapping[str, object]) -> dict[str, object]:
    evidence = payload.get("evidence")
    if evidence is None:
        return {}
    coverage = _mapping(_mapping(evidence).get("coverage"))
    level = coverage.get("level")
    if level not in _SUPPORT_LEVELS:
        raise _fail()
    fields: dict[str, object] = {"coverage_level": level}
    for source_name, field_name in (
        ("available", "coverage_available"),
        ("missing_required", "coverage_missing_required"),
        ("missing_supporting", "coverage_missing_supporting"),
        ("inadequate", "coverage_inadequate"),
    ):
        raw = coverage.get(source_name, [])
        values = _artifact_types(raw)
        fields[f"{field_name}_count"] = len(values)
    missing_any_of = coverage.get("missing_any_of", [])
    groups = _sequence(missing_any_of)
    for group in groups:
        _artifact_types(group)
    fields["coverage_missing_any_of_count"] = len(groups)
    return fields


def _ledger(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    evidence = payload.get("evidence")
    if evidence is None:
        return None
    raw = _mapping(evidence).get("accounting_ledger")
    if raw is None:
        return None
    value = _mapping(raw)
    if value.get("status") not in _LEDGER_STATES:
        raise _fail()
    return value


def _memory_evidence(
    *,
    analysis_id: UUID,
    source: LoadedCanonicalResult,
    payload: Mapping[str, object],
) -> dict[str, object]:
    contract = _mapping(payload.get("analysis_contract"))
    generator = _mapping(payload.get("generator"))
    support = contract.get("support_level")
    primary_support = contract.get("primary_intent_support_level")
    if support not in _SUPPORT_LEVELS or primary_support not in _SUPPORT_LEVELS:
        raise _fail()
    fields: dict[str, object] = {
        "generator_version": generator.get("version"),
        "support_level": support,
        "primary_intent_support_level": primary_support,
    }
    fields.update(_coverage_fields(payload))
    ledger = _ledger(payload)
    fields["accounting_ledger_status"] = (
        ledger.get("status") if ledger is not None else "not_provided"
    )
    request = payload.get("request")
    if isinstance(request, Mapping):
        intent = request.get("intent")
        if isinstance(intent, str) and intent in _INTENTS:
            fields["intent"] = intent
    evidence_id = _stable_id("evidence", analysis_id, str(source.artifact_id))
    return {
        "evidence_id": evidence_id,
        "source": "android_memory.context",
        "query_id": "android_memory.context.v1_2",
        "interval_start_ns": None,
        "interval_end_ns": None,
        "artifact_id": str(source.artifact_id),
        "fields": fields,
    }


def _memory_metrics(
    *,
    analysis_id: UUID,
    payload: Mapping[str, object],
    evidence_id: str,
) -> list[dict[str, object]]:
    ledger = _ledger(payload)
    if ledger is None or ledger.get("status") != "available":
        return []
    if ledger.get("schema_version") != "1.0":
        raise _fail()
    rows = _sequence(ledger.get("rows"))
    selected: dict[str, Mapping[str, object]] = {}
    for raw in rows:
        row = _mapping(raw)
        name = row.get("name")
        if name not in _LEDGER_ROWS:
            continue
        if name in selected:
            raise _fail()
        selected[str(name)] = _mapping(row.get("meminfo"))
    if "TOTAL" not in selected:
        raise _fail()
    metrics: list[dict[str, object]] = []
    for row_name, row_slug in _LEDGER_ROWS.items():
        values = selected.get(row_name)
        if values is None:
            continue
        for field_name, (metric_slug, label) in _LEDGER_FIELDS.items():
            if field_name not in values:
                raise _fail()
            numeric_value = _nonnegative_number(values[field_name])
            source_id = f"{row_slug}.{metric_slug}"
            metrics.append(
                {
                    "metric_id": _stable_id("metric", analysis_id, source_id),
                    "name": f"memory.meminfo.{source_id}",
                    "status": "available",
                    "numeric_value": numeric_value,
                    "unit": "kB",
                    "definition": (
                        f"{label} reported by dumpsys meminfo for the {row_name} row."
                    ),
                    "threshold": None,
                    "sample_ids": [evidence_id],
                }
            )
    return sorted(metrics, key=lambda item: str(item["metric_id"]))


def _limitation(
    analysis_id: UUID,
    *,
    code: str,
    summary: str,
    evidence_ids: list[str],
) -> dict[str, object]:
    return {
        "limitation_id": _stable_id("limitation", analysis_id, code),
        "code": code,
        "summary": summary,
        "evidence_ids": evidence_ids,
    }


def _memory_limitations(
    *,
    analysis_id: UUID,
    payload: Mapping[str, object],
    result_state: str,
    evidence_id: str,
) -> list[dict[str, object]]:
    support = _mapping(payload.get("analysis_contract")).get("support_level")
    items: dict[str, dict[str, object]] = {}
    if support == "insufficient" or result_state == "insufficient_data":
        code = "android_memory.evidence_insufficient"
        items[code] = _limitation(
            analysis_id,
            code=code,
            summary="Android memory evidence is insufficient for a complete analysis.",
            evidence_ids=[evidence_id],
        )
    elif support == "limited":
        code = "android_memory.evidence_limited"
        items[code] = _limitation(
            analysis_id,
            code=code,
            summary="Android memory evidence supports only a limited analysis.",
            evidence_ids=[evidence_id],
        )
    ledger = _ledger(payload)
    ledger_state = str(ledger.get("status")) if ledger is not None else "not_provided"
    if ledger_state != "available":
        code = f"android_memory.ledger_{ledger_state}"
        items[code] = _limitation(
            analysis_id,
            code=code,
            summary="A usable Android meminfo accounting ledger is not available.",
            evidence_ids=[evidence_id],
        )
    next_evidence = payload.get("next_evidence", [])
    for raw in _sequence(next_evidence):
        artifact_type = _mapping(raw).get("artifact_type")
        if not isinstance(artifact_type, str) or artifact_type not in _ARTIFACT_TYPES:
            raise _fail()
        code = f"android_memory.missing_{artifact_type}"
        items[code] = _limitation(
            analysis_id,
            code=code,
            summary=f"Additional {artifact_type} evidence is requested by Android Memory.",
            evidence_ids=[evidence_id],
        )
    return [items[code] for code in sorted(items)][:20]


def _memory_scenario(
    *,
    analysis_id: UUID,
    source: LoadedCanonicalResult,
    payload: Mapping[str, object],
    result_state: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    evidence = _memory_evidence(
        analysis_id=analysis_id,
        source=source,
        payload=payload,
    )
    support = _mapping(payload.get("analysis_contract")).get("support_level")
    complete = result_state == "completed" and support in _COMPLETE_SUPPORT
    scenario = {
        "scenario_id": _stable_id("scenario", analysis_id, str(source.artifact_id)),
        "scenario_type": "memory_cycle",
        "core_state": "complete" if complete else "partial",
        "metrics": _memory_metrics(
            analysis_id=analysis_id,
            payload=payload,
            evidence_id=str(evidence["evidence_id"]),
        ),
        "findings": [],
        "evidence": [evidence],
        "trace_health": _trace_health(),
        "trace_capabilities": [
            {
                "name": "android_memory_evidence",
                "required": True,
                "status": "available" if complete else "insufficient_data",
                "reason": (
                    None
                    if complete
                    else "Android memory evidence support is incomplete."
                ),
            }
        ],
    }
    limitations = _memory_limitations(
        analysis_id=analysis_id,
        payload=payload,
        result_state=result_state,
        evidence_id=str(evidence["evidence_id"]),
    )
    return scenario, limitations


def _unavailable_scenario(
    analysis_id: UUID,
    *,
    reason: MemoryUnavailableReason,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if reason not in _UNAVAILABLE_REASONS:
        raise _fail()
    scenario = {
        "scenario_id": _stable_id("scenario", analysis_id, f"unavailable:{reason}"),
        "scenario_type": "memory_cycle",
        "core_state": "partial",
        "metrics": [],
        "findings": [],
        "evidence": [],
        "trace_health": _trace_health(),
        "trace_capabilities": [
            {
                "name": "android_memory_evidence",
                "required": True,
                "status": "unavailable",
                "reason": "Android memory analysis did not produce a usable result.",
            }
        ],
    }
    code = f"android_memory.{reason}"
    return scenario, [
        _limitation(
            analysis_id,
            code=code,
            summary="Android memory analysis did not produce a usable result.",
            evidence_ids=[],
        )
    ]


def _combine_unique(
    left: Sequence[object],
    right: Sequence[object],
    *,
    key: str,
    maximum: int,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[object] = set()
    for raw in (*left, *right):
        value = _mapping(raw)
        identifier = value.get(key)
        if identifier in seen:
            raise _fail()
        seen.add(identifier)
        result.append(dict(value))
    if len(result) > maximum:
        raise _fail()
    return result


def _join(
    core: NormalizedTraceReport,
    *,
    scenario: dict[str, object],
    memory_limitations: list[dict[str, object]],
) -> NormalizedTraceReport:
    if not isinstance(core, NormalizedTraceReport):
        raise _fail()
    document = validate_contract("normalized-trace-report", core.document)
    if document.get("analysis_mode") != "device":
        raise _fail()
    reports = _sequence(document.get("scenario_reports"))
    memory_indexes = [
        index
        for index, value in enumerate(reports)
        if _mapping(value).get("scenario_type") == "memory_cycle"
    ]
    if len(memory_indexes) > 1:
        raise _fail()
    joined_reports = [dict(_mapping(value)) for value in reports]
    if memory_indexes:
        index = memory_indexes[0]
        existing = joined_reports[index]
        merged = dict(existing)
        merged["core_state"] = (
            "complete"
            if existing.get("core_state") == scenario.get("core_state") == "complete"
            else "partial"
        )
        merged["metrics"] = _combine_unique(
            _sequence(existing.get("metrics")),
            _sequence(scenario.get("metrics")),
            key="metric_id",
            maximum=100,
        )
        merged["findings"] = _combine_unique(
            _sequence(existing.get("findings")),
            _sequence(scenario.get("findings")),
            key="finding_id",
            maximum=100,
        )
        merged["evidence"] = _combine_unique(
            _sequence(existing.get("evidence")),
            _sequence(scenario.get("evidence")),
            key="evidence_id",
            maximum=100,
        )
        existing_capabilities = [
            value
            for value in _sequence(existing.get("trace_capabilities"))
            if _mapping(value).get("name") != "android_memory_evidence"
        ]
        merged["trace_capabilities"] = _combine_unique(
            existing_capabilities,
            _sequence(scenario.get("trace_capabilities")),
            key="name",
            maximum=50,
        )
        joined_reports[index] = merged
    else:
        joined_reports.append(scenario)
    order = {"startup": 0, "scroll": 1, "memory_cycle": 2}
    joined_reports.sort(key=lambda value: order[str(value["scenario_type"])])
    document["scenario_reports"] = joined_reports
    document["core_state"] = (
        "partial"
        if any(value.get("core_state") == "partial" for value in joined_reports)
        else "complete"
    )
    existing_limitations = [dict(_mapping(value)) for value in _sequence(document["limitations"])]
    memory_ids = {value["limitation_id"] for value in memory_limitations}
    retained = [
        value
        for value in existing_limitations
        if value.get("limitation_id") not in memory_ids
    ][: max(0, 20 - len(memory_limitations))]
    document["limitations"] = sorted(
        [*retained, *memory_limitations],
        key=lambda value: str(value["limitation_id"]),
    )
    validated = validate_contract("normalized-trace-report", document)
    payload = canonical_json_bytes(validated)
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
    )


def join_android_memory_result(
    core: NormalizedTraceReport,
    source: LoadedCanonicalResult,
) -> NormalizedTraceReport:
    """Join a terminal, immutable Android Memory result into a device core."""

    try:
        document = validate_contract("normalized-trace-report", core.document)
        analysis_id = UUID(str(document.get("analysis_id")))
        payload, result_state = _loaded_memory_payload(source, analysis_id=analysis_id)
        scenario, limitations = _memory_scenario(
            analysis_id=analysis_id,
            source=source,
            payload=payload,
            result_state=result_state,
        )
        return _join(core, scenario=scenario, memory_limitations=limitations)
    except AndroidMemoryNormalizationError:
        raise
    except Exception:
        raise AndroidMemoryNormalizationError from None


def join_unavailable_android_memory(
    core: NormalizedTraceReport,
    *,
    reason: MemoryUnavailableReason,
) -> NormalizedTraceReport:
    """Preserve the SmartPerfetto report while making missing memory evidence explicit."""

    try:
        document = validate_contract("normalized-trace-report", core.document)
        analysis_id = UUID(str(document.get("analysis_id")))
        scenario, limitations = _unavailable_scenario(analysis_id, reason=reason)
        return _join(core, scenario=scenario, memory_limitations=limitations)
    except AndroidMemoryNormalizationError:
        raise
    except Exception:
        raise AndroidMemoryNormalizationError from None


__all__ = [
    "AndroidMemoryNormalizationError",
    "MemoryUnavailableReason",
    "join_android_memory_result",
    "join_unavailable_android_memory",
]
