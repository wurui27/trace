from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.writer import (
    AnalysisReportWriteRequest,
    ReportSourceError,
    compose_analysis_report,
    public_item_id,
    report_version_id,
)


ROOT = Path(__file__).resolve().parents[4]
SYNTHESIS_ID = UUID("22222222-2222-4222-8222-222222222222")
TEAM_ID = UUID("11000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
CANONICAL_ID = UUID("85000000-0000-4000-8000-000000000001")
PROJECTION_ID = UUID("89000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("88000000-0000-4000-8000-000000000001")
CHECKSUM = base64.b64encode(b"c" * 32).decode("ascii")
PROMPT_CHECKSUM = base64.b64encode(b"p" * 32).decode("ascii")
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts" / "v1" / "examples" / name).read_text(encoding="utf-8")
    )


def _request(*, failed: bool = False) -> AnalysisReportWriteRequest:
    return AnalysisReportWriteRequest(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        synthesis_execution_id=SYNTHESIS_ID,
        tenant_resource_version=7,
        generation=2,
        generated_at=NOW,
        core_document=_load("normalized-trace-report.valid.json"),
        synthesis_document=None if failed else _load("synthesis-output.valid.json"),
        synthesis_failure_code="synthesis_unavailable" if failed else None,
        canonical_artifact_id=CANONICAL_ID,
        canonical_sha256_b64=CHECKSUM,
        projection_artifact_id=PROJECTION_ID,
        projection_sha256_b64=CHECKSUM,
        synthesis_artifact_id=None if failed else CANDIDATE_ID,
        synthesis_sha256_b64=None if failed else CHECKSUM,
        normalizer_version="smartperfetto-normalizer-1",
        prompt_template_version="1.0.0",
        prompt_template_sha256_b64=PROMPT_CHECKSUM,
        report_worker_image_digest="sha256:" + "1" * 64,
        provider_protocol="chat-completions-json-schema-v1",
        provider_name="approved-provider",
        model="approved-model",
        prompt_tokens=None if failed else 100,
        completion_tokens=None if failed else 200,
        total_tokens=None if failed else 300,
        latency_ms=None if failed else 1234,
    )


def test_report_and_public_item_ids_are_deterministic_and_position_stable() -> None:
    assert report_version_id(SYNTHESIS_ID) == report_version_id(SYNTHESIS_ID)
    assert public_item_id(SYNTHESIS_ID, "recommendation", 0) == public_item_id(
        SYNTHESIS_ID, "recommendation", 0
    )
    assert public_item_id(SYNTHESIS_ID, "recommendation", 0) != public_item_id(
        SYNTHESIS_ID, "recommendation", 1
    )
    assert public_item_id(SYNTHESIS_ID, "recommendation", 0) != public_item_id(
        SYNTHESIS_ID, "retest", 0
    )


def test_composer_builds_valid_ordered_v11_report_and_exact_provenance() -> None:
    result = compose_analysis_report(_request(), report_version=4)

    assert result.id == report_version_id(SYNTHESIS_ID)
    assert result.document["schema_version"] == "1.1"
    assert result.document["report_version"] == 4
    assert result.document["generated_at"] == "2026-08-03T12:00:00Z"
    assert result.document["state"] == "completed"
    scenarios = result.document["scenario_reports"]
    assert [item["scenario_type"] for item in scenarios] == ["startup"]
    synthesis = result.document["synthesis"]
    assert synthesis["state"] == "completed"
    assert synthesis["provenance"]["generation"] == 2
    assert synthesis["provenance"]["projection_artifact_id"] == str(PROJECTION_ID)
    assert synthesis["provenance"]["prompt_tokens"] == 100
    assert result.tenant_provenance["latency_ms"] == 1234
    assert result.tenant_provenance["public_item_ids"] == {
        "recommendations": [str(public_item_id(SYNTHESIS_ID, "recommendation", 0))],
        "retest_plan": [str(public_item_id(SYNTHESIS_ID, "retest", 0))],
    }
    assert result.canonical_bytes == canonical_json_bytes(result.document)
    assert result.sha256_b64 == base64.b64encode(
        hashlib.sha256(result.canonical_bytes).digest()
    ).decode("ascii")


def test_composer_builds_v12_single_document_without_source_context() -> None:
    synthesis = _load("synthesis-output-v2.valid.json")
    synthesis["source_fixes"] = []
    synthesis["conclusions"][0]["source_ref_ids"] = []
    synthesis["conclusions"][0]["source_root_cause"] = (
        "本次没有足够强的源码匹配，暂不能定位到具体实现。"
    )
    source_code = {
        "requested": False,
        "provider_kind": None,
        "agent_id": None,
        "workspace_id": None,
        "snapshot_policy": None,
        "validation_profile_id": None,
        "snapshot": None,
        "context_state": "not_requested",
        "match_summary": "none",
        "source_refs": [],
        "exclusions": [],
        "fixes": [],
        "limitations": [],
    }

    result = compose_analysis_report(
        replace(
            _request(),
            synthesis_document=synthesis,
            source_code_document=source_code,
        ),
        report_version=4,
    )

    assert result.document["schema_version"] == "1.2"
    assert result.document["synthesis"]["output"]["schema_version"] == "2.0"
    assert result.document["source_code"] == source_code


def test_composer_enriches_strong_source_fix_in_the_same_v12_document() -> None:
    source_report = _load("analysis-report-v1.2.valid.json")
    source_code = source_report["source_code"]
    source_code["fixes"] = []

    result = compose_analysis_report(
        replace(
            _request(),
            synthesis_document=_load("synthesis-output-v2.valid.json"),
            source_code_document=source_code,
        ),
        report_version=4,
    )

    public_fix = result.document["source_code"]["fixes"][0]
    assert public_fix["fix_id"] == result.document["synthesis"]["output"][
        "source_fixes"
    ][0]["fix_id"]
    assert result.document["synthesis"]["output"]["source_fixes"][0][
        "validation_profile_id"
    ] is None
    assert public_fix["validation_profile_id"] == source_code[
        "validation_profile_id"
    ]
    assert public_fix["verification"]["state"] == "not_requested"


def test_composer_keeps_source_diff_when_validation_profile_is_not_configured() -> None:
    source_code = _load("analysis-report-v1.2.valid.json")["source_code"]
    source_code["validation_profile_id"] = None
    source_code["fixes"] = []

    result = compose_analysis_report(
        replace(
            _request(),
            synthesis_document=_load("synthesis-output-v2.valid.json"),
            source_code_document=source_code,
        ),
        report_version=4,
    )

    public_fix = result.document["source_code"]["fixes"][0]
    assert public_fix["validation_profile_id"] is None
    assert public_fix["verification"]["state"] == "not_configured"
    assert public_fix["verification"]["profile_id"] is None


def test_composer_preserves_device_mode_in_v11_report() -> None:
    core = _load("normalized-trace-report.valid.json")
    core["analysis_mode"] = "device"

    result = compose_analysis_report(
        replace(_request(), core_document=core),
        report_version=1,
    )

    assert result.document["schema_version"] == "1.1"
    assert result.document["analysis_mode"] == "device"
    assert result.document["state"] == "partially_completed"
    assert [item["scenario_type"] for item in result.document["scenario_reports"]] == [
        "startup"
    ]


def test_composer_publishes_stable_core_report_when_synthesis_failed() -> None:
    result = compose_analysis_report(_request(failed=True), report_version=1)

    assert result.document["state"] == "partially_completed"
    assert result.document["synthesis"] == {
        "state": "failed",
        "output": None,
        "synthesis_artifact_id": None,
        "failure_code": "synthesis_unavailable",
        "provenance": None,
    }
    assert result.tenant_provenance["synthesis_artifact_id"] is None


@pytest.mark.parametrize(
    "candidate",
    [
        replace(_request(), generated_at=NOW.replace(tzinfo=None)),
        replace(_request(), total_tokens=301),
        replace(_request(), canonical_sha256_b64="not-a-checksum"),
        replace(_request(), synthesis_artifact_id=None),
        replace(_request(failed=True), synthesis_failure_code="private details here"),
    ],
)
def test_composer_rejects_non_authoritative_or_inconsistent_inputs(
    candidate: AnalysisReportWriteRequest,
) -> None:
    with pytest.raises(ReportSourceError):
        compose_analysis_report(candidate, report_version=1)
