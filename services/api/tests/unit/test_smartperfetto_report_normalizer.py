from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.ai.synthesis import validate_synthesis_output
from perfpilot_api.reports.normalizer import (
    NormalizedTraceReport,
    SmartPerfettoNormalizationError,
    normalize_smartperfetto_result,
)
from perfpilot_api.reports.smartperfetto_live_normalizer import (
    normalize_live_smartperfetto_result,
)
from perfpilot_api.reports.projection import build_ai_projection
from perfpilot_api.services.canonical_result_reader import LoadedCanonicalResult
from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.engines.smartperfetto_contracts import SmartPerfettoReportResponse


FIXTURE = Path(__file__).parents[1] / "fixtures/canonical_results/smartperfetto-result-contract-1.0.0.json"


def _source(document: dict[str, object] | None = None) -> LoadedCanonicalResult:
    copied = deepcopy(document or json.loads(FIXTURE.read_text()))
    try:
        payload = canonical_json_bytes(copied)
    except Exception:
        payload = json.dumps(copied, allow_nan=True).encode()
    return LoadedCanonicalResult(
        team_id=UUID("81000000-0000-4000-8000-000000000001"),
        analysis_id=UUID(str(copied["analysis_id"])),
        execution_id=UUID(str(copied["execution_id"])),
        artifact_id=UUID(str(copied["artifact_id"])),
        tenant_resource_version=7,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
        document=copied,
        canonical_bytes=payload,
    )


def _report(document: dict[str, object]) -> dict[str, object]:
    return document["result"]["payload"]["report"]  # type: ignore[index,return-value]


def _live_source(scenario_type: str, *, artifact_id: str) -> LoadedCanonicalResult:
    document = json.loads(FIXTURE.read_text())
    document["artifact_id"] = artifact_id
    document["result"]["payload"]["report"] = {  # type: ignore[index]
        "resultContract": {
            "version": "1.0.0",
            "dataEnvelopes": [
                {
                    "meta": {
                        "source": f"{scenario_type}.frame_timeline",
                        "stepId": f"{scenario_type}.frames",
                    },
                    "data": {
                        "columns": ["frame_count"],
                        "rows": [[7]],
                    },
                    "display": {
                        "title": "Frame count",
                        "columns": [
                            {
                                "name": "frame_count",
                                "label": "Frame count",
                                "unit": "count",
                            }
                        ],
                    },
                },
                {
                    "meta": {
                        "source": "shared.cpu_metrics",
                        "stepId": "shared.cpu",
                    },
                    "data": {
                        "columns": ["duration_ms"],
                        "rows": [[12]],
                    },
                    "display": {
                        "title": "CPU duration",
                        "columns": [
                            {
                                "name": "duration_ms",
                                "label": "CPU duration",
                                "unit": "ms",
                            }
                        ],
                    },
                },
            ],
            "diagnostics": [
                {
                    "id": "shared-diagnostic",
                    "title": "Shared diagnostic",
                    "description": "Shared diagnostic description",
                    "severity": "warning",
                    "confidence": 0.8,
                    "evidence": [],
                }
            ],
            "actions": [],
        }
    }
    return _source(document)


def _mutated_live_source(
    mutate: Callable[[dict[str, object]], None],
    *,
    scenario_type: str = "startup",
) -> LoadedCanonicalResult:
    source = _live_source(
        scenario_type,
        artifact_id="85000000-0000-4000-8000-000000000001",
    )
    document = deepcopy(source.document)
    mutate(_report(document)["resultContract"])  # type: ignore[arg-type]
    return _source(document)


