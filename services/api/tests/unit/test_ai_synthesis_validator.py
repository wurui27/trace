from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.projection import AIProjection


ROOT = Path(__file__).parents[4]
UNKNOWN_ID = "87000000-0000-4000-8000-000000000001"


def _json_fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "contracts/v1/examples" / name).read_text(encoding="utf-8"))


def _projection_document() -> dict[str, object]:
    document = _json_fixture("analysis-projection-v2.valid.json")
    document["source_context"] = None
    return document


def _projection() -> AIProjection:
    payload = canonical_json_bytes(_projection_document())
    return AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")


def _candidate() -> dict[str, object]:
    document = _json_fixture("synthesis-output-v2.valid.json")
    document["source_fixes"] = []
    document["conclusions"][0]["source_ref_ids"] = []  # type: ignore[index]
    document["conclusions"][0]["source_root_cause"] = (  # type: ignore[index]
        "本次没有足够强的源码匹配，暂不能定位到具体实现。"
    )
    return document


def _projection_v21() -> AIProjection:
    document = _json_fixture("analysis-projection-v2.1.valid.json")
    payload = canonical_json_bytes(document)
    return AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")


def _candidate_v21() -> dict[str, object]:
    return _json_fixture("synthesis-output-v2.1.valid.json")


def test_v21_accepts_only_the_deterministic_finding_order() -> None:
    validated = _validate(_candidate_v21(), _projection_v21())
    assert validated.document["schema_version"] == "2.1"  # type: ignore[attr-defined]

    duplicate = deepcopy(_projection_v21().document["workbench"]["findings"][0])
    duplicate.update(
        finding_id="85000000-0000-4000-8000-000000000002",
        title="第二项问题",
        priority="p1",
        priority_score=70,
        retest_plan_id="89000000-0000-4000-8000-000000000002",
    )
    projection_document = deepcopy(_projection_v21().document)
    projection_document["workbench"]["findings"].append(duplicate)
    projection_document["workbench"]["retest_plans"].append(
        {
            **deepcopy(projection_document["workbench"]["retest_plans"][0]),
            "retest_plan_id": duplicate["retest_plan_id"],
            "finding_id": duplicate["finding_id"],
        }
    )
    payload = canonical_json_bytes(projection_document)
    projection = AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate_v21()

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


@pytest.mark.parametrize(
    ("claim_type", "metric_id", "evidence_id"),
    [
        ("metric_observed", UNKNOWN_ID, None),
        ("evidence_supports_mechanism", None, UNKNOWN_ID),
        (
            "evidence_on_critical_path",
            None,
            "86000000-0000-4000-8000-000000000002",
        ),
    ],
)
def test_v21_rejects_claims_outside_the_finding_workbench(
    claim_type: str,
    metric_id: str | None,
    evidence_id: str | None,
) -> None:
    projection_document = deepcopy(_projection_v21().document)
    if evidence_id and evidence_id != UNKNOWN_ID:
        extra = deepcopy(projection_document["workbench"]["evidence"][0])
        extra["evidence_id"] = evidence_id
        projection_document["workbench"]["evidence"].append(extra)
    payload = canonical_json_bytes(projection_document)
    projection = AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate_v21()
    candidate["conclusions"][0]["claim_refs"] = [
        {
            "claim_type": claim_type,
            "metric_id": metric_id,
            "evidence_id": evidence_id,
        }
    ]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


