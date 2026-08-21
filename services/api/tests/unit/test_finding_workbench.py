from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from perfpilot_api.reports.finding_workbench import (
    build_capabilities,
    build_finding_workbench,
    build_report_quality,
)


ROOT = Path(__file__).parents[4]
ENVIRONMENT_FINGERPRINT = (
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)


def _core() -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts/v1/examples/normalized-trace-report.valid.json").read_text(
            encoding="utf-8"
        )
    )


def _strong_source_context() -> dict[str, object]:
    return {
        "match_summary": "strong",
        "fragments": [
            {
                "source_ref_id": "97000000-0000-4000-8000-000000000001",
                "relative_path": "app/src/main/java/demo/MainActivity.kt",
                "symbol": "demo.MainActivity.onCreate",
                "match_grade": "strong",
                "finding_ids": ["85000000-0000-4000-8000-000000000001"],
                "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
            }
        ],
    }


def test_builder_is_stable_and_merges_same_root_cause() -> None:
    core = _core()
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    duplicate = deepcopy(scenario["findings"][0])
    duplicate["finding_id"] = "85000000-0000-4000-8000-000000000002"
    duplicate["evidence_ids"] = ["86000000-0000-4000-8000-000000000002"]
    scenario["findings"].append(duplicate)
    second_evidence = deepcopy(scenario["evidence"][0])
    second_evidence["evidence_id"] = "86000000-0000-4000-8000-000000000002"
    second_evidence["interval_start_ns"] = 320_000_000
    second_evidence["interval_end_ns"] = 410_000_000
    scenario["evidence"].append(second_evidence)

    first = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )
    second = build_finding_workbench(
        core_document=deepcopy(core),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert first == second
    assert len(first["findings"]) == 1
    finding = first["findings"][0]
    assert finding["evidence_ids"] == sorted(finding["evidence_ids"])
    assert finding["confidence"]["evidence_grade"] == "E3"
    assert finding["critical_path_contribution"] == 0.35
    assert first["primary_finding_ids"] == [finding["finding_id"]]


def test_unlinked_finding_does_not_inherit_global_startup_metrics() -> None:
    workbench = build_finding_workbench(
        core_document=_core(),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["metric_ids"] == []
    assert workbench["retest_plans"][0]["metric_ids"] == []
    assert workbench["retest_plans"][0]["pass_criteria"] == [
        "在相同场景中确认该机制不再出现在关键路径，或补采到可直接量化的指标证据。"
    ]
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


def test_system_side_subcause_is_retained_but_not_promoted_to_primary() -> None:
    core = _core()
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    finding_template = deepcopy(scenario["findings"][0])
    evidence_template = deepcopy(scenario["evidence"][0])
    metric_template = deepcopy(scenario["metrics"][0])
    scenario["findings"] = []
    scenario["evidence"] = []
    scenario["metrics"] = []

    titles = (
        "attachApplication 系统层锁竞争",
        "三方 SDK 主线程同步初始化",
        "首帧 Compose 测量过重",
        "应用主线程同步调用 system_server 等待回包",
    )
    rules = ("startup.gamma", "startup.zeta", "startup.alpha", "startup.beta")
    for index, (title, rule_id) in enumerate(zip(titles, rules, strict=True), start=1):
        evidence = deepcopy(evidence_template)
        evidence["evidence_id"] = f"86000000-0000-4000-8000-{index:012d}"
        scenario["evidence"].append(evidence)

        finding = deepcopy(finding_template)
        finding["finding_id"] = f"85000000-0000-4000-8000-{index:012d}"
        finding["rule_id"] = rule_id
        finding["title"] = title
        finding["summary"] = (
            "system_server 内部 monitor 锁竞争，应用不可直接修复。"
            if index == 1
            else "应用侧可通过调整主线程工作减少等待。"
        )
        finding["evidence_ids"] = [evidence["evidence_id"]]
        finding["severity"] = "critical" if index == 1 else "warning"
        scenario["findings"].append(finding)

        if index > 1:
            metric = deepcopy(metric_template)
            metric["metric_id"] = f"84000000-0000-4000-8000-{index:012d}"
            metric["sample_ids"] = [evidence["evidence_id"]]
            scenario["metrics"].append(metric)

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    system_finding = next(
        item for item in workbench["findings"] if "系统层锁竞争" in item["title"]
    )
    application_binder = next(
        item for item in workbench["findings"] if "调用 system_server" in item["title"]
    )
    primary_titles = [
        item["title"]
        for item in workbench["findings"]
        if item["finding_id"] in workbench["primary_finding_ids"]
    ]
    assert system_finding["priority"] == "p2"
    assert system_finding["metric_ids"] == []
    assert system_finding["finding_id"] not in workbench["primary_finding_ids"]
    assert application_binder["priority"] in {"p0", "p1"}
    assert application_binder["finding_id"] in workbench["primary_finding_ids"]
    assert primary_titles == list(titles[1:])


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        (
            "应用重复 Binder 调用",
            "已排除 system_server 内部锁竞争，根因是应用重复调用。",
        ),
        (
            "应用主线程同步调用 system_server",
            "不是系统层问题，应用重复 IPC 才是根因。",
        ),
    ],
)
def test_system_ownership_downgrade_ignores_explicitly_negated_system_causes(
    title: str,
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = title
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] in {"p0", "p1"}
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "system_server 不是应用进程，属于系统层根因。",
        "system_server 并非应用可控，属于系统侧瓶颈。",
    ],
)
def test_system_ownership_downgrade_keeps_positive_external_ownership(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "attachApplication 锁竞争"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] == "p2"
    assert workbench["primary_finding_ids"] == []


