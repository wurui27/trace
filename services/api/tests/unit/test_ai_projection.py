from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.privacy import ProjectionPrivacyError, reject_private_json
from perfpilot_api.reports.projection import (
    ProjectionQuestionError,
    build_ai_projection,
)
from perfpilot_api.reports.source_context import validate_source_context


def _core() -> NormalizedTraceReport:
    document = json.loads(
        (Path(__file__).parents[4] / "contracts/v1/examples/normalized-trace-report.valid.json").read_text()
    )
    payload = canonical_json_bytes(document)
    return NormalizedTraceReport(canonical_bytes=payload, sha256_b64="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=")


def test_projection_is_allowlisted_and_uses_only_authoritative_question() -> None:
    core = _core()
    document = core.document
    document["provenance"]["engine_commit_sha"] = "private source commit"
    document["scenario_reports"][0]["trace_health"]["target_resolution"]["package_name"] = "private.package"
    payload = canonical_json_bytes(document)
    core = NormalizedTraceReport(canonical_bytes=payload, sha256_b64=core.sha256_b64)

    projection = build_ai_projection(core, analysis_profile="auto", question="  Diagnose startup  ")

    assert projection.document["question"] == "Diagnose startup"
    assert projection.document["source"] == {
        "engine_id": "smartperfetto",
        "adapter_version": "1.0.0",
        "source_contract": "workspace-agent-v1",
        "canonical_artifact_id": "85000000-0000-4000-8000-000000000001",
    }
    serialized = projection.canonical_bytes.decode("utf-8")
    assert "private source commit" not in serialized
    assert "private.package" not in serialized
    assert set(projection.document["scenarios"][0]) == {
        "scenario_id", "scenario_type", "core_state", "metrics", "findings", "evidence", "limitations"
    }


def test_v2_projection_is_default_even_without_source_context() -> None:
    projection = build_ai_projection(
        _core(), analysis_profile="auto", question=None
    )

    assert projection.document["schema_version"] == "2.0"
    assert projection.document["source_context"] is None


def test_v2_projection_copies_only_server_validated_source_refs() -> None:
    content = "class Startup { fun init() = Unit }\n"
    context = validate_source_context(
        {
            "snapshot_id": "95000000-0000-4000-8000-000000000001",
            "snapshot_hash": "a" * 64,
            "git_head": "b" * 40,
            "tracked_dirty_count": 0,
            "fragments": [
                {
                    "source_ref_id": "97000000-0000-4000-8000-000000000001",
                    "relative_path": "app/src/main/java/demo/Startup.kt",
                    "language": "kotlin",
                    "symbol": "demo.Startup.init",
                    "start_line": 1,
                    "end_line": 1,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "snapshot_hash": "a" * 64,
                    "finding_ids": ["85000000-0000-4000-8000-000000000001"],
                    "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
                    "rule_ids": ["android.ui.blocking_wait"],
                    "match_signals": ["trace_symbol"],
                }
            ],
            "exclusions": [],
            "truncated": False,
        },
        direct_identifiers=("demo.Startup.init",),
        allowed_finding_ids=("85000000-0000-4000-8000-000000000001",),
        allowed_evidence_ids=("86000000-0000-4000-8000-000000000001",),
    )

    document = build_ai_projection(
        _core(),
        analysis_profile="auto",
        question=None,
        source_context=context,
    ).document

    assert document["source_context"]["match_summary"] == "strong"
    assert document["source_context"]["fragments"][0]["match_grade"] == "strong"
    assert "git_head" not in json.dumps(document)


@pytest.mark.parametrize(
    "private_value",
    [
        "https://objects.invalid/a?X-Amz-Signature=secret",
        "https://objects.invalid/private/customer.trace",
        "https://storage.invalid/blob?sig=secret",
        "https://storage.googleapis.com/a?X-Goog-Signature=secret",
        "https://user:secret@objects.invalid/a",
        "postgresql://user:secret@db.invalid/app",
        "s3://private-bucket/customer/trace",
        "gs://private-bucket/customer/trace",
        "Bearer private-token-value",
        "Basic dXNlcjpzZWNyZXQ=",
        "api_key=private-token-value",
        "-----BEGIN PRIVATE KEY-----",
        "/srv/private/customer.trace",
        r"C:\\private\\customer.trace",
        "%2Fsrv%2Fprivate%2Fcustomer.trace",
        "%EF%BC%8Fsrv%EF%BC%8Fprivate%EF%BC%8Fcustomer.trace",
        "../private/customer.trace",
    ],
)
def test_projection_rejects_private_strings(private_value: str) -> None:
    document = _core().document
    document["scenario_reports"][0]["findings"][0]["summary"] = private_value
    core = NormalizedTraceReport(
        canonical_bytes=canonical_json_bytes(document),
        sha256_b64="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
    )
    with pytest.raises(ProjectionPrivacyError, match="^projection contains private data$"):
        build_ai_projection(core, analysis_profile="auto", question=None)


@pytest.mark.parametrize("value", [{"nested": object()}, {"value": float("nan")}, {"value": "x" * 2001}])
def test_recursive_scanner_rejects_non_json_nonfinite_and_overlong_values(value: object) -> None:
    with pytest.raises(ProjectionPrivacyError, match="^projection contains private data$"):
        reject_private_json(value)


def test_recursive_scanner_rejects_cycles_and_private_keys() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    with pytest.raises(ProjectionPrivacyError, match="^projection contains private data$"):
        reject_private_json(cycle)
    with pytest.raises(ProjectionPrivacyError, match="^projection contains private data$"):
        reject_private_json({"/private/path": "safe"})
    with pytest.raises(ProjectionPrivacyError, match="^projection contains private data$"):
        reject_private_json({"password": "hunter2"})


@pytest.mark.parametrize("question", ["", " \t ", "x" * 2001])
def test_question_must_be_nonempty_and_bounded(question: str) -> None:
    with pytest.raises(ProjectionQuestionError):
        build_ai_projection(_core(), analysis_profile="auto", question=question)


def test_projection_is_defensive_and_size_bounded() -> None:
    projection = build_ai_projection(_core(), analysis_profile="auto", question=None)
    document = projection.document
    document["question"] = "changed"
    assert projection.document["question"] is None
    with pytest.raises(Exception):
        build_ai_projection(_core(), analysis_profile="auto", question=None, max_bytes=1)
    with pytest.raises(ProjectionPrivacyError):
        build_ai_projection(
            _core(),
            analysis_profile="auto",
            question=None,
            max_bytes=256 * 1024 + 1,
        )
