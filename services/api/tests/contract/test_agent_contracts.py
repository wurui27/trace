from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import jsonschema
import pytest

from perfpilot_agent.source_contracts import (
    SourceContractError,
    canonical_source_contract_bytes,
    validate_source_contract_semantics,
)

ROOT = Path(__file__).parents[4]

CONTRACT_EXAMPLES = (
    ("agents/registration-code-response.schema.json", "agent-registration-code.valid.json"),
    ("agents/registration-request.schema.json", "agent-registration-request.valid.json"),
    ("agents/registration-response.schema.json", "agent-registration-response.valid.json"),
    ("agents/heartbeat-request.schema.json", "agent-heartbeat.valid.json"),
    ("agents/device-list-response.schema.json", "agent-device-list.valid.json"),
    ("agents/task-poll-response.schema.json", "agent-task-poll.valid.json"),
    ("agents/task-snapshot.schema.json", "agent-task-snapshot.valid.json"),
    ("agents/execution-manifest.schema.json", "agent-execution-manifest.valid.json"),
    ("agents/source-task-snapshot.schema.json", "source-task-snapshot.valid.json"),
    ("agents/source-task-completion.schema.json", "source-task-completion.valid.json"),
)


@lru_cache
def validator(schema_name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "contracts/v1" / schema_name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def example(example_name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts/v1/examples" / example_name).read_text(encoding="utf-8")
    )


def _validate_agent_contract(schema_name: str, document: dict[str, object]) -> None:
    validator(schema_name).validate(document)
    try:
        validate_source_contract_semantics(schema_name, document)
    except SourceContractError as exc:
        raise jsonschema.ValidationError("source task semantics are invalid") from exc


def _sized_unified_diff(path: str, target_bytes: int) -> str:
    prefix = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "+"
    )
    suffix = "\n"
    fill_bytes = target_bytes - len((prefix + suffix).encode("utf-8"))
    assert fill_bytes >= 0
    return prefix + "界" * (fill_bytes // 3) + "x" * (fill_bytes % 3) + suffix


def _heartbeat_v11() -> dict[str, object]:
    heartbeat = example("agent-heartbeat.valid.json")
    heartbeat.update(
        {
            "schema_version": "1.1",
            "workspaces": [
                {
                    "workspace_id": "92000000-0000-4000-8000-000000000001",
                    "name": "Demo Android",
                    "state": "ready",
                    "git_branch": "main",
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 2,
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [
                        {
                            "profile_id": "94000000-0000-4000-8000-000000000001",
                            "name": "Android check",
                        }
                    ],
                }
            ],
        }
    )
    return heartbeat


def _patch_completion() -> dict[str, object]:
    completion = example("source-task-completion.valid.json")
    patch_completion = {key: value for key, value in completion.items() if key != "result"}
    patch_completion.update(
        {
            "task_type": "patch_verification",
            "state": "completed",
            "result": {
                "verification_state": "verified",
                "exit_code": 0,
                "duration_ms": 1200,
                "profile_id": "94000000-0000-4000-8000-000000000001",
                "patch_sha256": "d" * 64,
                "log_summary": "Validation passed.",
            },
        }
    )
    return patch_completion


@pytest.mark.parametrize(("schema_name", "example_name"), CONTRACT_EXAMPLES)
def test_agent_examples_are_valid_and_closed(
    schema_name: str,
    example_name: str,
) -> None:
    contract = validator(schema_name)
    payload = example(example_name)

    contract.validate(payload)
    with pytest.raises(jsonschema.ValidationError):
        contract.validate({**payload, "unexpected": True})


def test_device_list_contract_never_accepts_raw_serial() -> None:
    payload = example("agent-device-list.valid.json")
    devices = payload["devices"]
    assert isinstance(devices, list)
    device = devices[0]
    assert isinstance(device, dict)
    device["serial"] = "R3CN30SECRET"

    errors = list(validator("agents/device-list-response.schema.json").iter_errors(payload))

    assert [error.validator for error in errors] == ["additionalProperties"]
    assert [list(error.absolute_path) for error in errors] == [["devices", 0]]


def test_task_snapshot_binds_agent_device_execution_and_lease() -> None:
    payload = example("agent-task-snapshot.valid.json")

    assert set(payload) >= {
        "agent_id",
        "device_digest",
        "execution_id",
        "lease_version",
        "expires_at",
    }
    validator("agents/task-snapshot.schema.json").validate(payload)


def test_source_task_is_not_a_device_capture_task() -> None:
    source = example("source-task-snapshot.valid.json")
    validator("agents/source-task-snapshot.schema.json").validate(source)
    assert source["task_type"] == "source_context"
    assert "device_digest" not in source

    device = example("agent-task-snapshot.valid.json")
    del device["device_digest"]
    with pytest.raises(jsonschema.ValidationError):
        validator("agents/task-snapshot.schema.json").validate(device)


def test_source_task_snapshot_discriminator_and_bounds_are_strict() -> None:
    contract = validator("agents/source-task-snapshot.schema.json")
    source = example("source-task-snapshot.valid.json")
    patch = {
        key: value
        for key, value in source.items()
        if key not in {"finding_hints", "limits"}
    }
    patch.update(
        {
            "task_type": "patch_verification",
            "validation_profile_id": "94000000-0000-4000-8000-000000000001",
            "snapshot_id": "95000000-0000-4000-8000-000000000001",
            "snapshot_hash": "a" * 64,
            "fix_id": "96000000-0000-4000-8000-000000000001",
            "patch": "diff --git a/app/src/Main.kt b/app/src/Main.kt\n",
        }
    )
    contract.validate(patch)

    invalid_documents = (
        {**source, "schema_version": "1.1"},
        {**source, "task_type": "device"},
        {**source, "device_digest": "a" * 64},
        {**source, "workspace_id": "not-a-uuid"},
        {**source, "limits": {**source["limits"], "max_files": 13}},  # type: ignore[dict-item]
        {**patch, "snapshot_hash": "A" * 64},
        {**patch, "patch": "x" * 65_537},
        {**patch, "validation_profile_id": None},
    )
    for document in invalid_documents:
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(document)


@pytest.mark.parametrize("embedded", [False, True], ids=["standalone", "task-poll"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_findings", 2),
        ("max_findings", 4),
        ("max_files", 11),
        ("max_files", 13),
        ("max_bytes", 98_303),
        ("max_bytes", 98_305),
    ],
)
def test_source_task_limits_are_exact_protocol_constants(
    embedded: bool,
    field: str,
    value: int,
) -> None:
    snapshot = example("source-task-snapshot.valid.json")
    snapshot["limits"][field] = value  # type: ignore[index]
    if embedded:
        contract = validator("agents/task-poll-response.schema.json")
        document = {
            "schema_version": "1.1",
            "task_kind": "source",
            "lease_token": "opaque-lease-token-123",
            "snapshot": snapshot,
            "signature_b64": "A" * 86 + "==",
        }
    else:
        contract = validator("agents/source-task-snapshot.schema.json")
        document = snapshot

    with pytest.raises(jsonschema.ValidationError):
        contract.validate(document)


def test_source_completion_is_closed_bounded_and_state_discriminated() -> None:
    contract = validator("agents/source-task-completion.schema.json")
    completion = example("source-task-completion.valid.json")
    contract.validate(completion)
    assert len(
        json.dumps(
            completion,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) <= 128 * 1024
    assert sum(
        len(fragment["content"].encode("utf-8"))
        for fragment in completion["result"]["fragments"]  # type: ignore[index,union-attr]
    ) <= 98_304

    fragment = completion["result"]["fragments"][0]  # type: ignore[index]
    for relative_path in (
        "/private/Main.kt",
        "C:/private/Main.kt",
        "../Main.kt",
        "./Main.kt",
        "app\\Main.kt",
    ):
        invalid = deepcopy(completion)
        invalid["result"]["fragments"][0]["relative_path"] = relative_path  # type: ignore[index]
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(invalid)

    for mutation in (
        {**completion, "schema_version": "1.1"},
        {**completion, "device_id": "72000000-0000-4000-8000-000000000001"},
        {**completion, "signature_b64": "not-a-signature"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(mutation)

    invalid = deepcopy(completion)
    invalid["result"]["fragments"] = [deepcopy(fragment) for _ in range(13)]  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)

    patch_completion = _patch_completion()
    contract.validate(patch_completion)
    failed_patch = deepcopy(patch_completion)
    failed_patch["state"] = "failed"
    failed_patch["result"].update(
        {
            "verification_state": "validation_failed",
            "exit_code": 1,
            "log_summary": "Validation failed.",
        }
    )
    contract.validate(failed_patch)
    invalid = deepcopy(patch_completion)
    invalid["result"]["verification_state"] = "validation_failed"
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)
    invalid = deepcopy(completion)
    invalid["result"]["full_log"] = "private output"
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)

def test_source_completion_terminal_state_matrix_is_explicit() -> None:
    contract = validator("agents/source-task-completion.schema.json")
    completion = example("source-task-completion.valid.json")
    failure_result = {"failure_code": "source_unavailable", "retryable": False}
    for state in ("failed", "canceled", "expired"):
        terminal = deepcopy(completion)
        terminal["state"] = state
        terminal["result"] = failure_result
        contract.validate(terminal)

    for state, verification_state in (
        ("canceled", "canceled"),
        ("expired", "timeout"),
    ):
        terminal = _patch_completion()
        terminal["state"] = state
        terminal["result"].update(  # type: ignore[union-attr]
            {
                "verification_state": verification_state,
                "exit_code": None,
                "duration_ms": None,
                "log_summary": None,
            }
        )
        contract.validate(terminal)

    for invalid_state in ("queued", "running"):
        invalid = deepcopy(completion)
        invalid["state"] = invalid_state
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(invalid)

    invalid = _patch_completion()
    invalid["state"] = "expired"
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)


