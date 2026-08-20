from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

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
    assert first["primary_finding_ids"] == [finding["finding_id"]]


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
