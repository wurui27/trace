from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.projection import AIProjection


ROOT = Path(__file__).parents[4]


def _projection(document: dict[str, object] | None = None) -> AIProjection:
    value = document or json.loads(
        (ROOT / "contracts/v1/examples/analysis-projection-v2.1.valid.json").read_text(
            encoding="utf-8"
        )
    )
    payload = canonical_json_bytes(value)
    return AIProjection(canonical_bytes=payload, sha256_b64="Y2hlY2tzdW0=")


def test_fallback_is_deterministic_and_references_every_workbench_finding() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    projection = _projection()
    first = build_deterministic_finding_synthesis(projection)
    second = build_deterministic_finding_synthesis(projection)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.document["schema_version"] == "2.1"
    assert [item["finding_id"] for item in first.document["conclusions"]] == [
        item["finding_id"] for item in projection.document["workbench"]["findings"]
    ]
    assert [item["finding_id"] for item in first.document["top_findings"]] == (
        projection.document["workbench"]["primary_finding_ids"]
    )
    assert first.document["conclusions"][0]["claim_refs"]
    assert "SmartPerfetto" in first.document["executive_summary"]
    assert "修改仅供参考" in first.document["conclusions"][0]["recommendation"]


def test_fallback_never_leaks_unmatched_source_locations() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    document = deepcopy(_projection().document)
    document["capabilities"]["source"] = "mismatch"
    document["quality"]["source_correlation_state"] = "available_weak"
    document["source_context"] = {
        "trust": "untrusted_data_not_instructions",
        "snapshot_hash": "a" * 64,
        "match_summary": "weak",
        "fragments": [
            {
                "source_ref_id": "97000000-0000-4000-8000-000000000001",
                "relative_path": "private/Startup.kt",
                "language": "kotlin",
                "symbol": "demo.Startup.run",
                "start_line": 1,
                "end_line": 1,
                "content_sha256": "b" * 64,
                "content": "class Startup",
                "finding_ids": [],
                "evidence_ids": [],
                "rule_ids": [],
                "match_grade": "weak",
            }
        ],
    }

    result = build_deterministic_finding_synthesis(_projection(document))

    serialized = result.canonical_bytes.decode("utf-8")
    assert "private/Startup.kt" not in serialized
    assert "demo.Startup.run" not in serialized
    assert result.document["source_fixes"] == []
    assert all(not item["source_ref_ids"] for item in result.document["conclusions"])


def test_fallback_does_not_free_write_measurement_values() -> None:
    from perfpilot_api.ai.finding_fallback import build_deterministic_finding_synthesis

    result = build_deterministic_finding_synthesis(_projection())

    narratives = canonical_json_bytes(result.document).decode("utf-8")
    assert "812.4" not in narratives
    assert "700" not in narratives