@pytest.mark.parametrize(
    "uncertain_phrase",
    [
        "疑似",
        "可能",
        "相关性",
        "聚合口径",
        "口径差异",
        "尚未证明",
        "提示",
        "候选",
        "需复核",
        "推测",
        "或许",
        "是否",
        "可能造成",
        "可能引发",
        "不作为独立根因",
    ],
)
def test_live_normalizer_keeps_uncertain_diagnostics_as_suspected_symptoms(
    uncertain_phrase: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic["description"] = f"该现象{uncertain_phrase}与启动耗时有关。"

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["status"] == "suspected"
    assert finding["kind"] == "symptom"


@pytest.mark.parametrize(
    ("severity", "title", "description"),
    [
        ("info", "诊断说明", "当前仅用于说明采集状态。"),
        ("warning", "阻塞归因能力边界", "阻塞函数无法精确归因。"),
        ("warning", "数据限制", "当前信息不足。"),
        ("warning", "采集状态", "thread-state-blocked-reason 未采集。"),
    ],
)
def test_live_normalizer_projects_informational_or_boundary_diagnostics_as_limitations(
    severity: str,
    title: str,
    description: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {"severity": severity, "title": title, "description": description}
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    matching_limitations = [
        limitation
        for limitation in core["limitations"]  # type: ignore[index]
        if limitation["summary"] == description
    ]

    assert scenario["findings"] == []
    assert len(matching_limitations) == 1
    assert matching_limitations[0]["evidence_ids"]
    assert set(matching_limitations[0]["evidence_ids"]).issubset(
        {item["evidence_id"] for item in scenario["evidence"]}
    )


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "JIT 编译活跃，疑似缺 Baseline Profile",
            "Trace 只能证明 JIT 活动，是否缺少 Baseline Profile 仍需核验。",
        ),
        (
            "并发进程 fork 干扰 + 调度延迟",
            "现象提示后台负载与 Runnable 等待相关，不能据此证明因果。",
        ),
    ],
)
def test_live_normalizer_keeps_informational_performance_diagnostics_as_findings(
    title: str,
    description: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {"severity": "info", "title": title, "description": description}
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]

    assert len(scenario["findings"]) == 1
    assert scenario["findings"][0]["status"] == "suspected"
    assert all(
        limitation["summary"] != description
        for limitation in core["limitations"]  # type: ignore[index]
    )


def test_live_normalizer_does_not_turn_finding_into_limitation_for_caveat_reference() -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": "Native 库加载耗时",
                "description": "该数值属于聚合口径，口径差异详见限制。",
                "severity": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]

    assert len(scenario["findings"]) == 1
    assert scenario["findings"][0]["status"] == "suspected"
    assert scenario["findings"][0]["kind"] == "symptom"


