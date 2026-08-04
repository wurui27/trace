from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.projection import AIProjection


ROOT = Path(__file__).parents[4]
UNKNOWN_ID = "87000000-0000-4000-8000-000000000001"


def _json_fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "contracts/v1/examples" / name).read_text(encoding="utf-8"))


def _projection_document() -> dict[str, object]:
    return _json_fixture("analysis-projection.valid.json")


def _projection() -> AIProjection:
    payload = canonical_json_bytes(_projection_document())
    return AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")


def _candidate() -> dict[str, object]:
    return _json_fixture("synthesis-output.valid.json")


def _validator() -> object:
    from perfpilot_api.ai.synthesis import validate_synthesis_output  # type: ignore[import-not-found]

    return validate_synthesis_output


def _validate(candidate: dict[str, object], projection: AIProjection | None = None) -> object:
    return _validator()(projection=projection or _projection(), candidate=candidate)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("top_findings", "finding_id"),
        ("top_findings", "evidence_ids"),
        ("recommendations", "finding_ids"),
        ("recommendations", "evidence_ids"),
        ("retest_plan", "metric_ids"),
    ],
)
def test_rejects_unknown_projection_references(section: str, field: str) -> None:
    candidate = _candidate()
    item = candidate[section][0]  # type: ignore[index]
    item[field] = UNKNOWN_ID if field.endswith("_id") else [UNKNOWN_ID]  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_unknown_limitation_reference() -> None:
    candidate = _candidate()
    candidate["limitations"] = [{"limitation_id": UNKNOWN_ID, "summary": "Missing input."}]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_top_finding_evidence_that_does_not_support_the_finding() -> None:
    projection_document = _projection_document()
    scenario = projection_document["scenarios"][0]
    scenario["evidence"].append(  # type: ignore[index]
        {
            "evidence_id": UNKNOWN_ID,
            "source": "perfetto.other",
            "query_id": "other.v1",
            "interval_start_ns": None,
            "interval_end_ns": None,
            "artifact_id": None,
            "fields": {},
        }
    )
    projection = AIProjection(canonical_bytes=canonical_json_bytes(projection_document), sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate()
    candidate["top_findings"][0]["evidence_ids"] = [UNKNOWN_ID]  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


def test_rejects_recommendation_without_actionable_finding_or_evidence() -> None:
    candidate = _candidate()
    recommendation = candidate["recommendations"][0]  # type: ignore[index]
    recommendation["finding_ids"] = []  # type: ignore[index]
    recommendation["evidence_ids"] = []  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


@pytest.mark.parametrize("status", ["insufficient_data", "invalid_capture"])
def test_rejects_recommendation_for_non_actionable_finding(status: str) -> None:
    projection_document = _projection_document()
    finding = projection_document["scenarios"][0]["findings"][0]  # type: ignore[index]
    finding["status"] = status
    finding["confidence"] = "none"
    projection = AIProjection(canonical_bytes=canonical_json_bytes(projection_document), sha256_b64="Y2hlY2tzdW0=")

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(_candidate(), projection)


def test_rejects_verify_metric_for_another_scenario() -> None:
    projection_document = _projection_document()
    second = deepcopy(projection_document["scenarios"][0])  # type: ignore[index]
    second["scenario_id"] = UNKNOWN_ID
    second["scenario_type"] = "scroll"
    projection_document["scenarios"].append(second)  # type: ignore[index]
    projection = AIProjection(canonical_bytes=canonical_json_bytes(projection_document), sha256_b64="Y2hlY2tzdW0=")
    candidate = _candidate()
    candidate["retest_plan"][0]["scenario_type"] = "scroll"  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate, projection)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metric_ids", []),
        ("steps", "Repeat cold launches and reach 600 ms."),
    ],
)
def test_rejects_verify_metric_without_a_metric_or_with_a_new_target(
    field: str, value: object
) -> None:
    candidate = _candidate()
    candidate["retest_plan"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_collect_evidence_retest_without_an_existing_limitation() -> None:
    candidate = _candidate()
    candidate["retest_plan"] = [
        {
            "mode": "collect_evidence",
            "scenario_type": "startup",
            "metric_ids": [],
            "limitation_ids": [UNKNOWN_ID],
            "steps": "Capture the missing evidence.",
            "success_condition": "evidence_collected",
            "failure_condition": "evidence_missing",
        }
    ]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


@pytest.mark.parametrize(
    "field_path",
    [
        ("executive_summary",),
        ("top_findings", 0, "user_impact"),
        ("recommendations", 0, "title"),
        ("recommendations", 0, "action"),
        ("recommendations", 0, "expected_effect"),
        ("retest_plan", 0, "steps"),
    ],
)
def test_rejects_new_numeric_literals_in_narrative_fields(field_path: tuple[str | int, ...]) -> None:
    candidate = _candidate()
    target: object = candidate
    for key in field_path[:-1]:
        target = target[key]  # type: ignore[index]
    target[field_path[-1]] = "A newly invented target is 16.0 ms."  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_invented_numeric_target_attached_to_unit() -> None:
    candidate = _candidate()
    candidate["retest_plan"][0]["steps"] = "Reduce the target to 16ms."

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


@pytest.mark.parametrize(
    ("section", "identifier"),
    [("recommendations", "recommendation_id"), ("retest_plan", "retest_id")],
)
def test_rejects_ai_created_public_ids(section: str, identifier: str) -> None:
    candidate = _candidate()
    candidate[section][0][identifier] = UNKNOWN_ID  # type: ignore[index]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        _validate(candidate)


def test_rejects_private_strings_with_a_stable_redacted_error() -> None:
    candidate = _candidate()
    private_value = "https://objects.invalid/a?X-Amz-Signature=private-secret"
    candidate["executive_summary"] = private_value

    with pytest.raises(ValueError) as caught:
        _validate(candidate)

    assert str(caught.value) == "AI synthesis output is invalid"
    assert private_value not in repr(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"1.0","schema_version":"1.0"}',
        b'{"schema_version":NaN}',
        b'{"schema_version":Infinity}',
        b'{} trailing',
        b"\xff",
    ],
)
def test_parse_candidate_rejects_malformed_or_ambiguous_json(payload: bytes) -> None:
    from perfpilot_api.ai.synthesis import parse_candidate  # type: ignore[import-not-found]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        parse_candidate(payload, max_bytes=128 * 1024)


def test_parse_candidate_rejects_empty_and_oversized_payloads() -> None:
    from perfpilot_api.ai.synthesis import parse_candidate  # type: ignore[import-not-found]

    for payload in (b"", b"x" * 17):
        with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
            parse_candidate(payload, max_bytes=16)


def test_returns_defensive_canonical_document() -> None:
    result = _validate(_candidate())
    document = result.document  # type: ignore[attr-defined]
    document["executive_summary"] = "changed"

    assert result.document["executive_summary"] != "changed"  # type: ignore[attr-defined]
    assert result.canonical_bytes == canonical_json_bytes(result.document)  # type: ignore[attr-defined]
