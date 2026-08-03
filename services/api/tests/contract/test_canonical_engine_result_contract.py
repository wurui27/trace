from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid5

import jsonschema
import pytest


ROOT = Path(__file__).parents[4]
SCHEMA_PATH = ROOT / "contracts/v1/engines/canonical-engine-result.schema.json"
EXAMPLE_PATHS = {
    "smartperfetto": ROOT
    / "contracts/v1/examples/canonical-engine-result.smartperfetto.valid.json",
    "android_memory": ROOT
    / "contracts/v1/examples/canonical-engine-result.android-memory.valid.json",
}
RESULT_NAMESPACE = UUID("a1c50ce0-6144-553e-8721-18f466991f32")


def _load(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        _load(SCHEMA_PATH),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _example(engine_id: str) -> dict[str, object]:
    return _load(EXAMPLE_PATHS[engine_id])


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def test_schema_is_valid_draft_2020_12() -> None:
    jsonschema.Draft202012Validator.check_schema(_load(SCHEMA_PATH))


@pytest.mark.parametrize("field", ["artifact_id", "analysis_id", "execution_id"])
def test_uuid_constraints_do_not_depend_on_optional_format_checker(
    field: str,
) -> None:
    payload = deepcopy(_example("smartperfetto"))
    payload[field] = "not-a-uuid"
    validator = jsonschema.Draft202012Validator(_load(SCHEMA_PATH))

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "artifact_id", "c79e45ad-fcb4-5b16-a327-f3aae70eebbc\n"),
        (None, "analysis_id", "00000000-0000-4000-8000-000000000001\n"),
        (None, "execution_id", "00000000-0000-4000-8000-000000000101\n"),
        ("engine", "adapter_version", "1.0.0\n"),
        ("engine", "source_commit_sha", "a" * 40 + "\n"),
        ("engine", "image_digest", "sha256:" + "a" * 64 + "\n"),
        ("attempt", "input_manifest_hash", "a" * 64 + "\n"),
        ("attempt", "config_hash", "a" * 64 + "\n"),
        ("result", "payload_sha256", "a" * 64 + "\n"),
    ],
)
def test_exact_formats_reject_trailing_line_terminators_without_format_checker(
    section: str | None,
    field: str,
    value: str,
) -> None:
    payload = deepcopy(_example("smartperfetto"))
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    target[field] = value
    validator = jsonschema.Draft202012Validator(_load(SCHEMA_PATH))

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize("engine_id", ["smartperfetto", "android_memory"])
def test_canonical_engine_result_examples_match_schema(engine_id: str) -> None:
    _validator().validate(_example(engine_id))


@pytest.mark.parametrize(
    ("engine_id", "source_contract"),
    [
        ("smartperfetto", "android-memory-ai-context-1.2"),
        ("android_memory", "workspace-agent-v1"),
    ],
)
def test_engine_contract_cross_pairing_is_rejected(
    engine_id: str,
    source_contract: str,
) -> None:
    payload = deepcopy(_example(engine_id))
    engine = payload["engine"]
    assert isinstance(engine, dict)
    engine["source_contract"] = source_contract

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize("engine_id", ["smartperfetto", "android_memory"])
@pytest.mark.parametrize(
    ("object_name", "unknown_key"),
    [
        ("root", "unknown_root"),
        ("engine", "unknown_engine"),
        ("attempt", "unknown_attempt"),
        ("result", "unknown_result"),
    ],
)
def test_closed_objects_reject_unknown_keys(
    engine_id: str,
    object_name: str,
    unknown_key: str,
) -> None:
    payload = deepcopy(_example(engine_id))
    target = payload if object_name == "root" else payload[object_name]
    assert isinstance(target, dict)
    target[unknown_key] = "not-permitted"

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize("engine_id", ["smartperfetto", "android_memory"])
def test_failed_result_state_is_rejected(engine_id: str) -> None:
    payload = deepcopy(_example(engine_id))
    result = payload["result"]
    assert isinstance(result, dict)
    result["state"] = "failed"

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("tenant_resource_version",), 0),
        (("attempt", "number"), 0),
    ],
)
def test_zero_versions_and_attempts_are_rejected(
    path: tuple[str, ...],
    value: int,
) -> None:
    payload = deepcopy(_example("smartperfetto"))
    target = payload
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize("field", ["artifact_id", "analysis_id", "execution_id"])
def test_malformed_uuids_are_rejected(field: str) -> None:
    payload = deepcopy(_example("smartperfetto"))
    payload[field] = "not-a-uuid"

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("engine", "source_commit_sha", "A" * 40),
        ("engine", "image_digest", "sha256:" + "g" * 64),
        ("attempt", "input_manifest_hash", "0" * 63),
        ("attempt", "config_hash", "0" * 65),
        ("result", "payload_sha256", "not-a-sha256"),
    ],
)
def test_malformed_provenance_and_payload_hashes_are_rejected(
    section: str,
    field: str,
    value: str,
) -> None:
    payload = deepcopy(_example("smartperfetto"))
    target = payload[section]
    assert isinstance(target, dict)
    target[field] = value

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize("invalid_payload", [None, [], "payload", 1, False])
def test_non_object_result_payload_is_rejected(invalid_payload: object) -> None:
    payload = deepcopy(_example("smartperfetto"))
    result = payload["result"]
    assert isinstance(result, dict)
    result["payload"] = invalid_payload

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize(
    ("engine_id", "execution_id", "expected_artifact_id"),
    [
        (
            "smartperfetto",
            "00000000-0000-4000-8000-000000000101",
            "c79e45ad-fcb4-5b16-a327-f3aae70eebbc",
        ),
        (
            "android_memory",
            "00000000-0000-4000-8000-000000000102",
            "5f9cc3e2-5d41-5db9-8964-38dc5cd819b4",
        ),
    ],
)
def test_examples_use_deterministic_artifact_ids(
    engine_id: str,
    execution_id: str,
    expected_artifact_id: str,
) -> None:
    payload = _example(engine_id)

    assert payload["execution_id"] == execution_id
    assert payload["artifact_id"] == expected_artifact_id
    assert uuid5(RESULT_NAMESPACE, execution_id) == UUID(expected_artifact_id)


@pytest.mark.parametrize(
    ("engine_id", "expected_hash"),
    [
        (
            "smartperfetto",
            "07b9aa68bf4d16936ee0d6f7b0234db1cc1d2e1e6b80e7c0a6fa8963289723b3",
        ),
        (
            "android_memory",
            "3960e8b9c6487d4c1a61d45fcbad511c048d721ff76047dac45ba89802eb7907",
        ),
    ],
)
def test_examples_have_byte_accurate_payload_hashes(
    engine_id: str,
    expected_hash: str,
) -> None:
    payload = _example(engine_id)
    result = payload["result"]
    assert isinstance(result, dict)

    assert _canonical_sha256(result["payload"]) == expected_hash
    assert result["payload_sha256"] == expected_hash