@pytest.mark.parametrize(
    "summary",
    [
        "应用主线程重复调用 system_server 内部服务，等待回包。",
        "应用循环 IPC 触发 system_server 内部工作，根因在应用侧。",
    ],
)
def test_system_ownership_downgrade_does_not_hide_application_root_causes(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "应用重复 Binder 调用"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] in {"p0", "p1"}
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "system_server 内部 monitor 锁竞争导致应用等待。",
        "system_server 内部 GC 暂停导致 Binder 返回延迟。",
    ],
)
def test_system_ownership_downgrade_recognizes_explicit_system_mechanisms(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "attachApplication 等待"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] == "p2"
    assert workbench["primary_finding_ids"] == []


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        (
            "系统层 monitor 锁竞争",
            "系统侧调度阻塞，应用不可直接修复。",
        ),
        (
            "框架侧 GC 暂停",
            "系统层根因，应用不可直接控制。",
        ),
    ],
)
def test_system_ownership_downgrade_accepts_explicit_chinese_system_ownership(
    title: str,
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = title
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] == "p2"
    assert workbench["primary_finding_ids"] == []


@pytest.mark.parametrize(
    "summary",
    [
        "已排除应用重复调用，system_server 内部 monitor 锁竞争是根因。",
        "不是应用侧根因，system_server 内部锁竞争导致等待。",
        "应用重复调用已排除；system_server 系统侧锁竞争是根因。",
        "未发现应用重复调用，system_server 内部 monitor 锁竞争是根因。",
        "未观察到应用循环调用，system_server 内部 GC 暂停导致等待。",
        "没有证据表明应用侧根因，system_server 系统侧锁竞争是根因。",
        "无法确认根因在应用侧，system_server 内部锁竞争是根因。",
        "应用侧根因尚未确认，system_server 内部锁竞争是根因。",
    ],
)
def test_system_ownership_downgrade_honors_negated_application_causes(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "attachApplication 等待"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] == "p2"
    assert workbench["primary_finding_ids"] == []