def test_source_completion_enforces_fragment_utf8_aggregate_budget() -> None:
    completion = example("source-task-completion.valid.json")
    fragment = completion["result"]["fragments"][0]  # type: ignore[index]

    exact_unicode = deepcopy(completion)
    exact_unicode["result"]["fragments"][0]["content"] = "界" * 32_768  # type: ignore[index]
    _validate_agent_contract("agents/source-task-completion.schema.json", exact_unicode)

    aggregate_over = deepcopy(completion)
    aggregate_over["result"]["fragments"] = [  # type: ignore[index]
        {
            **deepcopy(fragment),
            "source_ref_id": f"97000000-0000-4000-8000-{index:012d}",
            "content": "界" * 16_385,
        }
        for index in (1, 2)
    ]
    assert len(canonical_source_contract_bytes(aggregate_over)) <= 128 * 1024
    with pytest.raises(jsonschema.ValidationError):
        _validate_agent_contract("agents/source-task-completion.schema.json", aggregate_over)


def test_source_completion_enforces_canonical_json_budget() -> None:
    completion = example("source-task-completion.valid.json")
    canonical_near = deepcopy(completion)
    canonical_near["result"]["fragments"][0]["content"] = "x" * 70_000  # type: ignore[index]
    canonical_near["result"]["exclusions"] = [  # type: ignore[index]
        {
            "relative_path": f"src/{index:02d}/" + "x" * 800 + ".kt",
            "reason_code": "excluded_file",
        }
        for index in range(64)
    ]
    assert (
        120 * 1024
        < len(canonical_source_contract_bytes(canonical_near))
        <= 128 * 1024
    )
    _validate_agent_contract("agents/source-task-completion.schema.json", canonical_near)

    canonical_over = deepcopy(completion)
    canonical_over["result"]["fragments"][0]["content"] = "x" * 70_000  # type: ignore[index]
    canonical_over["result"]["exclusions"] = [  # type: ignore[index]
        {
            "relative_path": f"src/{index:02d}/" + "x" * 940 + ".kt",
            "reason_code": "excluded_file",
        }
        for index in range(64)
    ]
    assert sum(
        len(item["content"].encode("utf-8"))
        for item in canonical_over["result"]["fragments"]  # type: ignore[index]
    ) <= 98_304
    assert len(canonical_source_contract_bytes(canonical_over)) > 128 * 1024
    with pytest.raises(jsonschema.ValidationError):
        _validate_agent_contract("agents/source-task-completion.schema.json", canonical_over)


