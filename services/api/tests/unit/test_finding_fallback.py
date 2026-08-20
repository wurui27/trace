from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

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


def _set_scenario(document: dict[str, object], scenario_type: str) -> None:
    document["scenarios"][0]["scenario_type"] = scenario_type  # type: ignore[index]
    document["quality"]["scenarios"][0]["scenario_type"] = scenario_type  # type: ignore[index]
    document["workbench"]["findings"][0]["scenario_type"] = scenario_type  # type: ignore[index]
    document["workbench"]["retest_plans"][0]["scenario_type"] = scenario_type  # type: ignore[index]


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


def test_fallback_preserves_lexical_next_round_phrase() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["engine_recommendation"] = (
        "下一轮采集需要补充阻塞原因。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    narratives = canonical_json_bytes(result.document).decode("utf-8")
    assert "下一轮采集需要补充阻塞原因" in narratives


@pytest.mark.parametrize(
    "problem",
    [
        "发现四十二条长耗时。",
        "出现二十七次 JIT。",
        "发现三个热点。",
        "首帧有一百帧。",
        "CPU 占用百分之十。",
        "吞吐提升两倍。",
        "出现三项问题。",
        "执行五轮。",
        "每十帧发生卡顿。",
        "每百次出现异常。",
        "另两项问题。",
        "性能提升一百。",
        "耗时降低四十二。",
        "问题数量为二十七。",
        "排名下降三。",
        "收益提升两成。",
        "性能提升2x。",
        "耗时降低10milliseconds。",
        "占比减少20percent。",
        "排名提升Top10。",
        "启动速度提升一百。",
        "响应速度提升四十二。",
        "CPU 使用率降低二十七。",
        "卡顿率下降三。",
        "错误数减少一百。",
        "阻塞次数减少四十二。",
        "冷启动改善一百。",
        "整体提升一百。",
        "收益翻倍。",
        "耗时减半。",
        "占比降低10pct。",
        "吞吐提升10MBps。",
        "减少10frames。",
        "发生10calls。",
        "性能提升x2。",
        "性能提升10fold。",
        "延迟降到10millis。",
        "耗时10msecs。",
        "第一个问题最重要。",
        "第一项建议需要执行。",
        "第一轮复测通过。",
        "第一次采集失败。",
        "耗时 ten milliseconds。",
        "占比 twenty percent。",
        "排名 top ten。",
        "性能 double。",
        "耗时壹佰毫秒。",
        "占比贰拾％。",
        "性能提升双倍。",
        "耗时半秒。",
        "耗时 latency_500ms。",
        "占比 ratio_20percent。",
        "延迟 duration.999ms。",
        "吞吐 metric_2x。",
        "吞吐 metric_2 x。",
        "吞吐 metric_2倍。",
        "吞吐 metric_2成。",
        "吞吐 metric_2times。",
        "排名 rank.Top10。",
        "API999ms 耗时。",
    ],
)
def test_fallback_removes_chinese_written_measurements(problem: str) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = problem

    result = build_deterministic_finding_synthesis(_projection(document))

    rendered = result.document["conclusions"][0]["problem"]
    assert problem not in rendered


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


def test_fallback_preserves_problem_context_when_problem_contains_measurement_text() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "目标冷启动场景缺失"
    finding["problem"] = "目标进程在采集窗口内只有 8.5 ms 运行时间。"

    result = build_deterministic_finding_synthesis(_projection(document))

    problem = result.document["conclusions"][0]["problem"]
    assert "目标进程" in problem
    assert "采集窗口" in problem
    assert problem != finding["title"]


def test_fallback_removes_free_numbers_without_erasing_specific_finding_text() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "主线程同步 Binder 累计阻塞 333.35 ms"
    finding["problem"] = "主线程同步 Binder 在冷启动关键路径累计阻塞 333.35 ms，占比 20.7%。"
    finding["root_cause"] = "主线程发起同步 Binder 调用，等待 system_server 返回 97.69 ms。"
    finding["impact"] = "冷启动首帧等待增加 333.35 ms。"
    finding["engine_recommendation"] = (
        "将主线程 Binder 查询移出冷启动关键路径，并在下一轮复测中核对指标。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    recommendation = result.document["recommendations"][0]
    top_finding = result.document["top_findings"][0]
    assert "主线程同步 Binder" in conclusion["problem"]
    assert "冷启动关键路径" in conclusion["problem"]
    assert "主线程发起同步 Binder 调用" in conclusion["cause"]
    assert "将主线程 Binder 查询移出冷启动关键路径" in conclusion["recommendation"]
    assert "冷启动首帧等待增加" in recommendation["expected_effect"]
    assert "冷启动首帧等待增加" in top_finding["user_impact"]
    assert conclusion["problem"] != "检测到与该 Finding 对应的性能问题。"


def test_fallback_rewrites_number_words_without_losing_issue_terms() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "第三方 SDK 初始化出现四十二条长耗时"
    finding["problem"] = "第三方 SDK 初始化集中发生在第一帧之前。"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert "外部 SDK 初始化" in conclusion["problem"]
    assert "首帧之前" in conclusion["problem"]


def test_fallback_preserves_shorthand_external_sdk_wording() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "三方 SDK 初始化集中"
    finding["problem"] = "三方 SDK 初始化集中发生在首帧之前。"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert "外部 SDK 初始化" in conclusion["problem"]
    assert "方 SDK" not in conclusion["problem"]


def test_fallback_removes_internal_references_without_leaving_path_like_text() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "JIT 编译活跃"
    finding["problem"] = (
        "JIT 编译活跃 — SR01（27 次 / 373.657 ms，疑似缺 Baseline Profile），"
        "详见 art-32 与 execute_sql:19。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    problem = result.document["conclusions"][0]["problem"]
    assert "JIT 编译活跃" in problem
    assert "疑似缺 Baseline Profile" in problem
    assert "SR" not in problem
    assert "art-" not in problem
    assert "execute_sql" not in problem
    assert "/" not in problem


@pytest.mark.parametrize(
    "problem",
    [
        "主线程 49 次/333.35 ms，占比 20.7%。",
        "锁竞争 232次/71.0ms。",
        "CPU 运行 100ms/等待 20ms。",
    ],
)
def test_fallback_removes_numeric_slash_separators_without_spaces(problem: str) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = problem

    result = build_deterministic_finding_synthesis(_projection(document))

    sanitized = result.document["conclusions"][0]["problem"]
    assert "/" not in sanitized
    assert "／" not in sanitized


def test_fallback_removes_scientific_measurement_without_damaging_identifiers() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = (
        "Media3 主线程耗时 1e3 ms，H264 解码仍在关键路径。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    problem = result.document["conclusions"][0]["problem"]
    assert "1e3" not in problem
    assert "Media3" in problem
    assert "H264" in problem


def test_fallback_preserves_common_words_instead_of_deleting_number_characters() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["problem"] = "一般情况下应逐一复核一部分指标，一旦异常就检查每一项。"

    result = build_deterministic_finding_synthesis(_projection(document))

    problem = result.document["conclusions"][0]["problem"]
    assert problem == "一般情况下应逐一复核一部分指标，一旦异常就检查每一项。"


def test_fallback_preserves_alphanumeric_entities_and_lexical_number_words() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = (
        "Media3 与 H264 解码应统一采样口径，另一方面需要逐一核对。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    problem = result.document["conclusions"][0]["problem"]
    assert "Media3" in problem
    assert "H264" in problem
    assert "统一采样口径" in problem
    assert "另一方面" in problem
    assert "逐一核对" in problem


@pytest.mark.parametrize(
    "problem",
    [
        "建议从三方面优化启动流程。",
        "避免一次性加载全部资源。",
        "建议降低冷启动耗时。",
        "主线程同步调用增加耗时。",
        "首帧耗时。",
        "另一个问题需要处理。",
        "另一项建议需要复核。",
        "统一 Binder 调用并复测。",
        "逐一 Binder 调用核对。",
        "逐一 SDK 回调核对。",
        "统一 FPS 口径。",
        "上一帧与当前帧对比，下一帧继续复测。",
        "前一帧、后一帧、这一帧、某一帧和任一帧都要核对。",
        "新一轮、上一轮和这一轮使用相同环境。",
        "某一项和任一项都不能省略。",
        "下一次复测与上一次采集使用相同口径。",
        "Android14 上的 Media3、H264、HTTP2 与 TLS1.3 保持不变。",
        "两者与二者都要核对，一系列证据一经确认就应保持一致。",
        "该现象一度出现，但不应一时、一再、一贯或一向过度归因。",
        "建议从两方面复核。",
        "Android 14、HTTP/2、TLS 1.3、Wi-Fi 6 的配置需要核对。",
        "API 34、JDK 17、Kotlin 2.0、AGP 9 与 NDK r27 保持不变。",
        "二进制解析采用零拷贝，十六进制标识保持一致，千万不要伪造结论。",
    ],
)
def test_fallback_preserves_lexical_numbers_and_non_numeric_value_labels(
    problem: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = problem

    result = build_deterministic_finding_synthesis(_projection(document))

    assert result.document["conclusions"][0]["problem"] == problem


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("发现四十二条长耗时。", "发现多处长耗时问题。"),
        (
            "外部 SDK 出现四十二条长耗时。",
            "外部 SDK 出现多处长耗时问题。",
        ),
        ("首帧有一百帧。", "首帧帧数存在异常。"),
    ],
)
def test_fallback_rewrites_removed_written_counts_as_complete_sentences(
    problem: str,
    expected: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = problem

    result = build_deterministic_finding_synthesis(_projection(document))

    assert result.document["conclusions"][0]["problem"] == expected


def test_fallback_rewrites_numbered_frame_without_leaving_dangling_ordinal() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = (
        "首帧与第二帧都存在问题。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))
    problem = result.document["conclusions"][0]["problem"]

    assert problem == "首帧与后续帧都存在问题。"
    assert "第都" not in problem


def test_fallback_cleans_markdown_measurements_and_empty_evidence_clauses() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["problem"] = (
        "`QQMusicSdkAdapter.doInit` self **188.75 ms**（wall 287 ms）。\n"
        "- 根因链：外部 SDK 在主线程串行初始化，延长首帧前置工作。\n"
        "- 证据：art-32、art-37、art-41。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    problem = result.document["conclusions"][0]["problem"]
    assert "QQMusicSdkAdapter.doInit" in problem
    assert "外部 SDK" in problem
    assert "证据" not in problem
    assert "*" not in problem
    assert "`" not in problem
    assert "、、" not in problem
    assert "（wall ）" not in problem


def test_fallback_provides_specific_cautious_actions_for_trace_categories() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "JIT 编译活跃"
    finding["problem"] = "JIT 编译活跃，疑似缺 Baseline Profile。"
    finding["root_cause"] = "[redacted]"
    finding["engine_recommendation"] = "排查并优化：JIT 编译活跃。"
    finding["confidence_ceiling"] = "low"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert "仅显示" in conclusion["cause"]
    assert "不足以" in conclusion["cause"]
    assert "发布构建" in conclusion["recommendation"]
    assert "Baseline Profile" in conclusion["recommendation"]


@pytest.mark.parametrize(
    ("title", "aggressive", "expected"),
    [
        (
            "JIT 编译活跃，疑似缺 Baseline Profile",
            "直接补齐 Baseline Profile 配置",
            "发布构建",
        ),
        (
            "Native 加载聚合口径仍需核对",
            "立即延后所有 Native 库加载",
            "先确认首屏是否依赖",
        ),
        (
            "其他进程 fork 与调度延迟只有相关性",
            "关闭测试期间全部后台服务",
            "固定后台负载",
        ),
    ],
)
def test_fallback_does_not_publish_aggressive_engine_action_for_low_confidence_finding(
    title: str,
    aggressive: str,
    expected: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = title
    finding["problem"] = title
    finding["root_cause"] = title
    finding["engine_recommendation"] = aggressive
    finding["confidence_ceiling"] = "low"

    result = build_deterministic_finding_synthesis(_projection(document))
    recommendation = result.document["conclusions"][0]["recommendation"]

    assert aggressive not in recommendation
    assert expected in recommendation


@pytest.mark.parametrize(
    "aggressive",
    [
        "直接修改 system_server 内部 monitor 锁",
        "修改 SurfaceFlinger 调度策略",
    ],
)
def test_fallback_replaces_system_internal_engine_action_with_app_safe_action(
    aggressive: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "system_server 内部 monitor 锁竞争"
    finding["problem"] = finding["title"]
    finding["root_cause"] = "系统层根因，应用不可直接修改。"
    finding["engine_recommendation"] = aggressive

    result = build_deterministic_finding_synthesis(_projection(document))
    recommendation = result.document["conclusions"][0]["recommendation"]

    assert aggressive not in recommendation
    assert "应用侧优先减少" in recommendation


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "主线程同步 Binder IPC 是最大可操作热点 — self 333.35 ms（20.7%）",
            "主线程同步 Binder IPC 是最大可操作热点",
        ),
        (
            "三方 SDK 在主线程同步初始化（bindApplication 非框架占 68.4% / 439.9 ms）",
            "外部 SDK 在主线程同步初始化",
        ),
        (
            "Native 库加载（42 个 .so 累计 346.9 ms）",
            "Native 库加载",
        ),
        (
            "attachApplication 97.69 ms 中约 62 ms 是 system_server 内部 monitor 锁竞争（系统层根因）",
            "attachApplication 中存在 system_server 内部 monitor 锁竞争（系统层根因）",
        ),
    ],
)
def test_fallback_removes_dangling_measurement_labels_from_real_titles(
    original: str,
    expected: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = original

    result = build_deterministic_finding_synthesis(_projection(document))

    assert result.document["conclusions"][0]["problem"] == expected


@pytest.mark.parametrize(
    (
        "title",
        "root_cause",
        "confidence_ceiling",
        "expected_problem",
        "expected_cause",
        "expected_recommendation",
    ),
    [
        (
            "主线程同步 Binder IPC 阻塞 — 49 次、self 333ms（20.7%）",
            "启动期间主线程发起同步 Binder，并等待 system_server 返回。",
            "high",
            "冷启动主线程同步 Binder 调用阻塞关键路径。",
            "等待 system_server 返回",
            "同步 Binder",
        ),
        (
            "首帧渲染过重 — doFrame 349ms，CPU 占 76.9%",
            "首帧 doFrame 包含 Compose:recompose 与 AndroidOwner:onMeasure。",
            "high",
            "首帧渲染阶段主线程工作量过重。",
            "Compose 重组与 View 测量",
            "首帧 Compose",
        ),
        (
            "XeagleBtAdapter.start 同步启动 — 131ms（Sleeping 67.1%）",
            "XeagleBtAdapter.start 占用启动主线程。",
            "high",
            "XeagleBtAdapter.start 在冷启动主线程同步执行。",
            "XeagleBtAdapter.start 占用冷启动主线程",
            "外部 SDK",
        ),
        (
            "Native 库加载 — SR13：42 个 .so、346.9ms（最大单个 96.6ms）",
            "其余为 .so 构造器、链接与后台线程加载口径。",
            "low",
            "启动阶段存在 Native 库加载活动，实际关键路径影响仍需核验。",
            "不能把聚合值直接当作主线程阻塞时长",
            "先确认首屏是否依赖",
        ),
        (
            "主线程锁竞争 — SR04：232 次、71.0ms",
            "启动期间主线程锁竞争；其中包含 ClassLinker classes lock。",
            "high",
            "冷启动主线程存在锁竞争。",
            "ClassLinker 与应用锁竞争",
            "对应实现",
        ),
        (
            "缺 Baseline Profile：JIT 27 次、373.7ms（warning）",
            "冷启动伴随 JIT 编译活跃，疑似缺少 Baseline Profile。",
            "low",
            "冷启动期间存在 JIT 编译活动，是否缺少 Baseline Profile 尚未证实。",
            "不足以证明 Baseline Profile 缺失",
            "发布构建",
        ),
        (
            "并发应用启动干扰：±5s 内 5 个其他进程 fork",
            (
                "其他进程 fork 与调度等待同窗出现。首帧前耗时分解包含 "
                "doFrame 与 Compose，但不能证明 fork 导致首帧变慢。"
            ),
            "low",
            "测试窗口内存在并发进程活动，可能与调度等待相关。",
            "只有时间相关性",
            "固定后台负载",
        ),
    ],
)
def test_fallback_publishes_readable_cautious_conclusions_for_real_trace_findings(
    title: str,
    root_cause: str,
    confidence_ceiling: str,
    expected_problem: str,
    expected_cause: str,
    expected_recommendation: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = title
    finding["problem"] = title
    finding["root_cause"] = root_cause
    finding["engine_recommendation"] = f"排查并优化：{title}。"
    finding["confidence_ceiling"] = confidence_ceiling

    result = build_deterministic_finding_synthesis(_projection(document))
    conclusion = result.document["conclusions"][0]

    assert conclusion["problem"] == expected_problem
    assert expected_cause in conclusion["cause"]
    assert expected_recommendation in conclusion["recommendation"]
    assert all(
        fragment not in conclusion[field]
        for field in ("problem", "cause", "recommendation")
        for fragment in ("SR13", "SR04", "—、", "：、", "± 内", "（Sleeping）")
    )


def test_fallback_discards_engine_action_with_orphaned_metric_labels() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "三方 SDK 在主线程同步初始化 — QQMusicSdkAdapter.doInit"
    finding["problem"] = finding["title"]
    finding["root_cause"] = "[redacted]"
    finding["engine_recommendation"] = (
        "将 QQMusic SDK 延迟到首帧后，预期可回收 self 188ms、wall 287ms。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))
    conclusion = result.document["conclusions"][0]

    assert "QQMusicSdkAdapter.doInit 占用冷启动主线程" in conclusion["cause"]
    assert "外部 SDK 初始化延后" in conclusion["recommendation"]
    assert "self" not in conclusion["recommendation"].casefold()
    assert "wall" not in conclusion["recommendation"].casefold()


def test_fallback_keeps_system_cause_and_removes_internal_metric_labels() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["root_cause"] = (
        "binder 线程与 system_server 内部任务抢同一 monitor。\n"
        "- 证据：art_lock_contention。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    cause = result.document["conclusions"][0]["cause"]
    assert cause == "binder 线程与 system_server 内部任务抢同一 monitor。"
    assert "SmartPerfetto 证据支持" not in cause
    assert "art_lock_contention" not in cause


def test_fallback_removes_value_label_after_internal_parenthesis_is_removed() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["root_cause"] = (
        "启动期间主线程锁竞争 232 次，累计 71.0 ms（art_lock_contention 归因）。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    assert result.document["conclusions"][0]["cause"] == "启动期间主线程锁竞争。"


def test_fallback_does_not_merge_sdk_lifecycle_ownership() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "三方 SDK 在主线程同步初始化"
    finding["problem"] = finding["title"]
    finding["root_cause"] = (
        "QQMusicSdkAdapter.doInit 与 XeagleBtAdapter.start 在 bindApplication 串行执行。"
    )

    result = build_deterministic_finding_synthesis(_projection(document))

    cause = result.document["conclusions"][0]["cause"]
    assert "QQMusicSdkAdapter.doInit" in cause
    assert "XeagleBtAdapter.start" in cause
    assert "生命周期入口必须分别定位" in cause
    assert "在 bindApplication 串行执行" not in cause


@pytest.mark.parametrize(
    ("scenario_type", "title", "forbidden", "expected"),
    [
        ("scroll", "Binder 回调阻塞滑动帧", "冷启动", "滑动"),
        ("scroll", "SDK 回调阻塞滑动帧", "初始化延后", "滑动"),
        ("memory_cycle", "Native heap 持续增长", "库加载", "内存"),
        ("startup", "已排除 JIT 编译影响", "Baseline Profile", "关联 Trace 证据"),
    ],
)
def test_fallback_category_actions_respect_scenario_and_negation(
    scenario_type: str,
    title: str,
    forbidden: str,
    expected: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    _set_scenario(document, scenario_type)
    finding = document["workbench"]["findings"][0]
    finding["title"] = title
    finding["problem"] = title
    finding["root_cause"] = title
    finding["engine_recommendation"] = f"排查并优化：{title}。"

    result = build_deterministic_finding_synthesis(_projection(document))

    recommendation = result.document["conclusions"][0]["recommendation"]
    assert forbidden not in recommendation
    assert expected in recommendation
    if title.startswith("已排除 JIT"):
        assert "存在 JIT 编译活动" not in result.document["conclusions"][0]["cause"]


@pytest.mark.parametrize(
    ("title", "forbidden"),
    [
        ("已排除 SDK 初始化影响", "SDK 初始化延后"),
        ("SDK 初始化不是根因", "SDK 初始化延后"),
        ("已排除同步 Binder 阻塞", "同步 Binder 调用"),
        ("Binder 同步阻塞并非问题", "同步 Binder 调用"),
        ("已排除 Native 库加载影响", "Native 库加载延后"),
        ("没有 JIT 活动", "Baseline Profile"),
        ("不存在 JIT 影响", "Baseline Profile"),
        ("未观察到 JIT 活动", "Baseline Profile"),
        ("SDK 初始化未发现异常", "SDK 初始化延后"),
        ("同步 Binder 调用未观察到阻塞影响", "同步 Binder 调用"),
        ("Native 库加载未发现问题", "Native 库加载延后"),
        ("JIT 活动未观察到", "Baseline Profile"),
        ("首帧 Compose 未发现异常", "首帧 Compose"),
    ],
)
def test_fallback_does_not_recommend_categories_that_evidence_excludes(
    title: str,
    forbidden: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = title
    finding["problem"] = title
    finding["root_cause"] = title
    finding["engine_recommendation"] = f"排查并优化：{title}。"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert "已排除" in conclusion["cause"]
    assert forbidden not in conclusion["recommendation"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("已排除 JIT，Binder 同步等待仍是主要问题", "Binder"),
        ("已排除 Native，SDK 同步初始化仍是主要问题", "SDK"),
        (
            "已排除 system_server 内部锁竞争，应用重复 Binder 调用是根因",
            "Binder",
        ),
    ],
)
def test_fallback_preserves_confirmed_category_after_another_is_excluded(
    title: str,
    expected: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = title
    finding["problem"] = title
    finding["root_cause"] = title
    finding["engine_recommendation"] = f"排查并优化：{title}。"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert "已排除该候选机制" not in conclusion["cause"]
    assert expected in conclusion["recommendation"]


def test_fallback_does_not_claim_category_present_when_evidence_is_absent() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "没有证据表明 JIT 存在"
    finding["problem"] = finding["title"]
    finding["root_cause"] = finding["title"]
    finding["engine_recommendation"] = "排查并优化：JIT 编译。"

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert "证据不足" in conclusion["cause"]
    assert "存在 JIT 编译活动" not in conclusion["cause"]
    assert "Baseline Profile" not in conclusion["recommendation"]


@pytest.mark.parametrize(
    ("title", "forbidden"),
    [
        ("无法排除 SDK 初始化影响", "SDK 初始化延后"),
        ("不能排除 Native 库加载影响", "Native 库加载延后"),
        ("尚不能排除 JIT 活动影响", "Baseline Profile"),
        ("没有证据排除同步 Binder 阻塞", "同步 Binder 调用"),
        ("没有 JIT 活动不足以排除 Baseline Profile 问题", "Baseline Profile"),
        ("未观察到 Native 加载不能排除其他 Native 阻塞", "Native 库加载延后"),
        ("未发现 Binder 等待不代表可以排除 Binder 问题", "同步 Binder 调用"),
    ],
)
def test_fallback_treats_unable_to_exclude_as_uncertain(
    title: str,
    forbidden: str,
) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = title
    finding["problem"] = title
    finding["root_cause"] = title
    finding["engine_recommendation"] = f"直接执行：{title}。"

    result = build_deterministic_finding_synthesis(_projection(document))
    conclusion = result.document["conclusions"][0]

    assert "证据不足" in conclusion["cause"]
    assert forbidden not in conclusion["recommendation"]


@pytest.mark.parametrize(
    "problem",
    [
        "内存增长 100 MiB。",
        "内存增长 100 GiB。",
        "内存增长 100 兆字节。",
        "频率升至 300 兆赫。",
        "吞吐为 50 兆字节每秒。",
        "帧率为 60 赫兹。",
        "耗时 10µs。",
        "耗时 10μs。",
        "耗时 10 msec。",
        "耗时 10 sec。",
        "耗时一点五毫秒。",
        "耗时零点五秒。",
        "内存一点五 MB。",
    ],
)
def test_fallback_removes_measurement_and_unit_as_one_expression(problem: str) -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["workbench"]["findings"][0]["problem"] = problem

    result = build_deterministic_finding_synthesis(_projection(document))

    rendered = result.document["conclusions"][0]["problem"]
    assert not any(
        unit in rendered
        for unit in ("MiB", "GiB", "兆字节", "兆赫", "赫兹", "µs", "μs", "msec", "sec")
    )


@pytest.mark.parametrize(
    "title",
    [
        "未发现能够排除 JIT 的证据",
        "JIT 不是唯一因素",
        "JIT 并非无关，仍需核验",
    ],
)
def test_fallback_does_not_reverse_non_negated_jit_statements(title: str) -> None:
    from perfpilot_api.ai.finding_fallback import _hypothesis_cause

    cause = _hypothesis_cause(
        {
            "title": title,
            "problem": title,
            "root_cause": title,
            "scenario_type": "startup",
            "status": "hypothesis",
            "confidence_ceiling": "low",
        }
    )

    assert cause is not None
    assert "已排除" not in cause
    assert "仅显示" in cause


def test_fallback_retest_steps_name_the_specific_problem() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    finding = document["workbench"]["findings"][0]
    finding["title"] = "主线程同步 Binder 阻塞"

    result = build_deterministic_finding_synthesis(_projection(document))

    assert "主线程同步 Binder 阻塞" in result.document["retest_plan"][0]["steps"]


def test_fallback_does_not_invent_metric_retest_for_metricless_finding() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    workbench = document["workbench"]
    finding = workbench["findings"][0]
    finding["metric_ids"] = []

    result = build_deterministic_finding_synthesis(_projection(document))

    conclusion = result.document["conclusions"][0]
    assert result.document["key_metric_ids"] == []
    assert not [claim for claim in conclusion["claim_refs"] if claim["metric_id"]]
    assert result.document["retest_plan"] == []
