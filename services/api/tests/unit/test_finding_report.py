from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).parents[4]


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts/v1/examples" / name).read_text(encoding="utf-8")
    )


def _base_report() -> dict[str, object]:
    report = _load("analysis-report-v1.3.valid.json")
    for key in ("capabilities", "quality", "workbench"):
        report.pop(key)
    report["schema_version"] = "1.2"
    return report


def test_composer_builds_v13_from_validated_projection_and_synthesis() -> None:
    from perfpilot_api.reports.finding_report import compose_finding_report

    projection = _load("analysis-projection-v2.1.valid.json")
    synthesis = _load("synthesis-output-v2.1.valid.json")
    base = _base_report()
    original = deepcopy(base)

    result = compose_finding_report(
        base_report=base,
        projection=projection,
        synthesis=synthesis,
        report_version=3,
    )

    assert base == original
    assert result["schema_version"] == "1.3"
    assert result["state"] == "completed"
    assert result["capabilities"]["ai"] == "available"
    assert result["quality"]["synthesis_state"] == "completed"
    assert result["workbench"] == projection["workbench"]
    assert result["synthesis"]["output"]["schema_version"] == "2.1"
    assert result["scenario_reports"][0]["result_state"] == "completed"


def test_composer_keeps_partial_trace_evidence_without_failure() -> None:
    from perfpilot_api.reports.finding_report import compose_finding_report

    projection = _load("analysis-projection-v2.1.valid.json")
    projection["quality"]["trace_core_state"] = "partial"
    projection["quality"]["reason_codes"] = ["required_capability_binder_unavailable"]
    projection["quality"]["scenarios"][0]["capabilities"].append(
        {
            "name": "binder",
            "required": True,
            "status": "unavailable",
            "reason_code": "trace_data_source_missing",
        }
    )

    result = compose_finding_report(
        base_report=_base_report(),
        projection=projection,
        synthesis=_load("synthesis-output-v2.1.valid.json"),
        report_version=3,
    )

    assert result["state"] == "partially_completed"
    scenario = result["scenario_reports"][0]
    assert scenario["result_state"] == "partially_completed"
    assert scenario["failure"] is None
    assert scenario["bundle"]["bundle_state"] == "partial"
    assert scenario["bundle"]["valid_measurement"] is True


def test_composer_marks_deterministic_fallback_without_partial_state() -> None:
    from perfpilot_api.reports.finding_report import compose_finding_report

    result = compose_finding_report(
        base_report=_base_report(),
        projection=_load("analysis-projection-v2.1.valid.json"),
        synthesis=_load("synthesis-output-v2.1.valid.json"),
        report_version=3,
        ai_mode="deterministic_fallback",
    )

    assert result["state"] == "completed"
    assert result["capabilities"]["ai"] == "deterministic_fallback"


def test_composer_preserves_non_primary_finding_conclusions() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis
    from perfpilot_api.reports.contracts import canonical_json_bytes
    from perfpilot_api.reports.finding_report import compose_finding_report
    from perfpilot_api.reports.projection import AIProjection

    projection = _load("analysis-projection-v2.1.valid.json")
    workbench = projection["workbench"]
    finding = deepcopy(workbench["findings"][0])
    finding["finding_id"] = "85000000-0000-4000-8000-000000000002"
    finding["title"] = "次要启动问题"
    finding["priority"] = "p2"
    finding["priority_score"] = 50
    finding["retest_plan_id"] = "89000000-0000-4000-8000-000000000002"
    workbench["findings"].append(finding)
    retest = deepcopy(workbench["retest_plans"][0])
    retest["retest_plan_id"] = finding["retest_plan_id"]
    retest["finding_id"] = finding["finding_id"]
    workbench["retest_plans"].append(retest)
    payload = canonical_json_bytes(projection)
    synthesis = build_deterministic_finding_synthesis(
        AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")
    )

    result = compose_finding_report(
        base_report=_base_report(),
        projection=projection,
        synthesis=synthesis.document,
        report_version=3,
        ai_mode="deterministic_fallback",
    )

    assert [item["finding_id"] for item in result["synthesis"]["output"]["conclusions"]] == [
        item["finding_id"] for item in result["workbench"]["findings"]
    ]
    assert result["workbench"]["primary_finding_ids"] == [
        "85000000-0000-4000-8000-000000000001"
    ]


def test_composer_accepts_three_grounded_top_findings_with_two_primary() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis
    from perfpilot_api.ai.synthesis import AISynthesisOutput
    from perfpilot_api.reports.contracts import canonical_json_bytes
    from perfpilot_api.reports.finding_report import compose_finding_report
    from perfpilot_api.reports.projection import AIProjection

    projection = _load("analysis-projection-v2.1.valid.json")
    workbench = projection["workbench"]
    for index in (2, 3):
        finding = deepcopy(workbench["findings"][0])
        finding["finding_id"] = f"85000000-0000-4000-8000-{index:012d}"
        finding["title"] = f"证据支持的补充问题 {index}"
        finding["status"] = "hypothesis"
        finding["priority_score"] = 60 - index
        finding["retest_plan_id"] = f"89000000-0000-4000-8000-{index:012d}"
        workbench["findings"].append(finding)
        retest = deepcopy(workbench["retest_plans"][0])
        retest["retest_plan_id"] = finding["retest_plan_id"]
        retest["finding_id"] = finding["finding_id"]
        workbench["retest_plans"].append(retest)
    payload = canonical_json_bytes(projection)
    synthesis = build_deterministic_finding_synthesis(
        AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")
    ).document
    synthesis["top_findings"] = [
        {
            "finding_id": finding["finding_id"],
            "evidence_ids": list(finding["evidence_ids"]),
            "user_impact": "该问题会拖慢当前测试场景的关键性能路径。",
        }
        for finding in workbench["findings"][:3]
    ]
    synthesis_payload = canonical_json_bytes(synthesis)

    result = compose_finding_report(
        base_report=_base_report(),
        projection=projection,
        synthesis=AISynthesisOutput(
            canonical_bytes=synthesis_payload,
            sha256_b64="Y2hlY2tzdW0=",
        ).document,
        report_version=3,
    )

    assert [
        item["finding_id"] for item in result["synthesis"]["output"]["top_findings"]
    ] == [item["finding_id"] for item in workbench["findings"][:3]]
    assert len(result["workbench"]["primary_finding_ids"]) == 1


def test_composer_accepts_metricless_finding_retest_without_inventing_metrics() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis
    from perfpilot_api.reports.finding_report import compose_finding_report
    from perfpilot_api.reports.normalizer import NormalizedTraceReport
    from perfpilot_api.reports.projection import build_ai_projection

    core_document = _load("normalized-trace-report.valid.json")
    projection = build_ai_projection(
        NormalizedTraceReport(
            canonical_bytes=json.dumps(
                core_document, separators=(",", ":"), sort_keys=True
            ).encode("utf-8"),
            sha256_b64="Y2hlY2tzdW0=",
        ),
        analysis_profile="startup",
        question=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=(
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
        schema_version="2.1",
    )
    synthesis = build_deterministic_finding_synthesis(projection)

    result = compose_finding_report(
        base_report=_base_report(),
        projection=projection.document,
        synthesis=synthesis.document,
        report_version=3,
        ai_mode="deterministic_fallback",
    )

    assert result["workbench"]["findings"][0]["metric_ids"] == []
    assert result["workbench"]["retest_plans"][0]["metric_ids"] == []
    assert result["synthesis"]["output"]["retest_plan"] == []
