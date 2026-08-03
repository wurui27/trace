from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import SmartPerfettoNormalizationError, normalize_smartperfetto_result
from perfpilot_api.services.canonical_result_reader import LoadedCanonicalResult


FIXTURE = Path(__file__).parents[1] / "fixtures/canonical_results/smartperfetto-result-contract-1.0.0.json"


def _source(document: dict[str, object] | None = None) -> LoadedCanonicalResult:
    copied = deepcopy(document or json.loads(FIXTURE.read_text()))
    try:
        payload = canonical_json_bytes(copied)
    except Exception:
        payload = json.dumps(copied, allow_nan=True).encode()
    return LoadedCanonicalResult(
        team_id=UUID("81000000-0000-4000-8000-000000000001"),
        analysis_id=UUID(str(copied["analysis_id"])),
        execution_id=UUID(str(copied["execution_id"])),
        artifact_id=UUID(str(copied["artifact_id"])),
        tenant_resource_version=7,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
        document=copied,
        canonical_bytes=payload,
    )


def _report(document: dict[str, object]) -> dict[str, object]:
    return document["result"]["payload"]["report"]  # type: ignore[index,return-value]


def test_normalizer_is_byte_stable_and_sorts_public_ids() -> None:
    original = json.loads(FIXTURE.read_text())
    reordered = {key: deepcopy(original[key]) for key in reversed(tuple(original))}
    first = normalize_smartperfetto_result(_source(original))
    second = normalize_smartperfetto_result(_source(reordered))
    assert first.canonical_bytes == second.canonical_bytes
    assert [item["scenario_type"] for item in first.document["scenario_reports"]] == ["startup", "scroll"]  # type: ignore[index]
    for scenario in first.document["scenario_reports"]:  # type: ignore[index]
        for key, identifier in (("metrics", "metric_id"), ("findings", "finding_id"), ("evidence", "evidence_id")):
            assert [item[identifier] for item in scenario[key]] == sorted(item[identifier] for item in scenario[key])  # type: ignore[index]


def test_normalizer_only_uses_verified_or_explicitly_partial_evidence_and_excludes_private_source_fields() -> None:
    core = normalize_smartperfetto_result(_source()).document
    encoded = canonical_json_bytes(core).decode()
    assert "startup.display_delay" in encoded
    assert "scroll.unverified" in encoded
    assert all(
        finding["rule_id"] != "scroll.unverified"
        for scenario in core["scenario_reports"]  # type: ignore[index]
        for finding in scenario["findings"]
    )
    for marker in ("conversation-secret", "query-secret", "notes-secret", "echoed-query-secret", "session-secret", "workspace-secret", "run-secret", "tool-secret", "external-report-id"):
        assert marker not in encoded
    assert core["limitations"]


def test_missing_measurement_value_is_insufficient_data_never_zero() -> None:
    document = json.loads(FIXTURE.read_text())
    _report(document)["dataEnvelopes"][0]["columns"][0].pop("value")  # type: ignore[index]
    core = normalize_smartperfetto_result(_source(document)).document
    metric = core["scenario_reports"][0]["metrics"][0]  # type: ignore[index]
    assert metric["status"] == "insufficient_data"
    assert metric["numeric_value"] is None


def test_normalizer_reparses_authoritative_bytes_not_mutable_loaded_document() -> None:
    source = _source()
    expected = normalize_smartperfetto_result(source).canonical_bytes
    source.document["result"] = {"state": "completed", "payload": {"report": {}}}
    assert normalize_smartperfetto_result(source).canonical_bytes == expected


def test_normalizer_rejects_forged_canonical_bytes_or_checksum() -> None:
    source = _source()
    for forged in (replace(source, canonical_bytes=b"{}"), replace(source, sha256_b64="x" * 44)):
        with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
            normalize_smartperfetto_result(forged)


@pytest.mark.parametrize("mutation", ["cross_scenario_evidence", "duplicate_metric_id"])
def test_normalizer_rejects_cross_scenario_metric_evidence_and_global_metric_id_collisions(
    mutation: str,
) -> None:
    document = json.loads(FIXTURE.read_text())
    envelopes = _report(document)["dataEnvelopes"]  # type: ignore[index]
    if mutation == "cross_scenario_evidence":
        envelopes[0]["columns"][0]["evidenceId"] = "scroll-jank"
    else:
        envelopes[1]["columns"][0]["id"] = envelopes[0]["columns"][0]["id"]
    with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
        normalize_smartperfetto_result(_source(document))


@pytest.mark.parametrize("mutation", ["version", "nan", "duplicate", "unsupported"])
def test_normalizer_fails_closed_for_unsupported_or_ambiguous_source(mutation: str) -> None:
    document = json.loads(FIXTURE.read_text())
    report = _report(document)
    if mutation == "version":
        report["resultContract"]["version"] = "9.9.9"  # type: ignore[index]
    elif mutation == "nan":
        report["dataEnvelopes"][0]["columns"][0]["value"] = float("nan")  # type: ignore[index]
    elif mutation == "duplicate":
        report["dataEnvelopes"][0]["columns"].append(deepcopy(report["dataEnvelopes"][0]["columns"][0]))  # type: ignore[index]
    else:
        report["dataEnvelopes"][0]["type"] = "unknown-envelope@1"  # type: ignore[index]
    with pytest.raises(SmartPerfettoNormalizationError, match="^SmartPerfetto result cannot be normalized$"):
        normalize_smartperfetto_result(_source(document))