@pytest.mark.parametrize("embedded", [False, True], ids=["standalone", "task-poll"])
def test_patch_task_enforces_utf8_bytes_not_character_count(embedded: bool) -> None:
    path = "app/src/main/java/demo/MainActivity.kt"
    snapshot = example("source-task-snapshot.valid.json")
    snapshot = {
        key: value for key, value in snapshot.items() if key not in {"finding_hints", "limits"}
    }
    snapshot.update(
        {
            "task_type": "patch_verification",
            "validation_profile_id": "94000000-0000-4000-8000-000000000001",
            "snapshot_id": "95000000-0000-4000-8000-000000000001",
            "snapshot_hash": "a" * 64,
            "fix_id": "96000000-0000-4000-8000-000000000001",
            "patch": _sized_unified_diff(path, 65_536),
        }
    )
    if embedded:
        schema_name = "agents/task-poll-response.schema.json"
        document = {
            "schema_version": "1.1",
            "task_kind": "source",
            "lease_token": "opaque-lease-token-123",
            "snapshot": snapshot,
            "signature_b64": "A" * 86 + "==",
        }
    else:
        schema_name = "agents/source-task-snapshot.schema.json"
        document = snapshot
    assert len(snapshot["patch"]) <= 65_536  # type: ignore[arg-type]
    _validate_agent_contract(schema_name, document)

    snapshot["patch"] = _sized_unified_diff(path, 65_537)
    assert len(snapshot["patch"]) <= 65_536  # type: ignore[arg-type]
    with pytest.raises(jsonschema.ValidationError):
        _validate_agent_contract(schema_name, document)


