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


def test_trace_v11_rejects_uppercase_synthesis_artifact_id() -> None:
    report_schema = validator(load("contracts/v1/reports/analysis-report.schema.json"))
    trace_ai = load("contracts/v1/examples/analysis-report.trace-ai.valid.json")
    trace_ai["synthesis"]["synthesis_artifact_id"] = "AA000000-0000-4000-8000-000000000001"
    trace_ai["synthesis"]["state"] = "completed"
    trace_ai["synthesis"]["failure_code"] = None
    trace_ai["synthesis"]["output"] = load("contracts/v1/examples/synthesis-output.valid.json")
    trace_ai["synthesis"]["provenance"] = {
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
    }
    with pytest.raises(jsonschema.ValidationError):
        report_schema.validate(trace_ai)


def _source_binding() -> dict[str, object]:
    return {
        "provider_kind": "agent_workspace",
        "agent_id": "91000000-0000-4000-8000-000000000001",
        "workspace_id": "92000000-0000-4000-8000-000000000001",
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": "94000000-0000-4000-8000-000000000001",
    }


def _trace_create_request(schema_version: str) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "analysis_mode": "trace_upload",
        "analysis_profile": "auto",
        "question": None,
        "inputs": [
            {
                "kind": "trace",
                "mime": "application/octet-stream",
                "size": 4096,
                "sha256_b64": "A" * 43 + "=",
            }
        ],
    }


def test_create_request_v11_binds_source_without_weakening_v10_or_memory() -> None:
    contract = validator(load("contracts/v1/analyses/create-request.schema.json"))
    trace = {**_trace_create_request("1.1"), "source_binding": _source_binding()}
    contract.validate(trace)
    contract.validate(
        {
            "schema_version": "1.1",
            "analysis_mode": "device",
            "device_id": "72000000-0000-4000-8000-000000000001",
            "scenarios": ["cold_start", "scroll", "memory_cycle"],
            "apk": {
                "artifact_kind": "apk",
                "mime": "application/vnd.android.package-archive",
                "size": 4096,
                "sha256_b64": "A" * 43 + "=",
            },
            "source_binding": {
                **_source_binding(),
                "agent_id": "91000000-0000-4000-8000-000000000002",
                "validation_profile_id": None,
            },
        }
    )

    with pytest.raises(jsonschema.ValidationError):
        contract.validate(
            {**_trace_create_request("1.0"), "source_binding": _source_binding()}
        )
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(
            {
                "schema_version": "1.0",
                "analysis_mode": "memory_upload",
                "application_version_id": "71000000-0000-4000-8000-000000000001",
                "source_binding": _source_binding(),
            }
        )

    for forbidden in ("path", "repo_url", "remote", "argv", "command"):
        invalid = deepcopy(trace)
        invalid["source_binding"][forbidden] = "private"  # type: ignore[index]
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(invalid)


def _analysis_response_v11() -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "analysis_id": "31000000-0000-4000-8000-000000000001",
        "team_id": "21000000-0000-4000-8000-000000000001",
        "analysis_mode": "trace_upload",
        "state": "analyzing",
        "version": 3,
        "application_version_id": None,
        "application_metadata": None,
        "apk_upload": None,
        "scenarios": [],
        "sample_verdict_counts": {
            "valid": 0,
            "invalid": 0,
            "pending": 0,
            "validation_error": 0,
            "total": 0,
        },
        "active_lease": None,
        "report_available": False,
        "created_at": "2026-08-07T08:00:00Z",
        "started_at": "2026-08-07T08:01:00Z",
        "completed_at": None,
        "failure": None,
        "analysis_profile": "auto",
        "question": None,
        "input_uploads": [
            {
                "state": "awaiting_upload",
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": 4096,
                "sha256_b64": "A" * 43 + "=",
            }
        ],
        "stages": [
            {"stage": "input_validation", "state": "completed", "failure": None},
            {"stage": "smartperfetto", "state": "completed", "failure": None},
            {"stage": "perfpilot_ai", "state": "running", "failure": None},
            {"stage": "report", "state": "pending", "failure": None},
        ],
        "source_analysis": {
            "engine": "smartperfetto",
            "rounds": 53,
            "verification": "passed",
            "session_id": "session-local-1",
            "run_id": "run-local-1",
        },
        "source_code_analysis": {
            "requested": True,
            **_source_binding(),
            "context_state": "available",
            "match_summary": "strong",
            "verification_state": "verified",
            "failure_code": None,
        },
    }


def test_analysis_response_v11_separates_smartperfetto_and_source_code() -> None:
    contract = validator(load("contracts/v1/analyses/analysis-response.schema.json"))
    response = _analysis_response_v11()
    contract.validate(response)

    not_requested = deepcopy(response)
    not_requested["source_code_analysis"] = {
        "requested": False,
        "provider_kind": None,
        "agent_id": None,
        "workspace_id": None,
        "snapshot_policy": None,
        "validation_profile_id": None,
        "context_state": "not_requested",
        "match_summary": "none",
        "verification_state": "not_requested",
        "failure_code": None,
    }
    contract.validate(not_requested)

    invalid = deepcopy(response)
    invalid["source_code_analysis"]["path"] = "/private/repo"  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)
    invalid = deepcopy(not_requested)
    invalid["source_code_analysis"]["context_state"] = "available"  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)
    invalid = deepcopy(response)
    invalid["stages"] = list(reversed(invalid["stages"]))  # type: ignore[arg-type]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)

    legacy = deepcopy(response)
    legacy["schema_version"] = "1.0"
    del legacy["source_analysis"]
    del legacy["source_code_analysis"]
    contract.validate(legacy)
    with pytest.raises(jsonschema.ValidationError):
        contract.validate({**legacy, "source_code_analysis": not_requested})
