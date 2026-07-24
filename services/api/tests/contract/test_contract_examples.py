import json
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
