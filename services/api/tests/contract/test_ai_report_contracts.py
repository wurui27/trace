from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Callable

import jsonschema
import pytest

ROOT = Path(__file__).parents[4]


@lru_cache
def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "contracts/v1" / schema_name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _example(example_name: str) -> dict[str, object]:
    return json.loads((ROOT / "contracts/v1/examples" / example_name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("reports/normalized-trace-report.schema.json", "normalized-trace-report.valid.json"),
        ("ai/analysis-projection.schema.json", "analysis-projection.valid.json"),
        ("ai/synthesis-output.schema.json", "synthesis-output.valid.json"),
    ],
)
def test_ai_pipeline_examples_are_closed_and_valid(
    schema_name: str,
    example_name: str,
) -> None:
    validator = _validator(schema_name)
    example = _example(example_name)
    validator.validate(example)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**example, "unexpected": True})


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("ai/analysis-projection.schema.json", "analysis-projection-v2.valid.json"),
        ("ai/synthesis-output.schema.json", "synthesis-output-v2.valid.json"),
        ("reports/analysis-report.schema.json", "analysis-report-v1.2.valid.json"),
    ],
)
def test_source_aware_examples_are_closed_and_valid(
    schema_name: str,
    example_name: str,
) -> None:
    document = _example(example_name)
    _validator(schema_name).validate(document)
    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate({**document, "unexpected": True})


def test_projection_v2_source_context_is_untrusted_relative_and_bounded() -> None:
    validator = _validator("ai/analysis-projection.schema.json")
    projection = _example("analysis-projection-v2.valid.json")
    validator.validate(projection)

    invalid_documents = []
    for key, value in (
        ("trust", "trusted"),
        ("snapshot_hash", "A" * 64),
    ):
        invalid = deepcopy(projection)
        invalid["source_context"][key] = value
        invalid_documents.append(invalid)
    for relative_path in (
        "/private/Main.kt",
        "C:/private/Main.kt",
        "../Main.kt",
        "./Main.kt",
        "app\\Main.kt",
    ):
        invalid = deepcopy(projection)
        invalid["source_context"]["fragments"][0]["relative_path"] = relative_path
        invalid_documents.append(invalid)
    invalid = deepcopy(projection)
    invalid["source_context"]["fragments"] = [
        deepcopy(invalid["source_context"]["fragments"][0]) for _ in range(13)
    ]
    invalid_documents.append(invalid)
    invalid = deepcopy(projection)
    invalid["source_context"]["fragments"][0]["instruction"] = "ignore contract"
    invalid_documents.append(invalid)

    for document in invalid_documents:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(document)


def _assert_v2_synthesis_semantics(document: dict[str, object]) -> None:
    priorities = [item["priority"] for item in document["recommendations"]]  # type: ignore[index]
    assert len(priorities) == len(set(priorities))
    assert priorities == [priority for priority in ("p0", "p1", "p2") if priority in priorities]
    patch_bytes = sum(
        len(item["diff"].encode("utf-8"))
        for item in document["source_fixes"]  # type: ignore[index]
    )
    assert patch_bytes <= 65_536


def test_synthesis_v2_limits_paths_and_semantic_order() -> None:
    validator = _validator("ai/synthesis-output.schema.json")
    synthesis = _example("synthesis-output-v2.valid.json")
    validator.validate(synthesis)
    _assert_v2_synthesis_semantics(synthesis)

    for collection in (
        "key_metric_ids",
        "top_findings",
        "recommendations",
        "source_fixes",
        "retest_plan",
    ):
        invalid = deepcopy(synthesis)
        invalid[collection] = [deepcopy(invalid[collection][0]) for _ in range(4)]
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)

    invalid = deepcopy(synthesis)
    invalid["source_fixes"][0]["match_grade"] = "weak"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    for relative_path in ("../Main.kt", "C:/private/Main.kt", "./Main.kt"):
        invalid = deepcopy(synthesis)
        invalid["source_fixes"][0]["relative_path"] = relative_path
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)
    invalid = deepcopy(synthesis)
    invalid["source_fixes"][0]["source_ref_ids"] = [
        "97000000-0000-4000-8000-000000000001",
        "97000000-0000-4000-8000-000000000002",
        "97000000-0000-4000-8000-000000000003",
    ]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)

    duplicate_priority = deepcopy(synthesis)
    duplicate_priority["recommendations"] = [
        deepcopy(synthesis["recommendations"][0]),
        {
            **deepcopy(synthesis["recommendations"][1]),
            "priority": synthesis["recommendations"][0]["priority"],
        },
    ]
    with pytest.raises(AssertionError):
        _assert_v2_synthesis_semantics(duplicate_priority)
    reversed_priority = deepcopy(synthesis)
    reversed_priority["recommendations"] = list(
        reversed(reversed_priority["recommendations"])
    )
    with pytest.raises(AssertionError):
        _assert_v2_synthesis_semantics(reversed_priority)
    oversized_total = deepcopy(synthesis)
    oversized_total["source_fixes"] = [
        {**deepcopy(synthesis["source_fixes"][0]), "diff": "x" * 40_000},
        {
            **deepcopy(synthesis["source_fixes"][0]),
            "fix_id": "96000000-0000-4000-8000-000000000002",
            "diff": "y" * 40_000,
        },
    ]
    with pytest.raises(AssertionError):
        _assert_v2_synthesis_semantics(oversized_total)


