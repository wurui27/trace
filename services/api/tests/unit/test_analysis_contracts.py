from __future__ import annotations

import json
import subprocess
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


def _ecmascript_pattern_matches(pattern: str, value: str) -> bool:
    script = (
        "const [pattern, value] = process.argv.slice(1); "
        'process.stdout.write(String(new RegExp(pattern, "u").test(value)));'
    )
    completed = subprocess.run(
        ["node", "-e", script, pattern, value],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout == "true"


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
        "device_id": "72000000-0000-4000-8000-000000000001",
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


def _remote_device_analysis_response() -> dict[str, object]:
    payload = _pending_analysis_response(include_authorization=False)
    payload["schema_version"] = "1.1"
    payload["source_code_analysis"] = {
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
    memory = payload["scenarios"][2]  # type: ignore[index]
    memory.update(  # type: ignore[union-attr]
        {
            "state": "not_requested",
            "scenario_job_id": None,
            "version": None,
            "device_group_id": None,
            "started_at": None,
            "completed_at": None,
            "failure": None,
        }
    )
    return payload


def _script_device_analysis_response() -> dict[str, object]:
    payload = {
        key: value
        for key, value in _remote_device_analysis_response().items()
        if key != "apk_upload"
    }
    payload.update(
        {
            "schema_version": "1.2",
            "state": "queued",
            "version": 2,
            "capture_configuration": {
                "test_type": "cold_start",
                "launch_mode": "automatic",
                "duration_seconds": 15,
                "target": {
                    "package_name": "com.rivotek.mediacenter",
                    "launch_activity": (
                        "com.rivotek.mediacenter/.shell.MediaCenterActivity"
                    ),
                },
            },
            "scenarios": [
                {
                    "scenario_job_id": None,
                    "scenario_type": "cold_start",
                    "state": "queued",
                    "version": 2,
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
            ],
        }
    )
    return payload


def _memory_analysis_response(*, question: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "analysis_id": "31000000-0000-4000-8000-000000000001",
        "team_id": "21000000-0000-4000-8000-000000000001",
        "analysis_mode": "memory_upload",
        "state": "created",
        "version": 2,
        "application_version_id": "71000000-0000-4000-8000-000000000001",
        "application_metadata": {
            "package_name": "dev.perfpilot.memory",
            "version_name": "1.0",
            "version_code": 1,
            "launch_activity": "dev.perfpilot.memory.MainActivity",
            "min_sdk": 28,
            "target_sdk": 35,
            "supported_abis": ["arm64-v8a"],
            "has_native_libraries": False,
        },
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
        "created_at": "2026-07-28T12:00:00Z",
        "started_at": None,
        "completed_at": None,
        "failure": None,
        "question": question,
    }


def _trace_analysis_response(*, input_state: str) -> dict[str, object]:
    input_upload: dict[str, object] = {
        "state": input_state,
        "artifact_kind": "trace",
        "mime": "application/octet-stream",
        "size": 4_096,
        "sha256_b64": _sha(),
    }
    if input_state == "pending":
        input_upload.update(
            {
                "upload_id": "41000000-0000-4000-8000-000000000001",
                "expires_at": "2026-07-28T12:15:00Z",
            }
        )
    return {
        "schema_version": "1.0",
        "analysis_id": "31000000-0000-4000-8000-000000000001",
        "team_id": "21000000-0000-4000-8000-000000000001",
        "analysis_mode": "trace_upload",
        "state": "uploading" if input_state == "pending" else "created",
        "version": 2,
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
        "created_at": "2026-07-28T12:00:00Z",
        "started_at": None,
        "completed_at": None,
        "failure": None,
        "analysis_profile": "auto",
        "test_type": "other",
        "package_name": "com.rivotek.mediacenter",
        "custom_test_name": "自定义链路",
        "custom_test_description": "验证自定义业务链路性能。",
        "question": "为什么滑动卡顿？",
        "input_uploads": [input_upload],
        "stages": [
            {
                "stage": "input_validation",
                "state": "running" if input_state == "pending" else "pending",
                "failure": None,
            },
            {"stage": "smartperfetto", "state": "pending", "failure": None},
            {"stage": "perfpilot_ai", "state": "pending", "failure": None},
            {"stage": "report", "state": "pending", "failure": None},
        ],
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
        "device_id": "72000000-0000-4000-8000-000000000001",
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


def test_script_device_create_request_is_closed_without_apk_upload() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/create-request.schema.json", schemas)
    target = {
        "package_name": "com.rivotek.mediacenter",
        "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
    }
    automatic = {
        "schema_version": "1.2",
        "analysis_mode": "device",
        "device_id": "72000000-0000-4000-8000-000000000001",
        "test_type": "cold_start",
        "launch_mode": "automatic",
        "duration_seconds": 15,
        "target": target,
    }
    validator.validate(automatic)
    validator.validate(
        {
            **automatic,
            "test_type": "hot_start",
            "launch_mode": "manual",
            "target": None,
        }
    )
    validator.validate(
        {
            **automatic,
            "test_type": "scroll",
            "launch_mode": "manual",
        }
    )

    for mutation in (
        {**automatic, "apk": {}},
        {**automatic, "scenarios": ["cold_start"]},
        {**automatic, "duration_seconds": 0},
        {**automatic, "launch_mode": "manual"},
        {**automatic, "test_type": "scroll", "launch_mode": "automatic"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_memory_create_request_is_closed_without_raw_question_length_limit() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/create-request.schema.json", schemas)
    payload = {
        "schema_version": "1.0",
        "analysis_mode": "memory_upload",
        "application_version_id": "71000000-0000-4000-8000-000000000001",
        "question": " " * 2_001,
    }
    validator.validate(payload)
    validator.validate({key: value for key, value in payload.items() if key != "question"})
    validator.validate({**payload, "question": None})
    validator.validate({**payload, "question": " \n" + "x" * 2_000 + "\t "})

    for mutation in (
        {**payload, "apk": {}},
        {**payload, "scenarios": []},
        {**payload, "unexpected": True},
        {**payload, "question": " \n" + "x" * 2_001 + "\t "},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_trace_create_request_is_package_targeted_and_trace_only() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/create-request.schema.json", schemas)
    payload = {
        "schema_version": "1.0",
        "analysis_mode": "trace_upload",
        "test_type": "cold_start",
        "package_name": "com.rivotek.mediacenter",
        "question": "为什么滑动卡顿？",
        "inputs": [
            {
                "kind": "trace",
                "mime": "application/octet-stream",
                "size": 4_096,
                "sha256_b64": _sha(),
            }
        ],
    }

    validator.validate(payload)
    validator.validate({**payload, "test_type": "hot_start", "question": None})
    validator.validate({**payload, "test_type": "scroll"})
    validator.validate({
        **payload,
        "test_type": "other",
        "custom_test_name": "首页首帧",
        "custom_test_description": "进入首页后等待首帧稳定，用于检查自定义业务链路。",
    })

    invalid_payloads = (
        {**payload, "test_type": "auto"},
        {**payload, "package_name": ""},
        {**payload, "package_name": "../app"},
        {**payload, "package_name": "com.demo.app;rm"},
        {**payload, "inputs": []},
        {**payload, "inputs": [payload["inputs"][0], payload["inputs"][0]]},
        {
            **payload,
            "inputs": [
                *payload["inputs"],
                {
                    "kind": "mapping",
                    "mime": "text/plain",
                    "size": 2_048,
                    "sha256_b64": _sha(),
                },
            ],
        },
        {
            **payload,
            "inputs": [
                {
                    **payload["inputs"][0],
                    "kind": "unknown",
                }
            ],
        },
        {
            **payload,
            "inputs": [
                {
                    **payload["inputs"][0],
                    "sha256_b64": "not-a-checksum",
                }
            ],
        },
        {**payload, "test_type": "other"},
        {**payload, "test_type": "other", "custom_test_name": "首页首帧"},
        {**payload, "custom_test_name": "不应出现", "custom_test_description": "不应出现"},
        {**payload, "unexpected": True},
    )
    for mutation in invalid_payloads:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_memory_question_whitespace_matches_python_strip_across_regex_runtimes() -> None:
    schemas = _schemas()
    schema = schemas["contracts/v1/analyses/create-request.schema.json"]
    memory_branch = next(  # type: ignore[union-attr]
        branch
        for branch in schema["oneOf"]  # type: ignore[index]
        if branch["properties"]["analysis_mode"].get("const") == "memory_upload"
    )
    pattern = memory_branch["properties"]["question"]["oneOf"][0]["pattern"]  # type: ignore[index]
    assert isinstance(pattern, str)
    validator = _validator("contracts/v1/analyses/create-request.schema.json", schemas)
    payload = {
        "schema_version": "1.0",
        "analysis_mode": "memory_upload",
        "application_version_id": "71000000-0000-4000-8000-000000000001",
    }
    byte_order_marks = "\ufeff" * 2_001
    information_separators = "\u001c" * 2_001

    assert byte_order_marks.strip() == byte_order_marks
    assert information_separators.strip() == ""
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**payload, "question": byte_order_marks})
    validator.validate({**payload, "question": information_separators})
    assert not _ecmascript_pattern_matches(pattern, byte_order_marks)
    assert _ecmascript_pattern_matches(pattern, information_separators)


def test_pending_analysis_query_may_omit_but_not_split_upload_authorization() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/analysis-response.schema.json", schemas)
    validator.validate(_pending_analysis_response(include_authorization=False))
    validator.validate(_pending_analysis_response(include_authorization=True))

    missing_headers = _pending_analysis_response(include_authorization=True)
    del missing_headers["apk_upload"]["required_headers"]  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(missing_headers)

    for mutation in (
        {**_pending_analysis_response(include_authorization=False), "question": None},
        {**_pending_analysis_response(include_authorization=False), "apk_upload": None},
        {**_pending_analysis_response(include_authorization=False), "scenarios": []},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_only_device_1_1_memory_cycle_may_be_not_requested() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/analysis-response.schema.json", schemas)
    payload = _remote_device_analysis_response()
    validator.validate(payload)

    for scenario in payload["scenarios"][:2]:  # type: ignore[index]
        assert scenario["state"] == "awaiting_input"  # type: ignore[index]
    memory = payload["scenarios"][2]  # type: ignore[index]
    assert memory["state"] == "not_requested"  # type: ignore[index]
    assert memory["sample_verdict_counts"]["total"] == 0  # type: ignore[index]
    assert memory["failure"] is None  # type: ignore[index]

    legacy = _pending_analysis_response(include_authorization=False)
    legacy["scenarios"][2]["state"] = "not_requested"  # type: ignore[index]
    wrong_active = _remote_device_analysis_response()
    wrong_active["scenarios"][0]["state"] = "not_requested"  # type: ignore[index]
    private_memory = _remote_device_analysis_response()
    private_memory["scenarios"][2]["progress"] = 0  # type: ignore[index]
    trace = _trace_analysis_response(input_state="pending")
    trace["scenarios"] = [payload["scenarios"][2]]  # type: ignore[index]
    for mutation in (legacy, wrong_active, private_memory, trace):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_script_device_analysis_response_contains_one_requested_scenario() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/analysis-response.schema.json", schemas)
    payload = _script_device_analysis_response()
    validator.validate(payload)

    for mutation in (
        {**payload, "apk_upload": None},
        {**payload, "capture_configuration": None},
        {**payload, "scenarios": []},
        {
            **payload,
            "scenarios": [
                {
                    **payload["scenarios"][0],  # type: ignore[index]
                    "scenario_type": "scroll",
                }
            ],
        },
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_memory_analysis_response_requires_manual_zero_side_effect_invariants() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/analysis-response.schema.json", schemas)
    payload = _memory_analysis_response(question="retained objects")
    validator.validate(payload)
    validator.validate(_memory_analysis_response(question=None))

    for mutation in (
        {
            **payload,
            "apk_upload": _pending_analysis_response(include_authorization=False)["apk_upload"],
        },
        {**payload, "scenarios": [object()]},
        {**payload, "application_metadata": None},
        {**payload, "question": "x" * 2_001},
        {key: value for key, value in payload.items() if key != "question"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(mutation)


def test_trace_analysis_response_projects_declared_inputs_without_minting_urls() -> None:
    schemas = _schemas()
    validator = _validator("contracts/v1/analyses/analysis-response.schema.json", schemas)

    validator.validate(_trace_analysis_response(input_state="awaiting_upload"))
    validator.validate(_trace_analysis_response(input_state="pending"))

    unexpected_url = _trace_analysis_response(input_state="pending")
    unexpected_url["input_uploads"][0]["put_url"] = "https://objects.example/secret"  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(unexpected_url)

    wrong_order = _trace_analysis_response(input_state="pending")
    wrong_order["stages"] = list(reversed(wrong_order["stages"]))  # type: ignore[arg-type]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(wrong_order)

    missing_stages = _trace_analysis_response(input_state="pending")
    del missing_stages["stages"]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(missing_stages)


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