def test_source_contract_semantic_failures_are_redacted() -> None:
    snapshot = example("source-task-snapshot.valid.json")
    snapshot = {
        key: value for key, value in snapshot.items() if key not in {"finding_hints", "limits"}
    }
    snapshot.update(
        {
            "task_type": "patch_verification",
            "validation_profile_id": "94000000-0000-4000-8000-000000000001",
            "snapshot_id": "95000000-0000-4000-8000-000000000001",
            "snapshot_hash": "a" * 64,
            "fix_id": "96000000-0000-4000-8000-000000000001",
            "patch": "private-token" + "界" * 21_846,
        }
    )
    with pytest.raises(SourceContractError) as exc_info:
        validate_source_contract_semantics(
            "agents/source-task-snapshot.schema.json",
            snapshot,
        )
    assert str(exc_info.value) == "source task contract is invalid"
    assert "private-token" not in str(exc_info.value)


def test_heartbeat_v11_workspaces_are_public_bounded_metadata() -> None:
    contract = validator("agents/heartbeat-request.schema.json")
    heartbeat = _heartbeat_v11()
    contract.validate(heartbeat)

    legacy = example("agent-heartbeat.valid.json")
    contract.validate(legacy)
    with pytest.raises(jsonschema.ValidationError):
        contract.validate({**legacy, "workspaces": []})

    workspace = heartbeat["workspaces"][0]  # type: ignore[index]
    for key, value in (
        ("path", "/private/repo"),
        ("remote", "git@example/repo"),
        ("argv", ["./gradlew"]),
    ):
        invalid = deepcopy(heartbeat)
        invalid["workspaces"][0][key] = value  # type: ignore[index]
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(invalid)
    invalid = deepcopy(heartbeat)
    invalid["workspaces"] = [deepcopy(workspace) for _ in range(33)]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid)