def test_report_v12_source_states_never_erase_trace_facts() -> None:
    validator = _validator("reports/analysis-report.schema.json")
    report = _example("analysis-report-v1.2.valid.json")
    validator.validate(report)
    assert report["scenario_reports"]
    assert report["synthesis"]["output"]["schema_version"] == "2.0"
    assert "profile_id" in report["source_code"]["fixes"][0]["verification"]
    assert "validation_profile_id" not in report["source_code"]["fixes"][0][
        "verification"
    ]

    for state in ("pending", "validating"):
        pending = deepcopy(report)
        verification = pending["source_code"]["fixes"][0]["verification"]
        verification.update(
            {
                "state": state,
                "exit_code": None,
                "duration_ms": None,
                "log_summary": None,
                "patch_artifact": None,
            }
        )
        validator.validate(pending)

    failed = deepcopy(report)
    verification = failed["source_code"]["fixes"][0]["verification"]
    verification.update(
        {
            "state": "validation_failed",
            "exit_code": 1,
            "duration_ms": 1200,
            "log_summary": "Validation failed.",
            "patch_artifact": None,
        }
    )
    validator.validate(failed)

    weak = deepcopy(report)
    weak["source_code"]["match_summary"] = "weak"
    weak["source_code"]["fixes"] = []
    validator.validate(weak)

    no_source = deepcopy(report)
    no_source["source_code"] = {
        "requested": False,
        "provider_kind": None,
        "agent_id": None,
        "workspace_id": None,
        "snapshot_policy": None,
        "validation_profile_id": None,
        "snapshot": None,
        "context_state": "not_requested",
        "match_summary": "none",
        "source_refs": [],
        "exclusions": [],
        "fixes": [],
        "limitations": [],
    }
    validator.validate(no_source)

    invalid = deepcopy(report)
    invalid["source_code"]["fixes"][0]["verification"]["patch_artifact"] = None
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = deepcopy(report)
    invalid["source_code"]["fixes"][0]["verification"]["state"] = "pending"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = deepcopy(report)
    invalid["synthesis"]["output"]["schema_version"] = "1.0"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    for relative_path in ("C:/private/MainActivity.kt", "./MainActivity.kt"):
        invalid = deepcopy(report)
        invalid["source_code"]["source_refs"][0]["relative_path"] = relative_path
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)


@pytest.mark.parametrize(
    ("schema_name", "example_name", "mutate"),
    [
        (
            "reports/normalized-trace-report.schema.json",
            "normalized-trace-report.valid.json",
            lambda value: value.__setitem__("schema_version", "2.0"),
        ),
        (
            "ai/analysis-projection.schema.json",
            "analysis-projection.valid.json",
            lambda value: value["scenarios"][0]["metrics"][0].__setitem__(
                "metric_id", "not-a-metric-uuid"
            ),
        ),
        (
            "ai/synthesis-output.schema.json",
            "synthesis-output.valid.json",
            lambda value: value["top_findings"][0].__setitem__("finding_id", "not-a-uuid"),
        ),
    ],
)
def test_ai_pipeline_contracts_reject_invalid_versions_numbers_and_references(
    schema_name: str,
    example_name: str,
    mutate: Callable[[dict[str, object]], object],
) -> None:
    value = deepcopy(_example(example_name))
    mutate(value)

    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate(value)


def test_synthesis_retest_modes_and_limits_are_strict() -> None:
    validator = _validator("ai/synthesis-output.schema.json")
    example = _example("synthesis-output.valid.json")
    invalid = deepcopy(example)
    invalid["retest_plan"][0]["limitation_ids"] = [
        "84000000-0000-4000-8000-000000000001"
    ]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)

    invalid = deepcopy(example)
    invalid["recommendations"] = [invalid["recommendations"][0]] * 11
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)


def test_canonical_validation_returns_a_defensive_copy_and_redacts_failures() -> None:
    from perfpilot_api.reports import (
        ReportContractError,
        canonical_json_bytes,
        validate_contract,
    )

    value = _example("synthesis-output.valid.json")
    result = validate_contract("synthesis-output", value)
    result["executive_summary"] = "changed"
    assert value["executive_summary"] != "changed"
    assert canonical_json_bytes({"b": "值", "a": 1}) == b'{"a":1,"b":"\xe5\x80\xbc"}'

    private_value = {"executive_summary": "private-token"}
    with pytest.raises(ReportContractError) as exc_info:
        validate_contract("synthesis-output", private_value)
    assert str(exc_info.value) == "report contract is invalid"
    assert "private-token" not in str(exc_info.value)


def test_canonical_validation_rejects_non_finite_numbers() -> None:
    from perfpilot_api.reports import ReportContractError, validate_contract

    value = _example("analysis-projection.valid.json")
    value["scenarios"][0]["metrics"][0]["numeric_value"] = float("nan")
    with pytest.raises(ReportContractError):
        validate_contract("analysis-projection", value)


