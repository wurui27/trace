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
from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.engines.smartperfetto_contracts import SmartPerfettoReportResponse


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


def test_production_sanitizer_canonicalizer_and_normalizer_preserve_only_required_typed_fields() -> None:
    document = json.loads(FIXTURE.read_text())
    report = _report(document)
    report["reportUrl"] = "/api/reports/external-report-id"
    report["unknownPrivateField"] = "must-not-reach-canonical"
    response = SmartPerfettoReportResponse.model_validate({"success": True, "report": report})
    assert "dataEnvelopes" in response.sanitized_report
    assert "diagnostics" in response.sanitized_report
    assert "unknownPrivateField" not in response.sanitized_report
    assert "actions" not in response.sanitized_report
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=UUID("81000000-0000-4000-8000-000000000001"),
            analysis_id=UUID(str(document["analysis_id"])),
            execution_id=UUID(str(document["execution_id"])),
            expected_execution_version=1,
            tenant_resource_version=7,
            artifact_id=result_artifact_id(UUID(str(document["execution_id"]))),
            engine_id="smartperfetto",
            adapter_version="1.0.0",
            engine_commit_sha="1" * 40,
            engine_image_digest="sha256:" + "2" * 64,
            attempt_number=1,
            input_manifest_hash="3" * 64,
            config_hash="4" * 64,
            result=EngineResult(
                contract="workspace-agent-v1",
                state="completed",
                payload={"reportId": response.report_id, "report": response.sanitized_report},
            ),
        )
    )
    source = LoadedCanonicalResult(
        team_id=UUID("81000000-0000-4000-8000-000000000001"),
        analysis_id=UUID(str(document["analysis_id"])),
        execution_id=UUID(str(document["execution_id"])),
        artifact_id=result_artifact_id(UUID(str(document["execution_id"]))),
        tenant_resource_version=7,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )
    assert normalize_smartperfetto_result(source).document["scenario_reports"]


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


def test_normalized_report_document_is_a_defensive_copy_of_its_canonical_bytes() -> None:
    report = normalize_smartperfetto_result(_source())
    document = report.document
    document["analysis_id"] = "mutated"
    assert report.document["analysis_id"] != "mutated"
    assert report.sha256_b64 == base64.b64encode(hashlib.sha256(report.canonical_bytes).digest()).decode()


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


@pytest.mark.parametrize("mutation", ["cross_scenario_claim_evidence", "duplicate_envelope_id", "unknown_severity"])
def test_normalizer_rejects_cross_scenario_claim_evidence_duplicate_envelopes_and_unknown_severity(
    mutation: str,
) -> None:
    document = json.loads(FIXTURE.read_text())
    report = _report(document)
    if mutation == "cross_scenario_claim_evidence":
        report["diagnostics"][0]["claimRef"] = "scroll.partial"  # type: ignore[index]
    elif mutation == "duplicate_envelope_id":
        report["dataEnvelopes"][1]["id"] = report["dataEnvelopes"][0]["id"]  # type: ignore[index]
    else:
        report["diagnostics"][0]["severity"] = "unreviewed"  # type: ignore[index]
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
