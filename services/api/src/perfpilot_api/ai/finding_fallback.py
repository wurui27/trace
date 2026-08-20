"""Build a deterministic Chinese synthesis when the configured AI is unusable."""

from __future__ import annotations

import re
from typing import Mapping

from perfpilot_api.ai.synthesis import AISynthesisOutput, validate_synthesis_output
from perfpilot_api.reports.projection import AIProjection


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _safe_chinese(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text or len(text) > 2000 or not _CJK.search(text) or any(
        character.isdigit() for character in text
    ):
        return fallback
    return text


def _claim_refs(
    finding: Mapping[str, object],
    critical_evidence_ids: frozenset[str],
    key_metric_ids: frozenset[str],
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    for metric_id in finding["metric_ids"]:  # type: ignore[union-attr]
        if metric_id not in key_metric_ids:
            continue
        claims.append(
            {
                "claim_type": "metric_observed",
                "metric_id": metric_id,
                "evidence_id": None,
            }
        )
    for evidence_id in finding["evidence_ids"]:  # type: ignore[union-attr]
        claims.append(
            {
                "claim_type": (
                    "evidence_on_critical_path"
                    if evidence_id in critical_evidence_ids
                    else "evidence_supports_mechanism"
                ),
                "metric_id": None,
                "evidence_id": evidence_id,
            }
        )
    return claims


def _conclusion(
    finding: Mapping[str, object],
    *,
    source_matched: bool,
    critical_evidence_ids: frozenset[str],
    key_metric_ids: frozenset[str],
) -> dict[str, object]:
    status = finding["status"]
    ceiling = finding["confidence_ceiling"]
    confirmed = status == "confirmed" and ceiling not in {"low", "none"}
    recommendation = _safe_chinese(
        finding.get("engine_recommendation"),
        "建议根据关联证据调整相关实现，并按相同环境复测。",
    )
    return {
        "finding_id": finding["finding_id"],
        "evidence_ids": list(finding["evidence_ids"]),  # type: ignore[arg-type]
        "source_ref_ids": (
            list(finding["source_ref_ids"])  # type: ignore[arg-type]
            if source_matched
            else []
        ),
        "claim_refs": _claim_refs(finding, critical_evidence_ids, key_metric_ids),
        "problem": _safe_chinese(
            finding.get("problem"),
            _safe_chinese(
                finding.get("title"), "检测到与该 Finding 对应的性能问题。"
            ),
        ),
        "cause": _safe_chinese(
            finding.get("root_cause"),
            (
                "SmartPerfetto 证据支持该机制与测试场景的关键路径相关。"
                if confirmed
                else "SmartPerfetto 证据提示该机制可能影响测试场景，当前仍需复测确认。"
            ),
        ),
        "source_root_cause": (
            "强匹配源码证据支持该问题与对应实现相关。"
            if source_matched and finding["source_ref_ids"]
            else "当前没有经过验证的匹配源码，只能判断 Trace 机制，不能定位代码根因。"
        ),
        "recommendation": f"{recommendation.rstrip('。')}；修改仅供参考。",
    }


def _key_metric_ids(
    findings: list[Mapping[str, object]], primary_ids: list[str]
) -> list[str]:
    primary = set(primary_ids)
    selected: list[str] = []
    for finding in findings:
        if finding["finding_id"] not in primary:
            continue
        for metric_id in finding["metric_ids"]:  # type: ignore[union-attr]
            if metric_id not in selected:
                selected.append(metric_id)
            if len(selected) == 3:
                return selected
    return selected


def _recommendations(
    conclusions: list[dict[str, object]],
    findings_by_id: Mapping[str, Mapping[str, object]],
    primary_ids: list[str],
) -> list[dict[str, object]]:
    conclusions_by_id = {item["finding_id"]: item for item in conclusions}
    result: list[dict[str, object]] = []
    used_priorities: set[str] = set()
    for finding_id in primary_ids:
        finding = findings_by_id[finding_id]
        priority = str(finding["priority"])
        if priority not in {"p0", "p1", "p2"} or priority in used_priorities:
            continue
        conclusion = conclusions_by_id[finding_id]
        result.append(
            {
                "priority": priority,
                "title": _safe_chinese(
                    finding.get("title"), "处理已识别的性能问题"
                ),
                "action": conclusion["recommendation"],
                "expected_effect": _safe_chinese(
                    finding.get("impact"),
                    "减少已识别机制对测试场景关键路径的影响。",
                ),
                "finding_ids": [finding_id],
                "evidence_ids": list(conclusion["evidence_ids"]),  # type: ignore[arg-type]
            }
        )
        used_priorities.add(priority)
    return sorted(result, key=lambda item: ("p0", "p1", "p2").index(str(item["priority"])))


def build_deterministic_finding_synthesis(
    projection: AIProjection,
) -> AISynthesisOutput:
    """Return one fail-closed, deterministic synthesis for projection 2.1."""

    document = projection.document
    if document.get("schema_version") != "2.1":
        raise ValueError("finding fallback input is invalid")
    workbench = document["workbench"]
    findings = list(workbench["findings"])
    findings_by_id = {item["finding_id"]: item for item in findings}
    primary_ids = list(workbench["primary_finding_ids"])
    key_metric_ids = _key_metric_ids(findings, primary_ids)
    critical_evidence_ids = frozenset(
        evidence_id
        for segment in workbench["critical_path"]
        for evidence_id in segment["evidence_ids"]
    )
    source_matched = document["capabilities"]["source"] == "matched"
    conclusions = [
        _conclusion(
            finding,
            source_matched=source_matched,
            critical_evidence_ids=critical_evidence_ids,
            key_metric_ids=frozenset(key_metric_ids),
        )
        for finding in findings
    ]
    conclusions_by_id = {item["finding_id"]: item for item in conclusions}
    retest_by_finding = {
        item["finding_id"]: item for item in workbench["retest_plans"]
    }
    candidate = {
        "schema_version": "2.1",
        "verdict": "分析完成",
        "executive_summary": "SmartPerfetto 证据已经整理为可执行的问题与复测建议。",
        "key_metric_ids": key_metric_ids,
        "conclusions": conclusions,
        "top_findings": [
            {
                "finding_id": finding_id,
                "evidence_ids": list(conclusions_by_id[finding_id]["evidence_ids"]),  # type: ignore[arg-type]
                "user_impact": _safe_chinese(
                    findings_by_id[finding_id].get("impact"),
                    "该问题影响当前测试场景的关键性能路径。",
                ),
            }
            for finding_id in primary_ids
        ],
        "recommendations": _recommendations(
            conclusions, findings_by_id, primary_ids
        ),
        "source_fixes": [],
        "retest_plan": [
            {
                "mode": "verify_metric",
                "scenario_type": retest_by_finding[finding_id]["scenario_type"],
                "metric_ids": list(retest_by_finding[finding_id]["metric_ids"]),
                "limitation_ids": [],
                "steps": "按相同应用、环境、测试场景和采集方式重新执行测试。",
                "success_condition": "improve_from_baseline",
                "failure_condition": "threshold_missed",
            }
            for finding_id in primary_ids
            if finding_id in retest_by_finding
            and retest_by_finding[finding_id]["metric_ids"]
            and retest_by_finding[finding_id]["scenario_type"]
            in {"startup", "scroll", "memory_cycle"}
        ],
        "limitations": [
            {
                "limitation_id": limitation["limitation_id"],
                "summary": "相关证据存在限制，结论已按可用证据范围收敛。",
            }
            for limitation in [
                *document["limitations"],
                *[
                    item
                    for scenario in document["scenarios"]
                    for item in scenario["limitations"]
                ],
            ][:20]
        ],
    }
    return validate_synthesis_output(projection=projection, candidate=candidate)


__all__ = ["build_deterministic_finding_synthesis"]
