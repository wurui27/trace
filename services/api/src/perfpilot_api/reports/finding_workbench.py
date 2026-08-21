from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from uuid import UUID, uuid5


_FINDING_NAMESPACE = UUID("4d8de355-1b14-4e48-9156-44f51f7ad1d3")
_IMPACT_POINTS = {
    "critical": 40,
    "warning": 28,
    "healthy": 8,
    "informational": 4,
}
_EVIDENCE_POINTS = {"E0": 0, "E1": 6, "E2": 12, "E3": 16, "E4": 20}
_ATTRIBUTION_POINTS = {"low": 4, "medium": 12, "high": 20}
_CONFIDENCE = {"none": "low", "low": "low", "medium": "medium", "high": "high"}
_SOURCE_STATES = {
    "strong": "matched",
    "weak": "mismatch",
    "none": "mismatch",
}
_MAX_LINKED_IDS = 20
_EXTERNAL_SYSTEM_PROCESS_MARKERS = (
    "system_server",
    "surfaceflinger",
    "/vendor/bin/",
)
_EXTERNAL_SYSTEM_OWNERSHIP_MARKERS = (
    "系统层",
    "系统侧",
    "框架侧",
    "非应用可控",
    "应用不可直接",
    "应用不可直接控制",
)
_EXTERNAL_SYSTEM_MECHANISM_MARKERS = (
    "内部 monitor",
    "内部锁",
    "锁竞争",
    "内部 gc",
    "gc 暂停",
    "系统线程阻塞",
)
_APPLICATION_OWNERSHIP_MARKERS = (
    "根因在应用侧",
    "应用侧根因",
    "应用重复",
    "应用主线程重复",
    "应用循环",
    "应用同步调用",
    "应用发起",
    "应用主动触发",
    "根因是调用时机",
    "应用主线程同步调用",
    "主线程同步 binder",
    "主线程同步 ipc",
    "应用内部",
    "应用代码内部",
    "业务线程内部",
    "主线程内部",
    "工作线程内部",
    "渲染线程内部",
    "应用进程内部",
    "发生在应用进程",
)


def _invalid() -> ValueError:
    return ValueError("finding workbench input is invalid")


