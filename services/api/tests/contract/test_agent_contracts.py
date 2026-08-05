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
