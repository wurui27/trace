import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).parents[4]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def validator(schema: dict) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def test_event_envelope_example_matches_schema() -> None:
    schema = load("contracts/v1/events/event-envelope.schema.json")
    payload = load("contracts/v1/examples/event-envelope.valid.json")
    validator(schema).validate(payload)


@pytest.mark.parametrize(
    ("field", "malformed_uuid"),
    [
        ("event_id", "not-an-event-uuid"),
        ("subject_id", "not-a-subject-uuid"),
    ],
)
def test_event_envelope_rejects_malformed_uuid(field: str, malformed_uuid: str) -> None:
    schema = load("contracts/v1/events/event-envelope.schema.json")
    payload = load("contracts/v1/examples/event-envelope.valid.json")
    payload[field] = malformed_uuid

    with pytest.raises(jsonschema.ValidationError):
        validator(schema).validate(payload)


def test_error_example_rejects_missing_request_id() -> None:
    schema = load("contracts/v1/common/error.schema.json")
    payload = load("contracts/v1/examples/error.invalid.json")
    errors = list(validator(schema).iter_errors(payload))
    assert [error.validator for error in errors] == ["required"]
    assert [list(error.absolute_path) for error in errors] == [["error"]]
    assert [error.message for error in errors] == ["'request_id' is a required property"]


def test_trace_ai_analysis_report_example_matches_v11_schema() -> None:
    schema = load("contracts/v1/reports/analysis-report.schema.json")
    payload = load("contracts/v1/examples/analysis-report.trace-ai.valid.json")

    validator(schema).validate(payload)


def test_analysis_report_versions_keep_synthesis_unambiguous() -> None:
    report_schema = validator(load("contracts/v1/reports/analysis-report.schema.json"))
    legacy = load("contracts/v1/examples/analysis-report.partial.valid.json")
    report_schema.validate(legacy)
    legacy["synthesis"] = None
    with pytest.raises(jsonschema.ValidationError):
        report_schema.validate(legacy)

    trace_ai = load("contracts/v1/examples/analysis-report.trace-ai.valid.json")
    reordered = deepcopy(trace_ai)
    reordered["scenario_reports"][0], reordered["scenario_reports"][1] = (
        reordered["scenario_reports"][1],
        reordered["scenario_reports"][0],
    )
    with pytest.raises(jsonschema.ValidationError):
        report_schema.validate(reordered)

    failed_synthesis = deepcopy(trace_ai)
    failed_synthesis["synthesis"]["output"] = {"unexpected": True}
    with pytest.raises(jsonschema.ValidationError):
        report_schema.validate(failed_synthesis)


@pytest.mark.parametrize(
    "scenario_indexes",
    [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)],
)
def test_trace_v11_accepts_every_ordered_nonempty_scenario_subset(
    scenario_indexes: tuple[int, ...],
) -> None:
    report_schema = validator(load("contracts/v1/reports/analysis-report.schema.json"))
    trace_ai = load("contracts/v1/examples/analysis-report.trace-ai.valid.json")
    trace_ai["scenario_reports"] = [
        trace_ai["scenario_reports"][index] for index in scenario_indexes
    ]
    report_schema.validate(trace_ai)


def test_trace_v11_completed_synthesis_requires_full_design_provenance() -> None:
    report_schema = validator(load("contracts/v1/reports/analysis-report.schema.json"))
    trace_ai = load("contracts/v1/examples/analysis-report.trace-ai.valid.json")
    trace_ai["synthesis"] = {
        "state": "completed",
        "output": load("contracts/v1/examples/synthesis-output.valid.json"),
        "synthesis_artifact_id": "88000000-0000-4000-8000-000000000001",
        "failure_code": None,
        "provenance": {
            "provider_protocol": "chat-completions-json-schema-v1",
            "provider_name": "approved-provider",
            "model": "approved-model",
            "prompt_template_version": "1.0.0",
            "prompt_template_sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "normalizer_version": "smartperfetto-normalizer-1",
            "report_worker_image_digest": "sha256:" + "1" * 64,
            "projection_artifact_id": "89000000-0000-4000-8000-000000000001",
            "projection_sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "generated_at": "2026-08-03T12:00:00Z",
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "generation": 1,
        },
    }
    report_schema.validate(trace_ai)


def test_public_synthesis_reference_limits_match_private_output() -> None:
    private_schema = validator(load("contracts/v1/ai/synthesis-output.schema.json"))
    private_output = load("contracts/v1/examples/synthesis-output.valid.json")
    references = [f"8a000000-0000-4000-8000-{index:012d}" for index in range(1, 22)]
    private_output["top_findings"][0]["evidence_ids"] = references
    with pytest.raises(jsonschema.ValidationError):
        private_schema.validate(private_output)

    report_schema = validator(load("contracts/v1/reports/analysis-report.schema.json"))
    trace_ai = load("contracts/v1/examples/analysis-report.trace-ai.valid.json")
    trace_ai["synthesis"] = {
        "state": "completed",
        "output": private_output,
        "synthesis_artifact_id": "88000000-0000-4000-8000-000000000001",
        "failure_code": None,
        "provenance": {
            "provider_protocol": "chat-completions-json-schema-v1",
            "provider_name": "approved-provider",
            "model": "approved-model",
            "prompt_template_version": "1.0.0",
            "prompt_template_sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "normalizer_version": "smartperfetto-normalizer-1",
            "report_worker_image_digest": "sha256:" + "1" * 64,
            "projection_artifact_id": "89000000-0000-4000-8000-000000000001",
            "projection_sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "generated_at": "2026-08-03T12:00:00Z",
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "generation": 1,
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        report_schema.validate(trace_ai)