@pytest.mark.parametrize(
    "summary",
    [
        "未发现 system_server 内部 monitor 锁竞争。",
        "未观察到 system_server 内部 monitor 锁竞争。",
        "没有证据表明 system_server 内部 monitor 锁竞争。",
        "无法确认 system_server 内部 monitor 锁竞争。",
        "system_server 内部未发现 monitor 锁竞争。",
        "system_server 内部锁竞争未观察到。",
        "system_server 内部锁竞争尚未确认。",
        "system_server 内部没有证据支持锁竞争。",
        "系统侧锁竞争无法确认。",
    ],
)
def test_system_ownership_downgrade_ignores_unconfirmed_system_mechanisms(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "Binder 等待"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] in {"p0", "p1"}
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "不是应用重复调用，system_server 内部锁竞争是根因。",
        "并非应用循环调用，system_server 内部 GC 暂停导致等待。",
        "应用重复调用并不是根因，system_server 系统侧锁竞争是根因。",
    ],
)
def test_system_ownership_downgrade_understands_negated_application_phrasings(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "Binder 等待"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] == "p2"
    assert workbench["primary_finding_ids"] == []


@pytest.mark.parametrize(
    "summary",
    [
        "应用同步调用系统侧接口导致主线程阻塞。",
        "应用主动触发框架侧耗时路径，根因是调用时机不当。",
        "应用发起 system_server 请求的时机不当。",
    ],
)
def test_system_ownership_downgrade_keeps_actionable_application_callers(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "应用主线程等待"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] in {"p0", "p1"}
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


def test_system_ownership_keeps_main_thread_binder_target_actionable() -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "主线程同步 Binder 到 system_server 累计过高"
    finding["summary"] = (
        "启动期间主线程同步 Binder 到 system_server；部分请求在 system_server 侧 "
        "Sleeping。报告同时记录主线程锁竞争，根因包含应用和 vendor 服务同步调用。"
    )
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] in {"p0", "p1"}
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "system_server 返回完成，应用内部 monitor 锁竞争导致主线程等待。",
        "system_server 调用正常；应用代码内部锁竞争导致等待。",
        "system_server 返回正常；工作线程内部 monitor 锁竞争导致等待。",
        "system_server 无异常；工作线程内部锁竞争导致等待。",
        "system_server 已正常返回；锁竞争发生在应用进程。",
        "SurfaceFlinger 无异常；渲染线程内部锁竞争导致等待。",
    ],
)
def test_system_ownership_downgrade_does_not_pair_process_and_app_mechanism_across_clauses(
    summary: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = "应用主线程等待"
    finding["summary"] = summary
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] in {"p0", "p1"}
    assert workbench["primary_finding_ids"] == [
        workbench["findings"][0]["finding_id"]
    ]


@pytest.mark.parametrize("process", ["system_server", "SurfaceFlinger"])
def test_system_ownership_downgrade_carries_explicit_system_subject_into_root_cause(
    process: str,
) -> None:
    core = _core()
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    finding["title"] = f"{process} 阻塞"
    finding["summary"] = "内部 monitor 锁竞争导致应用等待。"
    finding["severity"] = "critical"

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["priority"] == "p2"
    assert workbench["primary_finding_ids"] == []


def test_critical_path_contribution_clips_and_deduplicates_intervals() -> None:
    core = _core()
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    finding = scenario["findings"][0]
    first = scenario["evidence"][0]
    first["interval_start_ns"] = 50_000_000
    first["interval_end_ns"] = 300_000_000
    second = deepcopy(first)
    second["evidence_id"] = "86000000-0000-4000-8000-000000000002"
    second["interval_start_ns"] = 250_000_000
    second["interval_end_ns"] = 500_000_000
    scenario["evidence"].append(second)
    finding["evidence_ids"].append(second["evidence_id"])

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["critical_path_contribution"] == 0.5


def test_critical_path_contribution_is_zero_without_a_valid_window() -> None:
    core = _core()
    window = core["scenario_reports"][0]["trace_health"]["measurement_window"]  # type: ignore[index]
    window["end_ns"] = window["start_ns"]

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["critical_path_contribution"] == 0.0