def _canonical_key(parts: tuple[str, ...]) -> str:
    normalized = "\u001f".join(part.strip().casefold() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def stable_finding_id(
    *,
    scenario_type: str,
    mechanism: str,
    root_cause_domain: str,
    responsible_component: str,
) -> str:
    key = _canonical_key(
        (scenario_type, mechanism, root_cause_domain, responsible_component)
    )
    return str(uuid5(_FINDING_NAMESPACE, key))


def priority_score(
    *,
    impact_points: int,
    evidence_grade: str,
    attribution: str,
    critical_path_points: int,
    reproducibility_points: int,
) -> int:
    if (
        impact_points not in _IMPACT_POINTS.values()
        or evidence_grade not in _EVIDENCE_POINTS
        or attribution not in _ATTRIBUTION_POINTS
        or not 0 <= critical_path_points <= 20
        or not 0 <= reproducibility_points <= 15
    ):
        raise _invalid()
    return min(
        100,
        impact_points
        + _EVIDENCE_POINTS[evidence_grade]
        + _ATTRIBUTION_POINTS[attribution]
        + critical_path_points
        + reproducibility_points,
    )


def build_capabilities(
    *,
    core_document: Mapping[str, object],
    source_context: Mapping[str, object] | None,
) -> dict[str, str]:
    if not isinstance(core_document.get("scenario_reports"), list):
        raise _invalid()
    source = "not_requested"
    if source_context is not None:
        summary = source_context.get("match_summary")
        if not isinstance(summary, str) or summary not in _SOURCE_STATES:
            raise _invalid()
        source = _SOURCE_STATES[summary]
    return {
        "trace": "available"
        if core_document.get("core_state") in {"complete", "partial"}
        else "unavailable",
        "smartperfetto": "available",
        "source": source,
        "ai": "pending",
    }


def build_report_quality(
    *,
    core_document: Mapping[str, object],
    source_context: Mapping[str, object] | None,
    synthesis_state: str,
    patch_validation_state: str,
) -> dict[str, object]:
    scenarios = core_document.get("scenario_reports")
    if not isinstance(scenarios, list):
        raise _invalid()

    required_reasons: list[str] = []
    optional_reasons: list[str] = []
    trace_unusable = core_document.get("core_state") not in {"complete", "partial"}
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise _invalid()
        health = scenario.get("trace_health")
        capabilities = scenario.get("trace_capabilities")
        if not isinstance(health, Mapping) or not isinstance(capabilities, list):
            raise _invalid()
        if health.get("parse_status") != "parsed":
            trace_unusable = True
        for capability in capabilities:
            if not isinstance(capability, Mapping):
                raise _invalid()
            name = capability.get("name")
            required = capability.get("required")
            status = capability.get("status")
            if not isinstance(name, str) or not isinstance(required, bool):
                raise _invalid()
            if status == "available":
                continue
            reason = f"{'required' if required else 'optional'}_capability_{name}_{status}"
            (required_reasons if required else optional_reasons).append(reason)

    trace_state = "unusable" if trace_unusable else "complete"
    if trace_state != "unusable" and (
        core_document.get("core_state") == "partial" or required_reasons
    ):
        trace_state = "partial"

    source_state = "not_requested"
    if source_context is not None:
        summary = source_context.get("match_summary")
        if summary == "strong":
            source_state = "available_strong"
        elif summary in {"weak", "none"}:
            source_state = "available_weak"
        else:
            raise _invalid()

    return {
        "trace_core_state": trace_state,
        "report_validation_state": "passed",
        "synthesis_state": synthesis_state,
        "source_correlation_state": source_state,
        "patch_validation_state": patch_validation_state,
        "reason_codes": sorted(set(required_reasons))
        + sorted(set(optional_reasons)),
        "scenarios": [_scenario_quality(scenario) for scenario in scenarios],
    }


def build_finding_workbench(
    *,
    core_document: Mapping[str, object],
    source_context: Mapping[str, object] | None,
    package_name: str,
    duration_seconds: int,
    environment_fingerprint: str,
) -> dict[str, object]:
    scenarios = core_document.get("scenario_reports")
    if (
        not isinstance(scenarios, list)
        or not package_name
        or not 1 <= duration_seconds <= 3600
        or not environment_fingerprint.startswith("sha256:")
        or len(environment_fingerprint) != 71
    ):
        raise _invalid()

    strong_fragments = _strong_fragments(source_context)
    findings = _merge_findings(scenarios, strong_fragments)
    # Keep SmartPerfetto's source order when two findings have the same score.  The
    # finding UUID is an identity, not a severity signal, so using it as a tie-break
    # can promote a later secondary cause above an earlier primary problem.
    findings.sort(key=lambda item: -int(item["priority_score"]))
    primary = [
        str(item["finding_id"])
        for item in findings
        if item["status"] == "confirmed" and item["priority"] in {"p0", "p1"}
    ][:3]
    return {
        "critical_path": _critical_path(scenarios),
        "metrics": _metric_views(scenarios),
        "evidence": _evidence_views(scenarios),
        "findings": findings,
        "primary_finding_ids": primary,
        "retest_plans": _retest_plans(
            findings,
            package_name=package_name,
            duration_seconds=duration_seconds,
            environment_fingerprint=environment_fingerprint,
        ),
    }


def _strong_fragments(
    source_context: Mapping[str, object] | None,
) -> list[Mapping[str, object]]:
    if source_context is None:
        return []
    fragments = source_context.get("fragments")
    if not isinstance(fragments, list):
        raise _invalid()
    if source_context.get("match_summary") != "strong":
        return []
    result: list[Mapping[str, object]] = []
    for fragment in fragments:
        if not isinstance(fragment, Mapping) or fragment.get("match_grade") != "strong":
            raise _invalid()
        result.append(fragment)
    return result


def _merge_findings(
    scenarios: list[object],
    strong_fragments: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    scenario_by_finding: dict[str, Mapping[str, object]] = {}
    original_ids_by_finding: dict[str, set[str]] = {}
    impact_points_by_finding: dict[str, int] = {}
    attribution_by_finding: dict[str, str] = {}
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise _invalid()
        scenario_type = scenario.get("scenario_type")
        evidence = scenario.get("evidence")
        raw_findings = scenario.get("findings")
        metrics = scenario.get("metrics")
        if (
            not isinstance(scenario_type, str)
            or not isinstance(evidence, list)
            or not isinstance(raw_findings, list)
            or not isinstance(metrics, list)
        ):
            raise _invalid()
        evidence_by_id = {
            item["evidence_id"]: item
            for item in evidence
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
        }
        for raw in raw_findings:
            if not isinstance(raw, Mapping):
                raise _invalid()
            original_id = raw.get("finding_id")
            rule_id = raw.get("rule_id")
            kind = raw.get("kind")
            if not all(isinstance(value, str) for value in (original_id, rule_id, kind)):
                raise _invalid()
            component = str(rule_id).rsplit(".", 1)[0]
            finding_id = stable_finding_id(
                scenario_type=scenario_type,
                mechanism=str(rule_id),
                root_cause_domain=str(kind),
                responsible_component=component,
            )
            scenario_by_finding.setdefault(finding_id, scenario)
            original_ids_by_finding.setdefault(finding_id, set()).add(str(original_id))
            evidence_ids = _string_ids(raw.get("evidence_ids"))[:_MAX_LINKED_IDS]
            grade = _evidence_grade(raw, evidence_ids, evidence_by_id)
            attribution = _CONFIDENCE.get(str(raw.get("confidence")))
            severity = str(raw.get("severity"))
            if attribution is None or severity not in _IMPACT_POINTS:
                raise _invalid()
            impact_points_by_finding[finding_id] = max(
                impact_points_by_finding.get(finding_id, 0),
                _IMPACT_POINTS[severity],
            )
            previous_attribution = attribution_by_finding.get(finding_id)
            if (
                previous_attribution is None
                or _ATTRIBUTION_POINTS[attribution]
                > _ATTRIBUTION_POINTS[previous_attribution]
            ):
                attribution_by_finding[finding_id] = attribution
            contribution = _critical_path_contribution(
                evidence_ids,
                evidence_by_id=evidence_by_id,
                scenario=scenario,
            )
            score = priority_score(
                impact_points=_IMPACT_POINTS[severity],
                evidence_grade=grade,
                attribution=attribution,
                critical_path_points=round(contribution * 20),
                reproducibility_points=4,
            )
            confirmed = raw.get("status") == "confirmed" and grade in {"E2", "E3", "E4"}
            candidate: dict[str, object] = {
                "finding_id": finding_id,
                "scenario_type": scenario_type,
                "title": str(raw.get("title")),
                "problem": str(raw.get("title")),
                "impact": _impact_sentence(severity),
                "mechanism": str(rule_id),
                "root_cause": (
                    str(raw.get("summary"))
                    if kind == "root_cause"
                    else "当前只能确认性能症状，尚未确认源码根因。"
                ),
                "critical_path_contribution": contribution,
                "priority": _priority(score),
                "priority_score": score,
                "evidence_ids": evidence_ids,
                "metric_ids": [],
                "source_ref_ids": [],
                "status": "confirmed" if confirmed else "hypothesis",
                "confidence": {
                    "data_completeness": _scenario_completeness(scenario),
                    "evidence_grade": grade,
                    "attribution": attribution,
                    "statistical": "single_sample",
                },
                "confidence_ceiling": str(raw.get("confidence_ceiling")),
                "confirmed_items": [str(raw.get("summary"))] if confirmed else [],
                "unconfirmed_items": ["单次样本尚未验证波动范围"],
                "exclusions": _closed_exclusions(raw.get("exclusions")),
                "engine_recommendation": raw.get("recommendation"),
                "engine_retest": str(raw.get("retest")),
                "retest_plan_id": str(uuid5(_FINDING_NAMESPACE, f"retest:{finding_id}")),
            }
            current = merged.get(finding_id)
            if current is None:
                merged[finding_id] = candidate
                continue
            current["evidence_ids"] = sorted(
                set(_string_ids(current["evidence_ids"])) | set(evidence_ids)
            )[:_MAX_LINKED_IDS]
            if candidate["status"] == "confirmed":
                current["status"] = "confirmed"
            current["critical_path_contribution"] = _critical_path_contribution(
                _string_ids(current["evidence_ids"]),
                evidence_by_id=evidence_by_id,
                scenario=scenario,
            )
            score = priority_score(
                impact_points=_IMPACT_POINTS[severity],
                evidence_grade=_merged_evidence_grade(
                    _string_ids(current["evidence_ids"]), evidence_by_id
                ),
                attribution=attribution,
                critical_path_points=round(
                    float(current["critical_path_contribution"]) * 20
                ),
                reproducibility_points=4,
            )
            current["priority_score"] = max(int(current["priority_score"]), score)
            current["priority"] = _priority(int(current["priority_score"]))
            current["confidence"]["evidence_grade"] = _merged_evidence_grade(  # type: ignore[index]
                _string_ids(current["evidence_ids"]), evidence_by_id
            )
    for finding_id, finding in merged.items():
        scenario = scenario_by_finding[finding_id]
        evidence = scenario.get("evidence")
        metrics = scenario.get("metrics")
        if not isinstance(evidence, list) or not isinstance(metrics, list):
            raise _invalid()
        evidence_by_id = {
            item["evidence_id"]: item
            for item in evidence
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
        }
        evidence_ids = _string_ids(finding["evidence_ids"])
        direct_metrics = _metric_ids_for_evidence(scenario, evidence_ids)
        finding["metric_ids"] = direct_metrics
        original_ids = original_ids_by_finding[finding_id]
        finding["source_ref_ids"] = sorted(
            {
                str(fragment["source_ref_id"])
                for fragment in strong_fragments
                if isinstance(fragment.get("source_ref_id"), str)
                and original_ids.intersection(fragment.get("finding_ids", []))
                and set(evidence_ids).intersection(fragment.get("evidence_ids", []))
            }
        )[:_MAX_LINKED_IDS]
        grade = _evidence_grade(finding, evidence_ids, evidence_by_id)
        contribution = _critical_path_contribution(
            evidence_ids,
            evidence_by_id=evidence_by_id,
            scenario=scenario,
        )
        attribution = attribution_by_finding[finding_id]
        score = priority_score(
            impact_points=impact_points_by_finding[finding_id],
            evidence_grade=grade,
            attribution=attribution,
            critical_path_points=round(contribution * 20),
            reproducibility_points=4,
        )
        if _is_external_system_finding(finding):
            score = min(score, 59)
        finding["critical_path_contribution"] = contribution
        finding["priority_score"] = score
        finding["priority"] = _priority(score)
        confidence = finding.get("confidence")
        if not isinstance(confidence, dict):
            raise _invalid()
        confidence["evidence_grade"] = grade
        confidence["attribution"] = attribution
    return list(merged.values())


def _is_external_system_finding(finding: Mapping[str, object]) -> bool:
    text = "\n".join(
        str(finding.get(field, ""))
        for field in ("title", "problem", "root_cause")
    ).casefold()
    clauses = re.split(r"[，。；！？\n]", text)
    system_subject = r"(?:system_server|surfaceflinger|系统层|系统侧|框架侧)"

    def negates_application_ownership(clause: str) -> bool:
        for marker in _APPLICATION_OWNERSHIP_MARKERS:
            escaped = re.escape(marker)
            if re.search(
                rf"(?:已排除|排除|不是|并非|未发现|未观察到|"
                rf"没有(?:证据表明)?|无法确认).{{0,8}}{escaped}",
                clause,
            ) or re.search(
                rf"{escaped}.{{0,8}}(?:已排除|排除|并不是根因|不是根因|"
                rf"并非根因|未发现|未观察到|尚未确认|无法确认|"
                rf"没有证据(?:表明|支持)?)",
                clause,
            ):
                return True
        return False

    if any(
        any(marker in clause for marker in _APPLICATION_OWNERSHIP_MARKERS)
        and not negates_application_ownership(clause)
        for clause in clauses
    ):
        return False

    def negates_system_ownership(clause: str) -> bool:
        if re.search(
            rf"{system_subject}.{{0,16}}(?:返回正常|无异常|已正常返回|调用正常|返回完成)",
            clause,
        ):
            return True
        if re.search(
            r"(?:未发现|未观察到|没有证据表明|无证据表明|无法确认)"
            r".{0,20}(?:system_server|surfaceflinger|系统层|系统侧|框架侧)",
            clause,
        ):
            return True
        system_match = re.search(
            r"(?:system_server|surfaceflinger|系统层|系统侧|框架侧)",
            clause,
        )
        if system_match is not None and re.search(
            r"(?:未发现|未观察到|尚未确认|无法确认|"
            r"没有证据(?:表明|支持)?)",
            clause[system_match.start() :],
        ):
            return True
        if any(marker in clause for marker in ("已排除", "排除")) and (
            any(marker in clause for marker in _EXTERNAL_SYSTEM_PROCESS_MARKERS)
            or any(marker in clause for marker in _EXTERNAL_SYSTEM_OWNERSHIP_MARKERS)
        ):
            return True
        if re.search(
            rf"(?:不是|并非|不属于|并不属于)\s*{system_subject}", clause
        ):
            return True
        return (
            re.search(
                rf"{system_subject}.{{0,12}}(?:不是|并非|不属于|并不属于)"
                r".{0,12}(?:问题|原因|根因|瓶颈|归因)",
                clause,
            )
            is not None
        )

    grounded_clauses = [
        clause
        for clause in clauses
        if not negates_system_ownership(clause)
    ]
    for clause in grounded_clauses:
        process_owned = any(
            marker in clause for marker in _EXTERNAL_SYSTEM_PROCESS_MARKERS
        )
        ownership_grounded = any(
            marker in clause for marker in _EXTERNAL_SYSTEM_OWNERSHIP_MARKERS
        ) or any(marker in clause for marker in _EXTERNAL_SYSTEM_MECHANISM_MARKERS)
        explicit_system_ownership = (
            any(marker in clause for marker in ("系统层", "系统侧", "框架侧"))
            and any(
                marker in clause
                for marker in (
                    "根因",
                    "瓶颈",
                    "阻塞",
                    "锁竞争",
                    "gc 暂停",
                    "应用不可直接",
                    "非应用可控",
                )
            )
        )
        if (process_owned and ownership_grounded) or explicit_system_ownership:
            return True
    has_system_subject = any(
        any(marker in clause for marker in _EXTERNAL_SYSTEM_PROCESS_MARKERS)
        for clause in grounded_clauses
    )
    has_system_mechanism = any(
        any(marker in clause for marker in _EXTERNAL_SYSTEM_MECHANISM_MARKERS)
        and not any(marker in clause for marker in _APPLICATION_OWNERSHIP_MARKERS)
        for clause in grounded_clauses
    )
    if has_system_subject and has_system_mechanism:
        return True
    return False


def _evidence_grade(
    finding: Mapping[str, object],
    evidence_ids: list[str],
    evidence_by_id: Mapping[str, Mapping[str, object]],
) -> str:
    if not evidence_ids:
        return "E0"
    if len(evidence_ids) >= 2 and all(
        _has_interval(evidence_by_id.get(evidence_id)) for evidence_id in evidence_ids
    ):
        return "E3"
    evidence = evidence_by_id.get(evidence_ids[0])
    if _has_direct_locator(evidence):
        return "E4"
    if finding.get("status") == "confirmed":
        return "E2"
    return "E1"


def _merged_evidence_grade(
    evidence_ids: list[str],
    evidence_by_id: Mapping[str, Mapping[str, object]],
) -> str:
    if len(evidence_ids) >= 2 and all(
        _has_interval(evidence_by_id.get(evidence_id)) for evidence_id in evidence_ids
    ):
        return "E3"
    return "E2"


def _has_interval(evidence: Mapping[str, object] | None) -> bool:
    return bool(
        evidence
        and isinstance(evidence.get("interval_start_ns"), int)
        and isinstance(evidence.get("interval_end_ns"), int)
        and int(evidence["interval_end_ns"]) >= int(evidence["interval_start_ns"])
    )


def _critical_path_contribution(
    evidence_ids: list[str],
    *,
    evidence_by_id: Mapping[str, Mapping[str, object]],
    scenario: Mapping[str, object],
) -> float:
    health = scenario.get("trace_health")
    if not isinstance(health, Mapping):
        return 0.0
    window = health.get("measurement_window")
    if not isinstance(window, Mapping):
        return 0.0
    window_start = window.get("start_ns")
    window_end = window.get("end_ns")
    if (
        type(window_start) is not int
        or type(window_end) is not int
        or window_end <= window_start
    ):
        return 0.0

    intervals: list[tuple[int, int]] = []
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        start = evidence.get("interval_start_ns")
        end = evidence.get("interval_end_ns")
        if type(start) is not int or type(end) is not int or end <= start:
            continue
        clipped_start = max(start, window_start)
        clipped_end = min(end, window_end)
        if clipped_end > clipped_start:
            intervals.append((clipped_start, clipped_end))
    if not intervals:
        return 0.0

    intervals.sort()
    covered_ns = 0
    merged_start, merged_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= merged_end:
            merged_end = max(merged_end, end)
            continue
        covered_ns += merged_end - merged_start
        merged_start, merged_end = start, end
    covered_ns += merged_end - merged_start
    return round(covered_ns / (window_end - window_start), 2)


def _has_direct_locator(evidence: Mapping[str, object] | None) -> bool:
    if not _has_interval(evidence) or evidence is None:
        return False
    fields = evidence.get("fields")
    return bool(
        isinstance(evidence.get("query_id"), str)
        and isinstance(fields, Mapping)
        and all(isinstance(fields.get(key), str) for key in ("process", "thread", "track", "slice"))
    )


def _scenario_completeness(scenario: Mapping[str, object]) -> str:
    capabilities = scenario.get("trace_capabilities")
    if not isinstance(capabilities, list):
        raise _invalid()
    for capability in capabilities:
        if (
            not isinstance(capability, Mapping)
            or not isinstance(capability.get("required"), bool)
        ):
            raise _invalid()
        if capability["required"] and capability.get("status") != "available":
            return "limited"
    return "complete"


def _scenario_quality(scenario: object) -> dict[str, object]:
    if not isinstance(scenario, Mapping):
        raise _invalid()
    health = scenario.get("trace_health")
    capabilities = scenario.get("trace_capabilities")
    if not isinstance(health, Mapping) or not isinstance(capabilities, list):
        raise _invalid()
    measurement_window = health.get("measurement_window")
    data_loss = health.get("data_loss")
    if not isinstance(measurement_window, Mapping) or not isinstance(data_loss, Mapping):
        raise _invalid()
    loss_categories = sorted(
        key
        for key, value in data_loss.items()
        if isinstance(key, str) and isinstance(value, int) and value > 0
    )
    projected_capabilities: list[dict[str, object]] = []
    for capability in capabilities:
        if not isinstance(capability, Mapping):
            raise _invalid()
        name = capability.get("name")
        required = capability.get("required")
        status = capability.get("status")
        if (
            not isinstance(name, str)
            or not isinstance(required, bool)
            or not isinstance(status, str)
        ):
            raise _invalid()
        projected_capabilities.append(
            {
                "name": name,
                "required": required,
                "status": status,
                "reason_code": _reason_code(capability.get("reason"), status=status),
            }
        )
    projected_capabilities.sort(key=lambda item: str(item["name"]))
    return {
        "scenario_type": scenario.get("scenario_type"),
        "parse_status": health.get("parse_status"),
        "measurement_window_coverage": measurement_window.get("coverage"),
        "data_loss_present": bool(loss_categories),
        "data_loss_categories": loss_categories,
        "capabilities": projected_capabilities,
    }


def _reason_code(value: object, *, status: str) -> str | None:
    if status == "available":
        return None
    if isinstance(value, str) and value and all(
        character.islower() or character.isdigit() or character in {"_", "."}
        for character in value
    ):
        return value
    return "capability_data_unavailable"


def _closed_exclusions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _invalid()
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _invalid()
        code = item.get("code")
        status = item.get("status")
        evidence_ids = item.get("evidence_ids")
        if not isinstance(code, str) or not isinstance(status, str):
            raise _invalid()
        result.append(
            {
                "code": code,
                "status": status,
                "evidence_ids": _string_ids(evidence_ids),
            }
        )
    return sorted(result, key=lambda item: (str(item["code"]), str(item["status"])))


def _metric_views(scenarios: list[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or not isinstance(scenario.get("metrics"), list):
            raise _invalid()
        evidence_by_metric, _ = _bounded_metric_evidence_links(scenario)
        for metric in scenario["metrics"]:
            if not isinstance(metric, Mapping) or not isinstance(metric.get("metric_id"), str):
                raise _invalid()
            result.append(
                {
                    "metric_id": metric["metric_id"],
                    "name": str(metric.get("name")),
                    "value": metric.get("numeric_value")
                    if metric.get("status") == "available"
                    else None,
                    "unit": metric.get("unit"),
                    "aggregation": "single_sample",
                    "scenario_type": scenario.get("scenario_type"),
                    "source": "smartperfetto",
                    "evidence_ids": evidence_by_metric[str(metric["metric_id"])],
                    "quality": "available"
                    if metric.get("status") == "available"
                    else "unavailable",
                }
            )
    return sorted(result, key=lambda item: str(item["metric_id"]))


def _locator(evidence: Mapping[str, object]) -> dict[str, object]:
    start_ns = evidence.get("interval_start_ns")
    end_ns = evidence.get("interval_end_ns")
    fields = evidence.get("fields")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int) or end_ns < start_ns:
        raise _invalid()
    if not isinstance(fields, Mapping):
        raise _invalid()
    return {
        "start_ns": start_ns,
        "end_ns": end_ns,
        "process": fields.get("process"),
        "thread": fields.get("thread"),
        "track": fields.get("track"),
        "slice": fields.get("slice"),
        "query_id": evidence.get("query_id"),
    }


def _evidence_views(scenarios: list[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or not isinstance(scenario.get("evidence"), list):
            raise _invalid()
        _, metrics_by_evidence = _bounded_metric_evidence_links(scenario)
        for evidence in scenario["evidence"]:
            if not isinstance(evidence, Mapping) or not isinstance(evidence.get("evidence_id"), str):
                raise _invalid()
            result.append(
                {
                    "evidence_id": evidence["evidence_id"],
                    "kind": "trace_interval" if _has_interval(evidence) else "metric",
                    "scenario_type": scenario.get("scenario_type"),
                    "metric_ids": metrics_by_evidence[str(evidence["evidence_id"])],
                    "summary": f"Trace 证据来自 {evidence.get('source')}。",
                    "source": str(evidence.get("source")),
                    "locator": _locator(evidence) if _has_interval(evidence) else None,
                }
            )
    return sorted(result, key=lambda item: str(item["evidence_id"]))


def _critical_path(scenarios: list[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for evidence in _evidence_views(scenarios):
        locator = evidence["locator"]
        if not isinstance(locator, Mapping):
            continue
        evidence_id = str(evidence["evidence_id"])
        result.append(
            {
                "segment_id": str(uuid5(_FINDING_NAMESPACE, f"segment:{evidence_id}")),
                "label": str(locator.get("slice") or evidence["source"]),
                "start_ns": locator["start_ns"],
                "end_ns": locator["end_ns"],
                "duration_ns": int(locator["end_ns"]) - int(locator["start_ns"]),
                "evidence_ids": [evidence_id],
            }
        )
    return sorted(result, key=lambda item: (int(item["start_ns"]), str(item["segment_id"])))


def _retest_plans(
    findings: list[dict[str, object]],
    *,
    package_name: str,
    duration_seconds: int,
    environment_fingerprint: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for finding in findings:
        metric_ids = finding["metric_ids"]
        result.append(
            {
            "retest_plan_id": finding["retest_plan_id"],
            "finding_id": finding["finding_id"],
            "scenario_type": finding["scenario_type"],
            "package_name": package_name,
            "duration_seconds": duration_seconds,
            "environment_fingerprint": environment_fingerprint,
            "metric_ids": metric_ids,
            "pass_criteria": [
                "使用相同环境复测，并确认关联指标优于当前基线。"
                if metric_ids
                else "在相同场景中确认该机制不再出现在关键路径，或补采到可直接量化的指标证据。"
            ],
            "notes": "使用相同设备、构建、场景和采集时长复测。",
            }
        )
    return result


def _string_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid()
    return sorted(set(value))


def _bounded_metric_evidence_links(
    scenario: Mapping[str, object],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    metrics = scenario.get("metrics")
    evidence = scenario.get("evidence")
    if not isinstance(metrics, list) or not isinstance(evidence, list):
        raise _invalid()
    metric_ids: list[str] = []
    evidence_ids: list[str] = []
    for metric in metrics:
        if not isinstance(metric, Mapping) or not isinstance(metric.get("metric_id"), str):
            raise _invalid()
        metric_ids.append(str(metric["metric_id"]))
    for item in evidence:
        if not isinstance(item, Mapping) or not isinstance(item.get("evidence_id"), str):
            raise _invalid()
        evidence_ids.append(str(item["evidence_id"]))
    available_evidence = frozenset(evidence_ids)
    candidates = sorted(
        {
            (evidence_id, str(metric["metric_id"]))
            for metric in metrics
            if isinstance(metric, Mapping)
            for evidence_id in _string_ids(metric.get("sample_ids"))
            if evidence_id in available_evidence
        }
    )
    evidence_by_metric = {metric_id: [] for metric_id in metric_ids}
    metrics_by_evidence = {evidence_id: [] for evidence_id in evidence_ids}
    for evidence_id, metric_id in candidates:
        metric_links = evidence_by_metric[metric_id]
        evidence_links = metrics_by_evidence[evidence_id]
        if (
            len(metric_links) >= _MAX_LINKED_IDS
            or len(evidence_links) >= _MAX_LINKED_IDS
        ):
            continue
        metric_links.append(evidence_id)
        evidence_links.append(metric_id)
    return evidence_by_metric, metrics_by_evidence


def _metric_ids_for_evidence(
    scenario: Mapping[str, object],
    evidence_ids: list[str],
) -> list[str]:
    _, metrics_by_evidence = _bounded_metric_evidence_links(scenario)
    return sorted(
        {
            metric_id
            for evidence_id in evidence_ids
            for metric_id in metrics_by_evidence.get(evidence_id, [])
        }
    )[:_MAX_LINKED_IDS]


def _priority(score: int) -> str:
    if score >= 80:
        return "p0"
    if score >= 60:
        return "p1"
    if score >= 40:
        return "p2"
    return "p3"


def _impact_sentence(severity: str) -> str:
    return {
        "critical": "该问题直接影响场景关键性能目标。",
        "warning": "该问题增加场景关键路径耗时。",
        "healthy": "该项当前未超过既有性能边界。",
        "informational": "该项用于补充解释当前性能环境。",
    }[severity]
