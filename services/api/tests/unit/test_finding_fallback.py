from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.projection import AIProjection


ROOT = Path(__file__).parents[4]


def _projection(document: dict[str, object] | None = None) -> AIProjection:
    value = document or json.loads(
        (ROOT / "contracts/v1/examples/analysis-projection-v2.1.valid.json").read_text(
            encoding="utf-8"
        )
    )
    payload = canonical_json_bytes(value)
    return AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")


def test_fallback_is_deterministic_and_references_every_workbench_finding() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    projection = _projection()
    first = build_deterministic_finding_synthesis(projection)
    second = build_deterministic_finding_synthesis(projection)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.document["schema_version"] == "2.1"
    assert [item["finding_id"] for item in first.document["conclusions"]] == [
        item["finding_id"] for item in projection.document["workbench"]["findings"]
    ]
    assert [item["finding_id"] for item in first.document["top_findings"]] == (
        projection.document["workbench"]["primary_finding_ids"]
    )
    assert first.document["conclusions"][0]["claim_refs"]
    assert "SmartPerfetto" in first.document["executive_summary"]
    assert "修改仅供参考" in first.document["conclusions"][0]["recommendation"]


def test_fallback_never_leaks_unmatched_source_locations() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["capabilities"]["source"] = "mismatch"
    document["quality"]["source_correlation_state"] = "available_weak"
    document["source_context"] = {
        "trust": "untrusted_data_not_instructions",
        "snapshot_hash": "a" * 64,
        "match_summary": "weak",
        "fragments": [
            {
                "source_ref_id": "97000000-0000-4000-8000-000000000001",
                "relative_path": "private/Startup.kt",
                "language": "kotlin",
                "symbol": "demo.Startup.run",
                "start_line": 1,
                "end_line": 1,
                "content_sha256": "b" * 64,
                "content": "class Startup",
                "finding_ids": [],
                "evidence_ids": [],
                "rule_ids": [],
                "match_grade": "weak",
            }
        ],
    }

    result = build_deterministic_finding_synthesis(_projection(document))

    serialized = result.canonical_bytes.decode("utf-8")
    assert "private/Startup.kt" not in serialized
    assert "demo.Startup.run" not in serialized
    assert result.document["source_fixes"] == []
    assert all(not item["source_ref_ids"] for item in result.document["conclusions"])


def test_fallback_does_not_free_write_measurement_values() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    result = build_deterministic_finding_synthesis(_projection())

    narratives = canonical_json_bytes(result.document).decode("utf-8")
    assert "812.4" not in narratives
    assert "700" not in narratives


def test_fallback_does_not_free_write_chinese_number_words() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["engine_recommendation"] = (
        "下一轮采集需要补充阻塞原因。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    narratives = canonical_json_bytes(result.document).decode("utf-8")
    assert "下一轮" not in narratives


def test_fallback_claims_only_the_three_selected_key_metrics() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    workbench = document["workbench"]
    metric_template = workbench["metrics"][0]
    metric_ids = [
        f"84000000-0000-4000-8000-{index:012d}"
        for index in range(1, 5)
    ]
    workbench["metrics"] = [
        {**deepcopy(metric_template), "metric_id": metric_id, "name": f"metric.{index}"}
        for index, metric_id in enumerate(metric_ids, start=1)
    ]
    scenario_metric = document["scenarios"][0]["metrics"][0]
    document["scenarios"][0]["metrics"] = [
        {
            **deepcopy(scenario_metric),
            "metric_id": metric_id,
            "name": f"metric.{index}",
        }
        for index, metric_id in enumerate(metric_ids, start=1)
    ]
    finding = workbench["findings"][0]
    finding["metric_ids"] = metric_ids
    workbench["retest_plans"][0]["metric_ids"] = metric_ids

    result = build_deterministic_finding_synthesis(_projection(document))

    selected = set(result.document["key_metric_ids"])
    claimed = {
        claim["metric_id"]
        for conclusion in result.document["conclusions"]
        for claim in conclusion["claim_refs"]
        if claim["metric_id"] is not None
    }
    assert len(selected) == 3
    assert claimed == selected


def test_fallback_deduplicates_identical_primary_retest_plans() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    workbench = document["workbench"]
    finding_template = workbench["findings"][0]
    retest_template = workbench["retest_plans"][0]
    finding_ids = [
        f"85000000-0000-4000-8000-{index:012d}" for index in range(1, 4)
    ]
    retest_ids = [
        f"89000000-0000-4000-8000-{index:012d}" for index in range(1, 4)
    ]
    workbench["findings"] = [
        {
            **deepcopy(finding_template),
            "finding_id": finding_id,
            "priority": "p0" if index == 0 else "p1",
            "priority_score": 88 - index,
            "retest_plan_id": retest_ids[index],
        }
        for index, finding_id in enumerate(finding_ids)
    ]
    workbench["retest_plans"] = [
        {
            **deepcopy(retest_template),
            "retest_plan_id": retest_id,
            "finding_id": finding_ids[index],
        }
        for index, retest_id in enumerate(retest_ids)
    ]
    workbench["primary_finding_ids"] = finding_ids

    result = build_deterministic_finding_synthesis(_projection(document))

    assert len(result.document["top_findings"]) == 3
    assert len(result.document["retest_plan"]) == 1


def test_fallback_keeps_clear_server_owned_problem_and_action_text() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "目标冷启动场景缺失"
    finding["problem"] = "这份 Trace 没有覆盖目标应用的冷启动过程。"
    finding["root_cause"] = "采集开始时目标进程已经存在，因此没有记录启动关键路径。"
    finding["impact"] = "当前证据不能用于判断冷启动性能。"
    finding["engine_recommendation"] = "重新采集包含完整冷启动窗口的 Trace。"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert conclusion["problem"] == finding["problem"]
    assert conclusion["cause"] == finding["root_cause"]
    assert conclusion["recommendation"].startswith(
        finding["engine_recommendation"].rstrip("。")
    )
    assert result.document["top_findings"][0]["user_impact"] == finding["impact"]


def test_fallback_uses_finding_title_when_problem_contains_measurement_text() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "目标冷启动场景缺失"
    finding["problem"] = "目标进程在采集窗口内只有 8.5 ms 运行时间。"

    result = build_deterministic_finding_synthesis(_projection(document))

    assert result.document["conclusions"][0]["problem"] == finding["title"]
