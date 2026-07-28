from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource


ROOT = Path(__file__).parents[4]
SCHEMA_PATHS = (
    "contracts/v1/analyses/create-request.schema.json",
    "contracts/v1/analyses/analysis-response.schema.json",
    "contracts/v1/analyses/scenario-execution-manifest.schema.json",
    "contracts/v1/reports/analysis-bundle.schema.json",
    "contracts/v1/reports/analysis-report.schema.json",
)


def _load(relative: str) -> dict[str, object]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _schemas() -> dict[str, dict[str, object]]:
    return {path: _load(path) for path in SCHEMA_PATHS}


def _registry(schemas: dict[str, dict[str, object]]) -> Registry:
    return Registry().with_resources(
        (str(schema["$id"]), Resource.from_contents(schema)) for schema in schemas.values()
    )


def _validator(
    relative: str,
    schemas: dict[str, dict[str, object]],
) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schemas[relative],
        registry=_registry(schemas),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _walk_schema(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


def _sha(character: str = "A") -> str:
    return character * 43 + "="


def _pending_analysis_response(*, include_authorization: bool) -> dict[str, object]:
    upload: dict[str, object] = {
        "state": "pending",
        "upload_id": "41000000-0000-4000-8000-000000000001",
        "artifact_kind": "apk",
        "mime": "application/vnd.android.package-archive",
        "size": 3,
        "sha256_b64": _sha(),
        "expires_at": "2026-07-28T12:15:00Z",
    }
    if include_authorization:
        upload.update(
            {
                "put_url": "https://objects.example/upload-token",
                "required_headers": {
                    "Content-Type": "application/vnd.android.package-archive",
                    "x-amz-checksum-sha256": _sha(),
                },
            }
        )

    scenario_types = ("cold_start", "scroll", "memory_cycle")
    return {
        "schema_version": "1.0",
        "analysis_id": "31000000-0000-4000-8000-000000000001",
        "team_id": "21000000-0000-4000-8000-000000000001",
        "analysis_mode": "device",
        "state": "created",
        "version": 1,
        "application_version_id": None,
        "application_metadata": None,
        "apk_upload": upload,
        "scenarios": [
            {
                "scenario_job_id": None,
                "scenario_type": scenario_type,
                "state": "awaiting_input",
                "version": None,
                "device_group_id": None,
                "sample_verdict_counts": {
                    "valid": 0,
                    "invalid": 0,
                    "pending": 0,
                    "validation_error": 0,
                    "total": 0,
                },
                "started_at": None,
                "completed_at": None,
                "failure": None,
            }
            for scenario_type in scenario_types
        ],
        "sample_verdict_counts": {
            "valid": 0,
            "invalid": 0,
            "pending": 0,
            "validation_error": 0,
            "total": 0,
        },
        "active_lease": None,
        "report_available": False,
        "created_at": "2026-07-28T12:00:00Z",
        "started_at": None,
        "completed_at": None,
        "failure": None,
    }


def test_analysis_contract_schemas_are_valid_and_close_declared_objects() -> None:
    schemas = _schemas()

    for schema in schemas.values():
        jsonschema.Draft202012Validator.check_schema(schema)
        for node in _walk_schema(schema):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False


def test_device_create_request_has_one_server_parsed_apk_and_fixed_scenario_order() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/create-request.schema.json", schemas)
    payload = {
        "schema_version": "1.0",
        "analysis_mode": "device",
        "scenarios": ["cold_start", "scroll", "memory_cycle"],
        "apk": {
            "artifact_kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 3,
            "sha256_b64": _sha(),
        },
    }
    validator.validate(payload)

    for mutation in (
        {**payload, "application_version_id": "31000000-0000-4000-8000-000000000099"},
        {**payload, "scenarios": ["scroll", "cold_start", "memory_cycle"]},
        {
            **payload,
            "apk": {**payload["apk"], "package_name": "client.claimed.package"},
        },
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_pending_analysis_query_may_omit_but_not_split_upload_authorization() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/analysis-response.schema.json", schemas)
    validator.validate(_pending_analysis_response(include_authorization=False))
    validator.validate(_pending_analysis_response(include_authorization=True))

    missing_headers = _pending_analysis_response(include_authorization=True)
    del missing_headers["apk_upload"]["required_headers"]  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(missing_headers)


def test_scenario_execution_manifest_never_claims_server_sample_validity() -> None:
    schemas = _schemas()
    validator = _validator(
        "contracts/v1/analyses/scenario-execution-manifest.schema.json",
        schemas,
    )
    payload = {
        "schema_version": "1.0",
        "analysis_id": "31000000-0000-4000-8000-000000000001",
        "scenario_job_id": "32000000-0000-4000-8000-000000000001",
        "lease_id": "33000000-0000-4000-8000-000000000001",
        "scenario_type": "cold_start",
        "expected_version": 4,
        "sample_attempt_ids": ["34000000-0000-4000-8000-000000000001"],
        "bundle_id": None,
        "total_attempts": 1,
        "stop_reason": "deterministic_error",
        "local_diagnostics": [
            {
                "code": "target_process_missing",
                "severity": "error",
                "message": "Target process did not start.",
            }
        ],
        "submitted_at": "2026-07-28T12:30:00Z",
    }
    validator.validate(payload)

    payload["server_valid_samples"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(payload)


def test_partial_report_keeps_ordered_successful_siblings_and_full_provenance() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    validator.validate(report)

    assert [item["scenario_type"] for item in report["scenario_reports"]] == [
        "cold_start",
        "scroll",
        "memory_cycle",
    ]
    assert [item["result_state"] for item in report["scenario_reports"]] == [
        "completed",
        "failed",
        "completed",
    ]
    assert report["scenario_reports"][0]["bundle"] is not None
    assert report["scenario_reports"][2]["bundle"] is not None
    for item in report["scenario_reports"]:
        bundle = item["bundle"]
        if bundle is not None:
            assert bundle["trace_health"]
            assert bundle["trace_capabilities"]
            assert bundle["provenance"]


def test_partial_report_contract_validates_offline_without_remote_resolution() -> None:
    schema = _load("contracts/v1/reports/analysis-report.schema.json")
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")

    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    ).validate(report)


@pytest.mark.parametrize("result_state", ["failed", "canceled"])
def test_failed_or_canceled_report_requires_failure_or_partial_bundle(
    result_state: str,
) -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    scenario = report["scenario_reports"][1]
    scenario["result_state"] = result_state
    scenario["bundle"] = None
    scenario["failure"] = None

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


def test_completed_report_requires_a_complete_bundle() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    completed = report["scenario_reports"][0]
    completed["bundle"] = None

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)

    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    completed = report["scenario_reports"][0]
    completed["bundle"]["bundle_state"] = "partial"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


@pytest.mark.parametrize("forbidden", ["database_url", "bucket", "object_key", "download_url"])
def test_report_contract_rejects_storage_and_tenant_internals(forbidden: str) -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    report["scenario_reports"][0]["bundle"]["artifacts"][0][forbidden] = "secret"

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


def test_report_rejects_reordered_device_scenarios() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    report["scenario_reports"][0], report["scenario_reports"][1] = (
        report["scenario_reports"][1],
        report["scenario_reports"][0],
    )

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


def test_report_version_is_a_javascript_safe_integer() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    report["report_version"] = 9_007_199_254_740_992

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


@pytest.mark.parametrize("parent_state", ["failed", "canceled"])
def test_terminal_parent_state_must_match_its_three_children(parent_state: str) -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/reports/analysis-report.schema.json", schemas)
    report = _load("contracts/v1/examples/analysis-report.partial.valid.json")
    report["state"] = parent_state

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)
