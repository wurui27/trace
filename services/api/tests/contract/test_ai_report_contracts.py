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
            lambda value: value["scenario_reports"][0]["metrics"][0].__setitem__(
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
    result = validate_contract("synthesis_output", value)
    result["executive_summary"] = "changed"
    assert value["executive_summary"] != "changed"
    assert canonical_json_bytes({"b": "值", "a": 1}) == b'{"a":1,"b":"\xe5\x80\xbc"}'

    private_value = {"executive_summary": "private-token"}
    with pytest.raises(ReportContractError) as exc_info:
        validate_contract("synthesis_output", private_value)
    assert str(exc_info.value) == "report contract is invalid"
    assert "private-token" not in str(exc_info.value)


def test_canonical_validation_rejects_non_finite_numbers() -> None:
    from perfpilot_api.reports import ReportContractError, validate_contract

    value = _example("analysis-projection.valid.json")
    value["scenario_reports"][0]["metrics"][0]["numeric_value"] = float("nan")
    with pytest.raises(ReportContractError):
        validate_contract("analysis_projection", value)