def test_direct_trace_evidence_not_source_match_determines_evidence_grade() -> None:
    core = _core()
    evidence = core["scenario_reports"][0]["evidence"][0]  # type: ignore[index]
    evidence["fields"].update(  # type: ignore[union-attr]
        {
            "process": "com.rivotek.mediacenter",
            "thread": "main",
            "track": "Main Thread",
            "slice": "Application.onCreate",
        }
    )

    without_source = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )
    with_source = build_finding_workbench(
        core_document=core,
        source_context=_strong_source_context(),
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert without_source["findings"][0]["confidence"]["evidence_grade"] == "E4"
    assert with_source["findings"][0]["confidence"]["evidence_grade"] == "E4"
    assert without_source["findings"][0]["source_ref_ids"] == []
    assert with_source["findings"][0]["source_ref_ids"] == [
        "97000000-0000-4000-8000-000000000001"
    ]


def test_quality_uses_required_capabilities_not_optional_diagnostics() -> None:
    core = _core()
    capabilities = core["scenario_reports"][0]["trace_capabilities"]  # type: ignore[index]
    capabilities.append(
        {
            "name": "frame_timeline",
            "required": False,
            "status": "unavailable",
            "reason": "trace_data_source_missing",
        }
    )

    complete = build_report_quality(
        core_document=core,
        source_context=None,
        synthesis_state="not_requested",
        patch_validation_state="not_requested",
    )
    capabilities[0]["status"] = "unavailable"
    capabilities[0]["reason"] = "trace_data_source_missing"
    partial = build_report_quality(
        core_document=core,
        source_context=None,
        synthesis_state="not_requested",
        patch_validation_state="not_requested",
    )

    assert complete["trace_core_state"] == "complete"
    assert complete["reason_codes"] == ["optional_capability_frame_timeline_unavailable"]
    assert partial["trace_core_state"] == "partial"
    assert partial["reason_codes"] == [
        "required_capability_android_startups_unavailable",
        "optional_capability_frame_timeline_unavailable",
    ]


def test_quality_projects_trace_health_and_capability_matrix_for_ai() -> None:
    core = _core()
    quality = build_report_quality(
        core_document=core,
        source_context=None,
        synthesis_state="not_requested",
        patch_validation_state="not_requested",
    )

    assert quality["scenarios"] == [
        {
            "scenario_type": "startup",
            "parse_status": "parsed",
            "measurement_window_coverage": "complete",
            "data_loss_present": False,
            "data_loss_categories": [],
            "capabilities": [
                {
                    "name": "android_startups",
                    "required": True,
                    "status": "available",
                    "reason_code": None,
                }
            ],
        }
    ]


def test_finding_keeps_ceiling_exclusions_and_engine_actions() -> None:
    workbench = build_finding_workbench(
        core_document=_core(),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    finding = workbench["findings"][0]
    assert finding["critical_path_contribution"] == 0.24
    assert finding["confidence_ceiling"] == "high"
    assert finding["exclusions"] == []
    assert finding["engine_recommendation"] == "Move the lookup off the launch-critical path."
    assert finding["engine_retest"] == "Repeat cold launches."


def test_capabilities_keep_quality_dimensions_separate() -> None:
    capabilities = build_capabilities(
        core_document=_core(),
        source_context=_strong_source_context(),
    )

    assert capabilities == {
        "trace": "available",
        "smartperfetto": "available",
        "source": "matched",
        "ai": "pending",
    }


def test_weak_source_never_publishes_paths_or_symbols() -> None:
    weak = _strong_source_context()
    weak["match_summary"] = "weak"
    weak["fragments"][0]["match_grade"] = "weak"  # type: ignore[index]
    weak["fragments"][0]["relative_path"] = "private/Startup.kt"  # type: ignore[index]
    weak["fragments"][0]["symbol"] = "demo.Private.start"  # type: ignore[index]

    workbench = build_finding_workbench(
        core_document=_core(),
        source_context=weak,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["source_ref_ids"] == []
    assert "private/Startup.kt" not in json.dumps(workbench)
    assert "demo.Private.start" not in json.dumps(workbench)


def test_evidence_without_interval_is_kept_without_timeline_locator() -> None:
    core = _core()
    evidence = core["scenario_reports"][0]["evidence"][0]  # type: ignore[index]
    evidence["interval_start_ns"] = None
    evidence["interval_end_ns"] = None

    workbench = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["evidence"][0]["evidence_id"] == evidence["evidence_id"]
    assert workbench["evidence"][0]["locator"] is None
    assert workbench["critical_path"] == []