@pytest.mark.parametrize(
    "invented_number",
    [
        "耗时为一百毫秒。",
        "耗时为１秒。",
        "耗时为1秒。",
        "耗时为12ms。",
        "耗时为1e3 ms。",
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
        "内存增长一百 MB。",
        "频率升至三百 MHz。",
        "吞吐为五十 MB/s。",
        "帧率为六十 FPS。",
        "分配两百字节。",
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
def test_v21_rejects_free_written_measurements(invented_number: str) -> None:
    candidate = _candidate_v21()
    candidate["executive_summary"] = invented_number

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, _projection_v21())


@pytest.mark.parametrize(
    "lexical_text",
    [
        "Media3 与 H264 解码位于主线程。",
        "一般情况下应逐一复核一部分指标，并统一采样口径。",
        "另一方面需要核对外部 SDK 回调。",
        "第一帧需要逐一核对每一项证据。",
        "建议从三方面优化启动流程，避免一次性加载全部资源。",
        "另一个问题需要处理，另一项建议需要复核。",
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
def test_v21_allows_digits_and_number_characters_inside_non_measurement_words(
    lexical_text: str,
) -> None:
    candidate = _candidate_v21()
    candidate["executive_summary"] = lexical_text

    validated = _validate(candidate, _projection_v21())

    assert validated.document["executive_summary"] == lexical_text  # type: ignore[attr-defined]


def test_v21_low_confidence_cannot_be_narrated_as_confirmed_root_cause() -> None:
    projection_document = deepcopy(_projection_v21().document)
    finding = projection_document["workbench"]["findings"][0]
    finding["status"] = "hypothesis"
    finding["confidence_ceiling"] = "low"
    finding["confidence"]["attribution"] = "low"
    payload = canonical_json_bytes(projection_document)
    projection = AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate_v21()
    candidate["conclusions"][0]["cause"] = "已经确认根因是主线程同步初始化。"

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_v2_requires_a_four_part_conclusion_for_every_supported_problem() -> None:
    candidate = _candidate()
    candidate["conclusions"] = [
        {
            "finding_id": "85000000-0000-4000-8000-000000000001",
            "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
            "source_ref_ids": [],
            "problem": "启动首屏出现明显延迟。",
            "cause": "主线程同步等待与启动区间重叠。",
            "source_root_cause": "本次没有足够强的源码匹配，暂不能定位到具体实现。",
            "recommendation": "把同步查询移出启动关键路径，并按相同冷启动场景复测。",
        }
    ]

    validated = _validate(candidate)
    assert validated.document["conclusions"] == candidate["conclusions"]  # type: ignore[attr-defined]

    missing = deepcopy(candidate)
    missing["conclusions"] = []
    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(missing)


def test_v2_rejects_unknown_key_metric_reference() -> None:
    candidate = _candidate()
    candidate["key_metric_ids"] = [UNKNOWN_ID]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_v2_source_fix_must_match_server_validated_ref() -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _json_fixture("synthesis-output-v2.valid.json")
    candidate["source_fixes"][0]["source_ref_ids"] = [UNKNOWN_ID]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_v2_rejects_duplicate_source_action_binding() -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _json_fixture("synthesis-output-v2.valid.json")
    duplicate = deepcopy(candidate["source_fixes"][0])  # type: ignore[index]
    duplicate["fix_id"] = "96000000-0000-4000-8000-000000000002"
    candidate["source_fixes"].append(duplicate)  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_v2_allows_multiple_distinct_source_actions_for_one_finding() -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    second_ref = deepcopy(projection_document["source_context"]["fragments"][0])  # type: ignore[index]
    second_ref.update(
        {
            "source_ref_id": "97000000-0000-4000-8000-000000000002",
            "relative_path": "app/src/main/java/demo/StartupDelegate.kt",
            "symbol": "demo.StartupDelegate.loadSettings",
        }
    )
    projection_document["source_context"]["fragments"].append(second_ref)  # type: ignore[index]
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _json_fixture("synthesis-output-v2.valid.json")
    second_fix = deepcopy(candidate["source_fixes"][0])  # type: ignore[index]
    second_fix.update(
        {
            "fix_id": "96000000-0000-4000-8000-000000000002",
            "source_ref_ids": [second_ref["source_ref_id"]],
            "relative_path": second_ref["relative_path"],
            "symbol": second_ref["symbol"],
            "diff": second_fix["diff"].replace(
                "app/src/main/java/demo/MainActivity.kt",
                "app/src/main/java/demo/StartupDelegate.kt",
            ),
        }
    )
    candidate["source_fixes"].append(second_fix)  # type: ignore[union-attr]

    validated = _validate(candidate, projection)

    assert len(validated.document["source_fixes"]) == 2  # type: ignore[attr-defined]


def test_v2_strong_source_keeps_manual_plan_when_no_diff_is_safe() -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _json_fixture("synthesis-output-v2.valid.json")
    candidate["source_fixes"] = []

    validated = _validate(candidate, projection)

    assert validated.document["source_fixes"] == []  # type: ignore[attr-defined]
    assert validated.document["conclusions"][0]["recommendation"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "leaked_location",
    [
        "app/src/main/java/demo/MainActivity.kt",
        "demo.MainActivity.onCreate",
    ],
)
def test_v2_weak_source_rejects_locations_in_manual_plan(
    leaked_location: str,
) -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    source_context = projection_document["source_context"]
    source_context["match_summary"] = "weak"  # type: ignore[index]
    source_context["fragments"][0]["match_grade"] = "weak"  # type: ignore[index]
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _candidate()
    candidate["conclusions"][0]["recommendation"] = (  # type: ignore[index]
        f"调整 {leaked_location} 后重新采集启动场景。"
    )

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


@pytest.mark.parametrize(
    ("field", "leaked_location"),
    [
        ("verdict", "app/src/main/java/demo/MainActivity.kt"),
        ("executive_summary", "demo.MainActivity.onCreate"),
    ],
)
def test_v2_weak_source_rejects_locations_in_every_narrative_field(
    field: str,
    leaked_location: str,
) -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    source_context = projection_document["source_context"]
    source_context["match_summary"] = "weak"  # type: ignore[index]
    source_context["fragments"][0]["match_grade"] = "weak"  # type: ignore[index]
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _candidate()
    candidate[field] = leaked_location

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_v2_conclusion_uses_available_strong_source_reference() -> None:
    projection_document = _json_fixture("analysis-projection-v2.valid.json")
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _json_fixture("synthesis-output-v2.valid.json")
    candidate["conclusions"][0]["source_ref_ids"] = []  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def _validator() -> object:
    from perfpilot_api.ai.synthesis import validate_synthesis_output  # type: ignore[import-not-found]

    return validate_synthesis_output


def _validate(candidate: dict[str, object], projection: AIProjection | None = None) -> object:
    return _validator()(projection=projection or _projection(), candidate=candidate)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("top_findings", "finding_id"),
        ("top_findings", "evidence_ids"),
        ("recommendations", "finding_ids"),
        ("recommendations", "evidence_ids"),
        ("retest_plan", "metric_ids"),
    ],
)
def test_rejects_unknown_projection_references(section: str, field: str) -> None:
    candidate = _candidate()
    item = candidate[section][0]  # type: ignore[index]
    item[field] = UNKNOWN_ID if field.endswith("_id") else [UNKNOWN_ID]  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_unknown_limitation_reference() -> None:
    candidate = _candidate()
    candidate["limitations"] = [{"limitation_id": UNKNOWN_ID, "summary": "Missing input."}]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_top_finding_evidence_that_does_not_support_the_finding() -> None:
    projection_document = _projection_document()
    scenario = projection_document["scenarios"][0]
    scenario["evidence"].append(  # type: ignore[index]
        {
            "evidence_id": UNKNOWN_ID,
            "source": "perfetto.other",
            "query_id": "other.v1",
            "interval_start_ns": None,
            "interval_end_ns": None,
            "artifact_id": None,
            "fields": {},
        }
    )
    projection = AIProjection(canonical_bytes=canonical_json_bytes(projection_document), sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate()
    candidate["top_findings"][0]["evidence_ids"] = [UNKNOWN_ID]  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_rejects_recommendation_evidence_unrelated_to_cited_finding() -> None:
    projection_document = _projection_document()
    scenario = projection_document["scenarios"][0]
    scenario["evidence"].append(  # type: ignore[index]
        {
            "evidence_id": UNKNOWN_ID,
            "source": "perfetto.other",
            "query_id": "other.v1",
            "interval_start_ns": None,
            "interval_end_ns": None,
            "artifact_id": None,
            "fields": {},
        }
    )
    projection = AIProjection(
        canonical_bytes=canonical_json_bytes(projection_document),
        sha256_b64="Y2hlY2tzdW0=",
    )
    candidate = _candidate()
    candidate["recommendations"][0]["evidence_ids"] = [UNKNOWN_ID]  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_rejects_recommendation_without_actionable_finding_or_evidence() -> None:
    candidate = _candidate()
    recommendation = candidate["recommendations"][0]  # type: ignore[index]
    recommendation["finding_ids"] = []  # type: ignore[index]
    recommendation["evidence_ids"] = []  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


@pytest.mark.parametrize("status", ["insufficient_data", "invalid_capture"])
def test_rejects_recommendation_for_non_actionable_finding(status: str) -> None:
    projection_document = _projection_document()
    finding = projection_document["scenarios"][0]["findings"][0]  # type: ignore[index]
    finding["status"] = status
    finding["confidence"] = "none"
    projection = AIProjection(canonical_bytes=canonical_json_bytes(projection_document), sha256_b64="Y2hlY2tzdW0=")

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(_candidate(), projection)


def test_rejects_verify_metric_for_another_scenario() -> None:
    projection_document = _projection_document()
    second = deepcopy(projection_document["scenarios"][0])  # type: ignore[index]
    second["scenario_id"] = UNKNOWN_ID
    second["scenario_type"] = "scroll"
    projection_document["scenarios"].append(second)  # type: ignore[index]
    projection = AIProjection(canonical_bytes=canonical_json_bytes(projection_document), sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate()
    candidate["retest_plan"][0]["scenario_type"] = "scroll"  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metric_ids", []),
        ("steps", "Repeat cold launches and reach 600 ms."),
    ],
)
def test_rejects_verify_metric_without_a_metric_or_with_a_new_target(
    field: str, value: object
) -> None:
    candidate = _candidate()
    candidate["retest_plan"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_collect_evidence_retest_without_an_existing_limitation() -> None:
    candidate = _candidate()
    candidate["retest_plan"] = [
        {
            "mode": "collect_evidence",
            "scenario_type": "startup",
            "metric_ids": [],
            "limitation_ids": [UNKNOWN_ID],
            "steps": "Capture the missing evidence.",
            "success_condition": "evidence_collected",
            "failure_condition": "evidence_missing",
        }
    ]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


@pytest.mark.parametrize(
    "field_path",
    [
        ("executive_summary",),
        ("top_findings", 0, "user_impact"),
        ("recommendations", 0, "title"),
        ("recommendations", 0, "action"),
        ("recommendations", 0, "expected_effect"),
        ("retest_plan", 0, "steps"),
    ],
)
def test_rejects_new_numeric_literals_in_narrative_fields(field_path: tuple[str | int, ...]) -> None:
    candidate = _candidate()
    target: object = candidate
    for key in field_path[:-1]:
        target = target[key]  # type: ignore[index]
    target[field_path[-1]] = "A newly invented target is 16.0 ms."  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_invented_numeric_target_attached_to_unit() -> None:
    candidate = _candidate()
    candidate["retest_plan"][0]["steps"] = "Reduce the target to 16ms."

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_allows_digits_embedded_in_technical_identifiers() -> None:
    candidate = _candidate()
    candidate["recommendations"][0]["action"] = (  # type: ignore[index]
        "Move fzlthpro_gb18030.ttf off the startup path."
    )

    _validate(candidate)


@pytest.mark.parametrize(
    ("section", "identifier"),
    [("recommendations", "recommendation_id"), ("retest_plan", "retest_id")],
)
def test_rejects_ai_created_public_ids(section: str, identifier: str) -> None:
    candidate = _candidate()
    candidate[section][0][identifier] = UNKNOWN_ID  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_private_strings_with_a_stable_redacted_error() -> None:
    candidate = _candidate()
    private_value = "https://objects.invalid/a?X-Amz-Signature=private-secret"
    candidate["executive_summary"] = private_value

    with pytest.raises(ValueError) as caught:
        _validate(candidate)

    assert str(caught.value) == "AI synthesis output is invalid"
    assert private_value not in repr(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"1.0","schema_version":"1.0"}',
        b'{"schema_version":NaN}',
        b'{"schema_version":Infinity}',
        b'{} trailing',
        b"\xff",
    ],
)
def test_parse_candidate_rejects_malformed_or_ambiguous_json(payload: bytes) -> None:
    from perfpilot_api.ai.synthesis import parse_candidate  # type: ignore[import-not-found]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        parse_candidate(payload, max_bytes=128 * 1024)


def test_parse_candidate_rejects_empty_and_oversized_payloads() -> None:
    from perfpilot_api.ai.synthesis import parse_candidate  # type: ignore[import-not-found]

    for payload in (b"", b"x" * 17):
        with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
            parse_candidate(payload, max_bytes=16)


def test_returns_defensive_canonical_document() -> None:
    result = _validate(_candidate())
    document = result.document  # type: ignore[attr-defined]
    document["executive_summary"] = "changed"

    assert result.document["executive_summary"] != "changed"  # type: ignore[attr-defined]
    assert result.canonical_bytes == canonical_json_bytes(result.document)  # type: ignore[attr-defined]
