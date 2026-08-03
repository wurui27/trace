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
