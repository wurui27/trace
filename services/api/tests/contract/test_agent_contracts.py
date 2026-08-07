from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import jsonschema
import pytest

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

    patch_completion = {
        key: value
        for key, value in completion.items()
        if key != "result"
    }
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

    oversized = deepcopy(completion)
    oversized["result"]["fragments"] = [
        {
            **deepcopy(fragment),
            "source_ref_id": f"97000000-0000-4000-8000-{index:012d}",
            "content": character * 70_000,
        }
        for index, character in ((2, "x"), (3, "y"))
    ]
    with pytest.raises(AssertionError):
        assert len(
            json.dumps(
                oversized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ) <= 128 * 1024


def test_heartbeat_v11_workspaces_are_public_bounded_metadata() -> None:
    contract = validator("agents/heartbeat-request.schema.json")
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
