from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.reports.memory_join import (
    AndroidMemoryNormalizationError,
    join_android_memory_result,
    join_unavailable_android_memory,
)
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import build_ai_projection
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.services.canonical_result_reader import LoadedCanonicalResult


ROOT = Path(__file__).parents[4]
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
TEAM_ID = UUID("81000000-0000-4000-8000-000000000001")
MEMORY_EXECUTION_ID = UUID("83000000-0000-4000-8000-000000000001")


def _core() -> NormalizedTraceReport:
    document = json.loads(
        (ROOT / "contracts/v1/examples/normalized-trace-report.valid.json").read_text()
    )
    document["analysis_mode"] = "device"
    payload = canonical_json_bytes(document)
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
    )


def _memory_payload(*, support_level: str = "strong") -> dict[str, object]:
    return {
        "context_type": "android-memory-ai-context",
        "schema_version": "1.2",
        "generator": {"name": "android-memory-ai", "version": "1.2.0"},
        "request": {
            "intent": "quick-triage",
            "evaluated_intents": ["quick-triage"],
        },
        "evidence": {
            "coverage": {
                "level": support_level,
                "available": ["meminfo", "smaps"],
                "missing_required": [],
                "missing_supporting": ["hprof"],
                "missing_any_of": [],
                "inadequate": [],
                "rationale_en": "DO_NOT_COPY_FREEFORM_COVERAGE_TEXT",
            },
            "accounting_ledger": {
                "schema_version": "1.0",
                "status": "available",
                "source_boundary": {
                    "policy_en": "DO_NOT_COPY_FREEFORM_LEDGER_TEXT",
                },
                "rows": [
                    {
                        "name": "TOTAL",
                        "meminfo": {
                            "pss_total_kb": 123456,
                            "private_dirty_kb": 45678,
                            "private_clean_kb": 987,
                            "swap_pss_kb": 321,
                            "rss_total_kb": 150000,
                        },
                    },
                    {
                        "name": "Native Heap",
                        "meminfo": {
                            "pss_total_kb": 32000,
                            "private_dirty_kb": 30000,
                            "private_clean_kb": 0,
                            "swap_pss_kb": 12,
                            "rss_total_kb": 34000,
                        },
                    },
                ],
            },
        },
        "analysis_contract": {
            "support_level": support_level,
            "primary_intent_support_level": support_level,
            "privacy": {
                "raw_contents_embedded": False,
                "local_paths_included": False,
            },
        },
        "next_evidence": [
            {
                "artifact_type": "hprof",
                "reason_en": "DO_NOT_COPY_FREEFORM_GAP_TEXT",
            }
        ],
        "limitations": [{"en": "DO_NOT_COPY_FREEFORM_LIMITATION_TEXT"}],
    }


def _memory_source(
    *,
    payload: dict[str, object] | None = None,
    state: str = "completed",
    analysis_id: UUID = ANALYSIS_ID,
) -> LoadedCanonicalResult:
    artifact_id = result_artifact_id(MEMORY_EXECUTION_ID)
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=TEAM_ID,
            analysis_id=analysis_id,
            execution_id=MEMORY_EXECUTION_ID,
            expected_execution_version=1,
            tenant_resource_version=7,
            artifact_id=artifact_id,
            engine_id="android_memory",
            adapter_version="1.0.0",
            engine_commit_sha="2" * 40,
            engine_image_digest="sha256:" + "3" * 64,
            attempt_number=1,
            input_manifest_hash="4" * 64,
            config_hash="5" * 64,
            result=EngineResult(
                contract="android-memory-ai-context-1.2",
                state=state,  # type: ignore[arg-type]
                payload=deepcopy(payload or _memory_payload()),
            ),
        )
    )
    return LoadedCanonicalResult(
        team_id=TEAM_ID,
        analysis_id=analysis_id,
        execution_id=MEMORY_EXECUTION_ID,
        artifact_id=artifact_id,
        tenant_resource_version=7,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )


def test_join_adds_allowlisted_memory_metrics_and_ai_evidence() -> None:
    joined = join_android_memory_result(_core(), _memory_source())

    document = joined.document
    memory = next(
        item
        for item in document["scenario_reports"]
        if item["scenario_type"] == "memory_cycle"
    )
    metrics = {item["name"]: item for item in memory["metrics"]}
    assert metrics["memory.meminfo.total.pss_kb"]["numeric_value"] == 123456
    assert metrics["memory.meminfo.native_heap.private_dirty_kb"]["numeric_value"] == 30000
    assert all(item["threshold"] is None for item in metrics.values())
    assert memory["findings"] == []
    assert memory["core_state"] == "complete"
    assert memory["evidence"][0]["fields"]["support_level"] == "strong"
    assert memory["evidence"][0]["artifact_id"] == str(result_artifact_id(MEMORY_EXECUTION_ID))

    projection = build_ai_projection(
        joined,
        analysis_profile="auto",
        question="Analyze memory evidence",
    )
    projected_memory = next(
        item
        for item in projection.document["scenarios"]
        if item["scenario_type"] == "memory_cycle"
    )
    assert projected_memory["metrics"]
    assert projected_memory["evidence"]
    serialized = projection.canonical_bytes.decode("utf-8")
    assert "DO_NOT_COPY_FREEFORM" not in serialized


def test_insufficient_memory_result_is_partial_and_never_invents_zero_metrics() -> None:
    payload = _memory_payload(support_level="insufficient")
    payload["evidence"] = {
        "coverage": {
            "level": "insufficient",
            "available": [],
            "missing_required": ["meminfo"],
            "missing_supporting": [],
            "missing_any_of": [],
            "inadequate": [],
        }
    }

    joined = join_android_memory_result(
        _core(),
        _memory_source(payload=payload, state="insufficient_data"),
    )

    memory = next(
        item
        for item in joined.document["scenario_reports"]
        if item["scenario_type"] == "memory_cycle"
    )
    assert joined.document["core_state"] == "partial"
    assert memory["core_state"] == "partial"
    assert memory["metrics"] == []
    assert any(
        item["code"] == "android_memory.evidence_insufficient"
        for item in joined.document["limitations"]
    )


def test_unavailable_memory_preserves_smartperfetto_and_marks_explicit_partial() -> None:
    joined = join_unavailable_android_memory(_core(), reason="execution_failed")

    document = joined.document
    assert document["scenario_reports"][0]["scenario_type"] == "startup"
    memory = next(
        item
        for item in document["scenario_reports"]
        if item["scenario_type"] == "memory_cycle"
    )
    assert memory["core_state"] == "partial"
    assert memory["metrics"] == []
    assert memory["trace_capabilities"] == [
        {
            "name": "android_memory_evidence",
            "required": True,
            "status": "unavailable",
            "reason": "Android memory analysis did not produce a usable result.",
        }
    ]
    assert any(
        item["code"] == "android_memory.execution_failed"
        for item in document["limitations"]
    )


def test_join_is_byte_stable_and_rejects_cross_analysis_results() -> None:
    first = join_android_memory_result(_core(), _memory_source())
    second = join_android_memory_result(_core(), _memory_source())
    assert first.canonical_bytes == second.canonical_bytes

    with pytest.raises(
        AndroidMemoryNormalizationError,
        match="^Android memory result cannot be normalized$",
    ):
        join_android_memory_result(
            _core(),
            _memory_source(
                analysis_id=UUID("82000000-0000-4000-8000-000000000099")
            ),
        )