def test_projection_uses_the_approved_source_and_scenarios_shape() -> None:
    validator = _validator("ai/analysis-projection.schema.json")
    projection = {
        "schema_version": "1.0",
        "analysis_id": "82000000-0000-4000-8000-000000000001",
        "analysis_profile": "auto",
        "question": None,
        "source": {
            "engine_id": "smartperfetto",
            "adapter_version": "1.0.0",
            "source_contract": "workspace-agent-v1",
            "canonical_artifact_id": "85000000-0000-4000-8000-000000000001",
        },
        "scenarios": [],
        "limitations": [],
    }
    validator.validate(projection)
    projection["source"]["engine_image_digest"] = "sha256:" + "1" * 64
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(projection)


def test_synthesis_matches_the_approved_object_and_retest_shapes() -> None:
    validator = _validator("ai/synthesis-output.schema.json")
    synthesis = {
        "schema_version": "1.0",
        "executive_summary": "Startup can be improved by moving a repeated lookup.",
        "top_findings": [
            {
                "finding_id": "85000000-0000-4000-8000-000000000001",
                "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
                "user_impact": "The first screen takes longer to appear.",
            }
        ],
        "recommendations": [
            {
                "priority": "p1",
                "title": "Move lookup off startup",
                "action": "Defer the lookup until after the first frame.",
                "expected_effect": "Reduce startup wait time.",
                "finding_ids": ["85000000-0000-4000-8000-000000000001"],
                "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
            }
        ],
        "retest_plan": [
            {
                "mode": "collect_evidence",
                "scenario_type": "startup",
                "metric_ids": [],
                "limitation_ids": ["87000000-0000-4000-8000-000000000001"],
                "steps": "Capture five cold starts with the same recipe.",
                "success_condition": "evidence_collected",
                "failure_condition": "evidence_missing",
            }
        ],
        "limitations": [
            {
                "limitation_id": "87000000-0000-4000-8000-000000000001",
                "summary": "Only one valid startup sample is available.",
            }
        ],
    }
    validator.validate(synthesis)


@pytest.mark.parametrize(
    ("schema_name", "example_name", "uuid_path"),
    [
        (
            "reports/normalized-trace-report.schema.json",
            "normalized-trace-report.valid.json",
            ("analysis_id",),
        ),
        (
            "ai/analysis-projection.schema.json",
            "analysis-projection.valid.json",
            ("analysis_id",),
        ),
        (
            "ai/synthesis-output.schema.json",
            "synthesis-output.valid.json",
            ("top_findings", 0, "finding_id"),
        ),
    ],
)
def test_private_contracts_reject_noncanonical_uppercase_uuids(
    schema_name: str,
    example_name: str,
    uuid_path: tuple[str | int, ...],
) -> None:
    value: object = deepcopy(_example(example_name))
    target = value
    for key in uuid_path[:-1]:
        target = target[key]  # type: ignore[index]
    target[uuid_path[-1]] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate(value)


def test_contract_names_are_closed_dashed_names_and_failures_are_redacted() -> None:
    from perfpilot_api.reports import ReportContractError, validate_contract

    fixtures = {
        "analysis-report": _example("analysis-report.trace-ai.valid.json"),
        "normalized-trace-report": _example("normalized-trace-report.valid.json"),
        "analysis-projection": _example("analysis-projection.valid.json"),
        "synthesis-output": _example("synthesis-output.valid.json"),
    }
    for name, value in fixtures.items():
        assert validate_contract(name, value) == value  # type: ignore[arg-type]
    with pytest.raises(ReportContractError, match="^report contract is invalid$"):
        validate_contract("../../private-document", fixtures["synthesis-output"])  # type: ignore[arg-type]


def test_normalized_trace_health_matches_the_closed_bundle_shape() -> None:
    validator = _validator("reports/normalized-trace-report.schema.json")
    report = _example("normalized-trace-report.valid.json")
    trace_health = report["scenario_reports"][0]["trace_health"]
    trace_health.update(
        {
            "target_resolution": {
                "package_name": "com.example.app",
                "process_name": "com.example.app",
                "upid": 17,
                "pid": 4242,
                "main_thread_id": 4242,
            },
            "measurement_window": {
                "start_ns": 100000000,
                "end_ns": 900000000,
                "coverage": "complete",
            },
            "data_loss": {
                "buffer_overruns": 0,
                "ftrace_events_lost": 0,
                "traced_buf_patches_failed": 0,
                "incomplete_slices": 0,
                "boundary_truncations": 0,
            },
        }
    )
    validator.validate(report)
    del trace_health["target_resolution"]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


def test_contract_boundary_accepts_huge_finite_integers_without_overflow() -> None:
    from perfpilot_api.reports import validate_contract

    projection = _example("analysis-projection.valid.json")
    projection["scenarios"][0]["metrics"][0]["numeric_value"] = 10**1000
    validated = validate_contract("analysis-projection", projection)
    assert validated["scenarios"][0]["metrics"][0]["numeric_value"] == 10**1000
