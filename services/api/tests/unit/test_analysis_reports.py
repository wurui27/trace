from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from perfpilot_api.services.analyses import (
    AnalysisUnavailableError,
    _aggregate_report_version,
    _assemble_report,
    _bundle_sha256_b64,
    _copy_public_json,
    _report_is_available,
)


ROOT = Path(__file__).parents[4]
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


def _example() -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts/v1/examples/analysis-report.partial.valid.json").read_text(
            encoding="utf-8"
        )
    )


def _report_rows() -> tuple[SimpleNamespace, list[SimpleNamespace], list[SimpleNamespace]]:
    report = _example()
    scenarios: list[SimpleNamespace] = []
    versions: list[SimpleNamespace] = []
    for item in report["scenario_reports"]:
        scenario_id = UUID(item["scenario_job_id"])
        failure = item["failure"]
        scenarios.append(
            SimpleNamespace(
                id=scenario_id,
                scenario_type=item["scenario_type"],
                state=item["result_state"],
                device_group_id=(
                    UUID(item["device_group_id"]) if item["device_group_id"] is not None else None
                ),
                device_group_reason=item["device_group_reason"],
                failure_code=failure["code"] if failure is not None else None,
                version=2,
                updated_at=NOW,
            )
        )
        bundle = item["bundle"]
        if bundle is not None:
            versions.append(
                SimpleNamespace(
                    scenario_result_id=scenario_id,
                    report_version=1,
                    bundle=deepcopy(bundle),
                    bundle_sha256_b64=_bundle_sha256_b64(bundle),
                    generated_at=NOW,
                )
            )
    job = SimpleNamespace(
        id=UUID(report["analysis_id"]),
        analysis_mode="device",
        state="partially_completed",
        updated_at=NOW,
    )
    return job, scenarios, versions


def test_aggregate_report_version_is_js_safe_and_changes_with_components() -> None:
    _, scenarios, versions = _report_rows()
    latest = {version.scenario_result_id: version for version in versions}

    first = _aggregate_report_version(scenarios=scenarios, latest=latest)
    versions[-1].report_version += 1
    second = _aggregate_report_version(scenarios=scenarios, latest=latest)

    assert 1 <= first <= 9_007_199_254_740_991
    assert 1 <= second <= 9_007_199_254_740_991
    assert first != second


def test_report_assembly_rejects_schema_valid_signed_url_in_free_text() -> None:
    job, scenarios, versions = _report_rows()
    bundle = versions[0].bundle
    bundle["findings"][0]["summary"] = "https://objects.example/private?X-Amz-Signature=secret"
    versions[0].bundle_sha256_b64 = _bundle_sha256_b64(bundle)

    with pytest.raises(AnalysisUnavailableError, match="private data"):
        _assemble_report(job=job, scenarios=scenarios, versions=versions)


@pytest.mark.parametrize(
    "value",
    (
        {"downloadUrl": "https://objects.example/private"},
        {"fields": {"note": "authorization=Bearer private-token"}},
        {"fields": {"note": "s3://private-bucket/private-key"}},
    ),
)
def test_copy_public_json_rejects_private_data(value: object) -> None:
    with pytest.raises(AnalysisUnavailableError, match="private data"):
        _copy_public_json(value)


def test_report_assembly_rejects_parent_child_aggregate_drift() -> None:
    job, scenarios, versions = _report_rows()
    job.state = "failed"

    with pytest.raises(AnalysisUnavailableError, match="aggregate state"):
        _assemble_report(job=job, scenarios=scenarios, versions=versions)


def test_report_availability_uses_the_same_bundle_validation_as_report_read() -> None:
    job, scenarios, versions = _report_rows()
    children = [
        SimpleNamespace(
            id=scenario.id,
            state="completed",
            scenario_type=scenario.scenario_type,
            failure_code=None,
        )
        for scenario in scenarios
    ]
    for scenario in scenarios:
        scenario.state = "completed"
        scenario.failure_code = None
    versions[0].bundle_sha256_b64 = "A" * 43 + "="

    assert not _report_is_available(children, scenarios, versions, parent_state="completed")