def test_heartbeat_workspace_and_profile_caps_closure_and_privacy() -> None:
    contract = validator("agents/heartbeat-request.schema.json")
    heartbeat = _heartbeat_v11()
    workspace = heartbeat["workspaces"][0]  # type: ignore[index]
    profile = workspace["validation_profiles"][0]

    too_many_workspaces = deepcopy(heartbeat)
    too_many_workspaces["workspaces"] = [
        {
            **deepcopy(workspace),
            "workspace_id": f"92000000-0000-4000-8000-{index:012d}",
        }
        for index in range(1, 34)
    ]
    errors = list(contract.iter_errors(too_many_workspaces))
    assert "maxItems" in {error.validator for error in errors}

    too_many_profiles = deepcopy(heartbeat)
    too_many_profiles["workspaces"][0]["validation_profiles"] = [  # type: ignore[index]
        {
            **deepcopy(profile),
            "profile_id": f"94000000-0000-4000-8000-{index:012d}",
        }
        for index in range(1, 10)
    ]
    errors = list(contract.iter_errors(too_many_profiles))
    assert "maxItems" in {error.validator for error in errors}

    for target, field, value in (
        ("workspace", "path", "/private/repo"),
        ("workspace", "argv", ["./gradlew", "test"]),
        ("workspace", "unexpected", True),
        ("profile", "path", "/private/repo"),
        ("profile", "argv", ["./gradlew", "test"]),
        ("profile", "unexpected", True),
    ):
        invalid = deepcopy(heartbeat)
        selected = invalid["workspaces"][0]  # type: ignore[index]
        if target == "profile":
            selected = selected["validation_profiles"][0]
        selected[field] = value
        with pytest.raises(jsonschema.ValidationError):
            contract.validate(invalid)

    for schema_version in ("1.2", "2.0"):
        with pytest.raises(jsonschema.ValidationError):
            contract.validate({**heartbeat, "schema_version": schema_version})


def test_task_poll_v11_discriminates_device_and_source_snapshots() -> None:
    contract = validator("agents/task-poll-response.schema.json")
    common = {
        "schema_version": "1.1",
        "lease_token": "opaque-lease-token-123",
        "signature_b64": "A" * 86 + "==",
    }
    source = {
        **common,
        "task_kind": "source",
        "snapshot": example("source-task-snapshot.valid.json"),
    }
    device = {
        **common,
        "task_kind": "device",
        "snapshot": example("agent-task-snapshot.valid.json"),
    }
    contract.validate(source)
    contract.validate(device)

    invalid_device = deepcopy(device)
    del invalid_device["snapshot"]["device_digest"]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid_device)
    with pytest.raises(jsonschema.ValidationError):
        contract.validate({**source, "task_kind": "device"})

    invalid_device = deepcopy(device)
    del invalid_device["snapshot"]["input_artifacts"][0]["artifact_id"]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid_device)
    invalid_source = deepcopy(source)
    invalid_source["snapshot"]["finding_hints"] = [
        {
            "finding_id": "85000000-0000-4000-8000-000000000001",
            "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
            "rule_id": "startup.main_thread_binder",
            "symbol_hints": ["demo.MainActivity.onCreate"],
            "path": "/private/repo",
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        contract.validate(invalid_source)


def test_heartbeat_accepts_no_more_than_32_devices() -> None:
    payload = example("agent-heartbeat.valid.json")
    devices = payload["devices"]
    assert isinstance(devices, list)
    payload["devices"] = [deepcopy(devices[0]) for _ in range(33)]

    errors = list(validator("agents/heartbeat-request.schema.json").iter_errors(payload))

    assert "maxItems" in {error.validator for error in errors}


def test_execution_manifest_accepts_no_more_than_32_artifacts() -> None:
    payload = example("agent-execution-manifest.valid.json")
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    payload["artifacts"] = [deepcopy(artifacts[0]) for _ in range(33)]

    errors = list(validator("agents/execution-manifest.schema.json").iter_errors(payload))

    assert "maxItems" in {error.validator for error in errors}


def test_browser_device_example_contains_no_private_transport_fields() -> None:
    payload = example("agent-device-list.valid.json")
    forbidden = {
        "serial",
        "serial_digest",
        "token",
        "token_digest",
        "public_key_b64",
        "object_key",
        "signed_url",
    }

    pending: list[object] = [payload]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            assert forbidden.isdisjoint(current)
            pending.extend(current.values())