def test_live_normalizer_downgrades_native_background_thread_aggregate() -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": "Native 库加载 — SR13：42 个 .so、346.9ms",
                "description": (
                    "主线程可观察到部分 dlopen；其余为 .so 构造器、链接与"
                    "后台线程加载口径，不能直接等同于主线程阻塞。"
                ),
                "severity": "warning",
                "confidence": 0.9,
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["status"] == "suspected"
    assert finding["kind"] == "symptom"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("线程池并发限制导致启动阻塞", "线程池容量不足，任务在主线程等待。"),
        ("Binder 根因", "已排除该可能性，根因是 Binder 同步等待。"),
        ("Binder 根因", "证据明确，提示用户调整同步 Binder 调用。"),
        ("Binder 根因", "证据已排除相关性推测，确认 Binder 同步等待。"),
        ("Binder 根因", "聚合口径差异已校正，直接证据确认 Binder 等待。"),
        ("Binder 根因", "无需复核，证据已确认。"),
        ("Binder 根因", "该机制不是候选原因，真正根因是 Binder。"),
        ("Binder 根因", "证据已证明该机制，不再是疑似。"),
        ("Binder 根因", "此前的候选原因已通过 Trace 确认为根因。"),
        ("Binder 根因", "疑似 Binder 问题现已确认是根因。"),
        ("Binder 根因", "需复核项已经复核并确认。"),
        ("已排除采集限制", "证据完整，Binder 同步等待是已确认根因。"),
        ("数据限制已解除", "证据完整，Binder 同步等待是已确认根因。"),
        ("证据限制已经解除", "证据完整，Binder 同步等待是已确认根因。"),
        ("采集限制现已消除", "证据完整，Binder 同步等待是已确认根因。"),
        ("已证明不存在数据限制", "证据完整，Binder 同步等待是已确认根因。"),
        ("数据限制", "该限制已解除，Binder 同步等待是已确认根因。"),
        ("证据限制", "现已证明不存在证据限制，Binder 是根因。"),
        ("采集限制", "限制已经消除，Binder 是根因。"),
        ("Binder 根因", "未采集到丢帧，但 Binder 等待是已确认根因。"),
        ("Binder 根因", "错误提示弹窗阻塞主线程，Trace 已确认。"),
        ("Binder 根因", "提示音播放导致音频线程阻塞。"),
        ("Binder 根因", "系统提示框绘制占用主线程。"),
        ("Binder 根因", "不可能是 SDK 初始化导致，Binder 同步等待已确认。"),
        ("Binder 根因", "该机制不可能导致当前问题，Binder 已确认。"),
        ("Binder 根因", "结果不提示 SDK 问题，Binder 已确认。"),
        ("Binder 根因", "相关性并不存在，Binder 已确认。"),
        ("Binder 根因", "无相关性问题，Binder 已确认。"),
        ("Binder 根因", "提示：Binder 已确认是根因。"),
    ],
)
def test_live_normalizer_does_not_treat_lexical_or_negated_markers_as_uncertain(
    title: str,
    description: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": title,
                "description": description,
                "severity": "high",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]

    assert len(scenario["findings"]) == 1
    assert scenario["findings"][0]["status"] == "confirmed"
    assert scenario["findings"][0]["kind"] == "root_cause"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("疑似 Binder 问题", "现已通过 Trace 确认 Binder 是根因。"),
        ("需复核 Binder", "证据已经复核并确认。"),
    ],
)
def test_live_normalizer_uses_later_confirmation_for_same_diagnostic(
    title: str,
    description: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": title,
                "description": description,
                "severity": "high",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["status"] == "confirmed"
    assert finding["kind"] == "root_cause"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("疑似 JIT 问题", "现已通过 Trace 确认 Binder 是根因。"),
        ("需复核 Native 加载", "已确认 Binder 同步等待。"),
        ("候选 SDK 根因", "已确认首帧 Compose 测量过重。"),
    ],
)
def test_live_normalizer_does_not_use_unrelated_mechanism_as_confirmation(
    title: str,
    description: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": title,
                "description": description,
                "severity": "high",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["status"] == "suspected"
    assert finding["kind"] == "symptom"


@pytest.mark.parametrize(
    "description",
    [
        "疑似 JIT 但 Binder 已确认根因。",
        "JIT 相关性待核对但 Binder 已确认根因。",
        "已排除 Binder 的可能性但 JIT 仍是候选。",
        "Binder 不是候选但 JIT 可能影响启动。",
        "疑似 JIT 提示：Binder 已确认根因。",
    ],
)
def test_live_normalizer_keeps_unresolved_mechanism_uncertain_in_mixed_clause(
    description: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": "启动机制需要核对",
                "description": description,
                "severity": "high",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["status"] == "suspected"
    assert finding["kind"] == "symptom"


def test_live_normalizer_does_not_keep_resolved_info_limitation() -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": "数据限制",
                "description": "该限制已解除，Binder 同步等待是已确认根因。",
                "severity": "info",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]

    assert scenario["findings"]
    assert all(
        limitation["summary"]
        != "该限制已解除，Binder 同步等待是已确认根因。"
        for limitation in core["limitations"]  # type: ignore[index]
    )


def test_live_normalizer_links_diagnostic_only_to_explicitly_referenced_envelope_artifact() -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelopes = contract["dataEnvelopes"]  # type: ignore[assignment]
        envelopes[0]["meta"]["artifactId"] = "art-32"
        envelopes[1]["meta"]["artifactId"] = "art-99"
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic["description"] = "主线程热点由 art-32 直接支持。"

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    finding = scenario["findings"][0]
    metrics_by_evidence = {
        evidence_id: metric["name"]
        for metric in scenario["metrics"]
        for evidence_id in metric["sample_ids"]
    }

    assert len(finding["evidence_ids"]) == 1
    assert {
        metrics_by_evidence[evidence_id] for evidence_id in finding["evidence_ids"]
    } == {"startup.startup.frame_timeline.frame_count"}


def test_live_normalizer_does_not_bind_first_row_metric_from_referenced_multirow_artifact() -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelope = contract["dataEnvelopes"][0]  # type: ignore[index]
        envelope["meta"]["artifactId"] = "art-32"
        envelope["data"]["rows"] = [[7], [99]]
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic["description"] = "SDK 初始化结论引用 art-32。"

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    finding = scenario["findings"][0]
    metric_evidence_ids = {
        evidence_id
        for metric in scenario["metrics"]
        for evidence_id in metric["sample_ids"]
    }
    evidence_by_id = {
        evidence["evidence_id"]: evidence for evidence in scenario["evidence"]
    }
    multirow_sample_ids = {
        evidence["evidence_id"]
        for evidence in scenario["evidence"]
        if evidence["source"] == "smartperfetto.live_envelope"
        and 7 in evidence["fields"].values()
    }

    assert set(finding["evidence_ids"]).isdisjoint(metric_evidence_ids)
    assert {
        evidence_by_id[evidence_id]["source"]
        for evidence_id in finding["evidence_ids"]
    } == {"smartperfetto.live_envelope_artifact"}
    assert all(
        multirow_sample_ids.isdisjoint(metric["sample_ids"])
        for metric in scenario["metrics"]
    )


@pytest.mark.parametrize(
    "unsafe_title",
    [
        "外部 SDK 在启动主线程同步执行（bindApplication 非框架占 68.4%）",
        "Application.onCreate 中外部 SDK 同步初始化",
        "外部 SDK 在 bindApplication 串行初始化",
    ],
)
def test_live_normalizer_does_not_publish_unverified_lifecycle_from_multirow_artifact(
    unsafe_title: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelope = contract["dataEnvelopes"][0]  # type: ignore[index]
        envelope["meta"]["artifactId"] = "art-32"
        envelope["data"]["rows"] = [[7], [99]]
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic["title"] = unsafe_title
        diagnostic["description"] = (
            "QQMusicSdkAdapter.doInit 与 XeagleBtAdapter.start 都在 "
            "bindApplication 串行执行，证据见 art-32。"
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["title"] == "外部 SDK 在启动主线程同步初始化"
    assert finding["summary"] == (
        "Trace 显示 QQMusicSdkAdapter.doInit 与 XeagleBtAdapter.start "
        "都占用启动主线程；相关调用的生命周期入口必须分别定位。"
    )
    assert "都在 bindApplication" not in finding["summary"]


def test_live_normalizer_does_not_confirm_diagnostic_from_empty_artifact() -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelope = contract["dataEnvelopes"][0]  # type: ignore[index]
        envelope["meta"]["artifactId"] = "art-empty"
        envelope["data"]["rows"] = []
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": "Binder 等待",
                "description": "Binder 等待由 art-empty 直接支持。",
                "severity": "high",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]
    evidence_by_id = {
        item["evidence_id"]: item
        for item in core["scenario_reports"][0]["evidence"]  # type: ignore[index]
    }

    assert finding["status"] == "suspected"
    assert finding["kind"] == "symptom"
    assert all(
        evidence_by_id[evidence_id]["source"] != "smartperfetto.live_envelope_artifact"
        for evidence_id in finding["evidence_ids"]
    )


def test_live_normalizer_bounds_informational_diagnostics_without_orphaning_evidence() -> None:
    def mutate(contract: dict[str, object]) -> None:
        template = contract["diagnostics"][0]  # type: ignore[index]
        contract["diagnostics"] = [
            {
                **deepcopy(template),
                "id": f"info_{index}",
                "title": f"采集说明 {index}",
                "description": "当前仅用于说明采集状态。",
                "severity": "info",
            }
            for index in range(101)
        ]

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    retained_evidence_ids = {
        evidence_id
        for limitation in core["limitations"]  # type: ignore[index]
        for evidence_id in limitation["evidence_ids"]
    }

    assert len(core["limitations"]) == 20  # type: ignore[arg-type]
    assert len(scenario["evidence"]) <= 100
    assert any(
        item["source"] == "smartperfetto.live_envelope" for item in scenario["evidence"]
    )
    assert all(
        item["evidence_id"] in retained_evidence_ids
        for item in scenario["evidence"]
        if item["source"] == "smartperfetto.diagnostic"
    )


def test_live_normalizer_retains_only_evidence_closed_findings_at_global_cap() -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelope_template = contract["dataEnvelopes"][0]  # type: ignore[index]
        diagnostic_template = contract["diagnostics"][0]  # type: ignore[index]
        contract["dataEnvelopes"] = [
            {
                **deepcopy(envelope_template),
                "meta": {
                    **deepcopy(envelope_template["meta"]),
                    "source": f"source_{index}",
                    "artifactId": "art-a" if index < 20 else "art-b",
                },
            }
            for index in range(40)
        ]
        contract["diagnostics"] = [
            {
                **deepcopy(diagnostic_template),
                "id": f"diagnostic_{index}",
                "title": f"Binder 等待 {index}",
                "description": (
                    "Binder 等待由 art-a 直接支持。"
                    if index == 0
                    else "Binder 等待由 art-b 直接支持。"
                    if index == 1
                    else "Binder 等待需要进一步核对。"
                ),
                "severity": "high",
                "confidence": "high",
            }
            for index in range(80)
        ]

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    retained = {item["evidence_id"] for item in scenario["evidence"]}

    assert len(scenario["evidence"]) <= 100
    assert all(
        set(finding["evidence_ids"]).issubset(retained)
        for finding in scenario["findings"]
    )
    assert all(
        set(limitation["evidence_ids"]).issubset(retained)
        for limitation in core["limitations"]  # type: ignore[index]
    )


def test_live_normalizer_prioritizes_high_severity_finding_before_unreferenced_envelopes() -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelope_template = contract["dataEnvelopes"][0]  # type: ignore[index]
        diagnostic_template = contract["diagnostics"][0]  # type: ignore[index]
        contract["dataEnvelopes"] = [
            {
                **deepcopy(envelope_template),
                "meta": {
                    **deepcopy(envelope_template["meta"]),
                    "source": f"source_{index}",
                    "artifactId": f"unused-{index}",
                },
            }
            for index in range(70)
        ]
        contract["diagnostics"] = [
            {
                **deepcopy(diagnostic_template),
                "id": f"diagnostic_{index}",
                "title": "关键根因" if index == 34 else f"普通现象 {index}",
                "description": "主线程同步 Binder 等待。",
                "severity": "critical" if index == 34 else "low",
                "confidence": "high",
            }
            for index in range(35)
        ]

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]

    assert len(scenario["evidence"]) <= 100
    assert any(
        finding["title"] == "关键根因" for finding in scenario["findings"]
    )


def test_live_normalizer_prioritizes_critical_diagnostic_after_eighty_low_items() -> None:
    def mutate(contract: dict[str, object]) -> None:
        template = contract["diagnostics"][0]  # type: ignore[index]
        contract["diagnostics"] = [
            {
                **deepcopy(template),
                "id": f"diagnostic_{index}",
                "title": "关键根因" if index == 80 else f"普通现象 {index}",
                "description": "主线程同步 Binder 等待。",
                "severity": "critical" if index == 80 else "low",
                "confidence": "high",
            }
            for index in range(81)
        ]

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    findings = core["scenario_reports"][0]["findings"]  # type: ignore[index]

    assert len(findings) <= 80
    assert any(finding["title"] == "关键根因" for finding in findings)


def test_live_normalizer_keeps_referenced_envelope_after_seventy_unused_items() -> None:
    def mutate(contract: dict[str, object]) -> None:
        envelope_template = contract["dataEnvelopes"][0]  # type: ignore[index]
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        contract["dataEnvelopes"] = [
            {
                **deepcopy(envelope_template),
                "meta": {
                    **deepcopy(envelope_template["meta"]),
                    "source": f"source_{index}",
                    "artifactId": (
                        "critical-art" if index == 70 else f"unused-{index}"
                    ),
                },
            }
            for index in range(71)
        ]
        diagnostic.update(
            {
                "title": "关键根因",
                "description": "主线程同步 Binder 等待由 critical-art 直接支持。",
                "severity": "critical",
                "confidence": "high",
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    scenario = core["scenario_reports"][0]  # type: ignore[index]
    evidence_by_id = {
        item["evidence_id"]: item for item in scenario["evidence"]
    }
    finding = next(
        item for item in scenario["findings"] if item["title"] == "关键根因"
    )

    assert any(
        evidence_by_id[evidence_id]["source"] == "smartperfetto.live_envelope"
        for evidence_id in finding["evidence_ids"]
    )


def test_live_normalizer_treats_fork_load_hint_as_uncertain_correlation() -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic.update(
            {
                "title": "并发进程 fork 干扰 + 调度延迟",
                "description": (
                    "启动窗口内有其他进程 fork；主线程 Runnable 状态偏高，"
                    "提示后台负载抬高调度等待。"
                ),
                "severity": "medium",
                "confidence": 0.7,
                "evidence": None,
            }
        )

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["status"] == "suspected"
    assert finding["kind"] == "symptom"


def test_live_normalizer_removes_internal_sr_labels_from_public_titles() -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic["title"] = "三方 SDK 主线程初始化 — SR12（wall 439.9 ms）"

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["title"] == "三方 SDK 主线程初始化（wall 439.9 ms）"
    assert "SR12" not in finding["title"]


@pytest.mark.parametrize(
    ("raw_title", "expected_title"),
    [
        ("Native 库加载 — SR13：42 个 .so", "Native 库加载：42 个 .so"),
        ("主线程锁竞争 — SR04：232 次", "主线程锁竞争：232 次"),
    ],
)
def test_live_normalizer_removes_internal_sr_labels_without_dangling_separators(
    raw_title: str,
    expected_title: str,
) -> None:
    def mutate(contract: dict[str, object]) -> None:
        diagnostic = contract["diagnostics"][0]  # type: ignore[index]
        diagnostic["title"] = raw_title

    core = normalize_live_smartperfetto_result(
        _mutated_live_source(mutate)
    ).document
    finding = core["scenario_reports"][0]["findings"][0]  # type: ignore[index]

    assert finding["title"] == expected_title


def test_live_normalizer_namespaces_public_ids_by_scenario() -> None:
    startup_report = normalize_live_smartperfetto_result(
        _live_source(
            "startup", artifact_id="85000000-0000-4000-8000-000000000001"
        ),
        analysis_mode="device",
    )
    scroll_report = normalize_live_smartperfetto_result(
        _live_source(
            "scroll", artifact_id="85000000-0000-4000-8000-000000000002"
        ),
        analysis_mode="device",
    )
    startup = startup_report.document
    scroll = scroll_report.document

    startup_scenario = startup["scenario_reports"][0]  # type: ignore[index]
    scroll_scenario = scroll["scenario_reports"][0]  # type: ignore[index]
    for collection, identifier in (
        ("metrics", "metric_id"),
        ("findings", "finding_id"),
        ("evidence", "evidence_id"),
    ):
        startup_ids = {item[identifier] for item in startup_scenario[collection]}
        scroll_ids = {item[identifier] for item in scroll_scenario[collection]}
        assert startup_ids.isdisjoint(scroll_ids)
    assert {
        item["limitation_id"] for item in startup["limitations"]  # type: ignore[index]
    }.isdisjoint(
        item["limitation_id"] for item in scroll["limitations"]  # type: ignore[index]
    )

    merged_document = {
        **startup,
        "scenario_reports": [
            startup["scenario_reports"][0],  # type: ignore[index]
            scroll["scenario_reports"][0],  # type: ignore[index]
        ],
        "limitations": [*startup["limitations"], *scroll["limitations"]],  # type: ignore[index]
    }
    merged_bytes = canonical_json_bytes(
        validate_contract("normalized-trace-report", merged_document)
    )
    projection = build_ai_projection(
        NormalizedTraceReport(
            canonical_bytes=merged_bytes,
            sha256_b64=base64.b64encode(hashlib.sha256(merged_bytes).digest()).decode(),
        ),
        analysis_profile="auto",
        question=None,
    )
    validate_synthesis_output(
        projection=projection,
        candidate={
            "schema_version": "2.0",
            "verdict": "已完成两个场景的证据校验。",
            "executive_summary": "已完成两个场景的证据校验。",
            "key_metric_ids": [],
            "conclusions": [
                {
                    "finding_id": finding["finding_id"],
                    "evidence_ids": finding["evidence_ids"],
                    "source_ref_ids": [],
                    "problem": "SmartPerfetto 发现性能问题。",
                    "cause": "Trace 证据表明关键执行存在阻塞。",
                    "source_root_cause": "当前没有源码证据定位具体实现。",
                    "recommendation": "缩短关键路径并使用相同场景复测。",
                }
                for scenario in projection.document["scenarios"]
                for finding in scenario["findings"]
                if finding["status"] in {"confirmed", "suspected"}
                and finding["evidence_ids"]
            ],
            "top_findings": [],
            "recommendations": [],
            "source_fixes": [],
            "retest_plan": [],
            "limitations": [],
        },
    )


def test_normalizer_is_byte_stable_and_sorts_public_ids() -> None:
    original = json.loads(FIXTURE.read_text())
    reordered = {key: deepcopy(original[key]) for key in reversed(tuple(original))}
    first = normalize_smartperfetto_result(_source(original))
    second = normalize_smartperfetto_result(_source(reordered))
    assert first.canonical_bytes == second.canonical_bytes
    assert [item["scenario_type"] for item in first.document["scenario_reports"]] == ["startup", "scroll"]  # type: ignore[index]
    for scenario in first.document["scenario_reports"]:  # type: ignore[index]
        for key, identifier in (("metrics", "metric_id"), ("findings", "finding_id"), ("evidence", "evidence_id")):
            assert [item[identifier] for item in scenario[key]] == sorted(item[identifier] for item in scenario[key])  # type: ignore[index]


def test_normalizer_carries_authoritative_device_analysis_mode() -> None:
    report = normalize_smartperfetto_result(_source(), analysis_mode="device")

    assert report.document["analysis_mode"] == "device"


def test_normalizer_rejects_unknown_analysis_mode() -> None:
    with pytest.raises(
        SmartPerfettoNormalizationError,
        match="^SmartPerfetto result cannot be normalized$",
    ):
        normalize_smartperfetto_result(_source(), analysis_mode="memory_upload")  # type: ignore[arg-type]


def test_production_sanitizer_canonicalizer_and_normalizer_preserve_only_required_typed_fields() -> None:
    document = json.loads(FIXTURE.read_text())
    report = _report(document)
    report["reportUrl"] = "/api/reports/external-report-id"
    report["unknownPrivateField"] = "must-not-reach-canonical"
    response = SmartPerfettoReportResponse.model_validate({"success": True, "report": report})
    assert "dataEnvelopes" in response.sanitized_report
    assert "diagnostics" in response.sanitized_report
    assert "unknownPrivateField" not in response.sanitized_report
    assert "actions" not in response.sanitized_report
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=UUID("81000000-0000-4000-8000-000000000001"),
            analysis_id=UUID(str(document["analysis_id"])),
            execution_id=UUID(str(document["execution_id"])),
            expected_execution_version=1,
            tenant_resource_version=7,
            artifact_id=result_artifact_id(UUID(str(document["execution_id"]))),
            engine_id="smartperfetto",
            adapter_version="1.0.0",
            engine_commit_sha="1" * 40,
            engine_image_digest="sha256:" + "2" * 64,
            attempt_number=1,
            input_manifest_hash="3" * 64,
            config_hash="4" * 64,
            result=EngineResult(
                contract="workspace-agent-v1",
                state="completed",
                payload={"reportId": response.report_id, "report": response.sanitized_report},
            ),
        )
    )
    source = LoadedCanonicalResult(
        team_id=UUID("81000000-0000-4000-8000-000000000001"),
        analysis_id=UUID(str(document["analysis_id"])),
        execution_id=UUID(str(document["execution_id"])),
        artifact_id=result_artifact_id(UUID(str(document["execution_id"]))),
        tenant_resource_version=7,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )
    assert normalize_smartperfetto_result(source).document["scenario_reports"]


def test_normalizer_only_uses_verified_or_explicitly_partial_evidence_and_excludes_private_source_fields() -> None:
    core = normalize_smartperfetto_result(_source()).document
    encoded = canonical_json_bytes(core).decode()
    assert "startup.display_delay" in encoded
    assert "scroll.unverified" in encoded
    assert all(
        finding["rule_id"] != "scroll.unverified"
        for scenario in core["scenario_reports"]  # type: ignore[index]
        for finding in scenario["findings"]
    )
    for marker in ("conversation-secret", "query-secret", "notes-secret", "echoed-query-secret", "session-secret", "workspace-secret", "run-secret", "tool-secret", "external-report-id"):
        assert marker not in encoded
    assert core["limitations"]
    assert core["core_state"] == "complete"


def test_missing_measurement_value_is_insufficient_data_never_zero() -> None:
    document = json.loads(FIXTURE.read_text())
    _report(document)["dataEnvelopes"][0]["columns"][0].pop("value")  # type: ignore[index]
    core = normalize_smartperfetto_result(_source(document)).document
    metric = core["scenario_reports"][0]["metrics"][0]  # type: ignore[index]
    assert metric["status"] == "insufficient_data"
    assert metric["numeric_value"] is None


def test_normalizer_reparses_authoritative_bytes_not_mutable_loaded_document() -> None:
    source = _source()
    expected = normalize_smartperfetto_result(source).canonical_bytes
    source.document["result"] = {"state": "completed", "payload": {"report": {}}}
    assert normalize_smartperfetto_result(source).canonical_bytes == expected


def test_normalized_report_document_is_a_defensive_copy_of_its_canonical_bytes() -> None:
    report = normalize_smartperfetto_result(_source())
    document = report.document
    document["analysis_id"] = "mutated"
    assert report.document["analysis_id"] != "mutated"
    assert report.sha256_b64 == base64.b64encode(hashlib.sha256(report.canonical_bytes).digest()).decode()


def test_normalizer_rejects_forged_canonical_bytes_or_checksum() -> None:
    source = _source()
    for forged in (replace(source, canonical_bytes=b"{}"), replace(source, sha256_b64="x" * 44)):
        with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
            normalize_smartperfetto_result(forged)


@pytest.mark.parametrize("mutation", ["cross_scenario_evidence", "duplicate_metric_id"])
def test_normalizer_rejects_cross_scenario_metric_evidence_and_global_metric_id_collisions(
    mutation: str,
) -> None:
    document = json.loads(FIXTURE.read_text())
    envelopes = _report(document)["dataEnvelopes"]  # type: ignore[index]
    if mutation == "cross_scenario_evidence":
        envelopes[0]["columns"][0]["evidenceId"] = "scroll-jank"
    else:
        envelopes[1]["columns"][0]["id"] = envelopes[0]["columns"][0]["id"]
    with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
        normalize_smartperfetto_result(_source(document))


@pytest.mark.parametrize("mutation", ["cross_scenario_claim_evidence", "duplicate_envelope_id", "unknown_severity"])
def test_normalizer_rejects_cross_scenario_claim_evidence_duplicate_envelopes_and_unknown_severity(
    mutation: str,
) -> None:
    document = json.loads(FIXTURE.read_text())
    report = _report(document)
    if mutation == "cross_scenario_claim_evidence":
        report["diagnostics"][0]["claimRef"] = "scroll.partial"  # type: ignore[index]
    elif mutation == "duplicate_envelope_id":
        report["dataEnvelopes"][1]["id"] = report["dataEnvelopes"][0]["id"]  # type: ignore[index]
    else:
        report["diagnostics"][0]["severity"] = "unreviewed"  # type: ignore[index]
    with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
        normalize_smartperfetto_result(_source(document))


@pytest.mark.parametrize("mutation", ["version", "nan", "duplicate", "unsupported"])
def test_normalizer_fails_closed_for_unsupported_or_ambiguous_source(mutation: str) -> None:
    document = json.loads(FIXTURE.read_text())
    report = _report(document)
    if mutation == "version":
        report["resultContract"]["version"] = "9.9.9"  # type: ignore[index]
    elif mutation == "nan":
        report["dataEnvelopes"][0]["columns"][0]["value"] = float("nan")  # type: ignore[index]
    elif mutation == "duplicate":
        report["dataEnvelopes"][0]["columns"].append(deepcopy(report["dataEnvelopes"][0]["columns"][0]))  # type: ignore[index]
    else:
        report["dataEnvelopes"][0]["type"] = "unknown-envelope@1"  # type: ignore[index]
    with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
        normalize_smartperfetto_result(_source(document))
