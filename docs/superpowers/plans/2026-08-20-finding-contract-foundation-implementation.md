# Finding Contract Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the closed `AnalysisReport 1.3` / `Synthesis 2.1` contracts and deterministic Finding, Evidence, Metric, capability, priority, and deduplication foundation.

**Architecture:** Keep current normalized SmartPerfetto reports immutable. Add a focused `finding_workbench.py` builder that deterministically derives the new workbench model from validated normalized reports and optional validated source context. Version-gate all new semantics so 1.0–1.2 reports and 1.0–2.0 synthesis documents remain exact.

**Tech Stack:** Python 3.12, JSON Schema 2020-12, `jsonschema`, Pydantic-compatible dictionaries, pytest, UUIDv5, Ruff.

---

## File responsibility map

- Create `services/api/src/perfpilot_api/reports/finding_workbench.py`: deterministic IDs, priority scoring, duplicate merging, capability projection, Evidence/Metric views, and workbench assembly.
- Create `services/api/tests/unit/test_finding_workbench.py`: deterministic builder and negative invariants.
- Modify `contracts/v1/reports/analysis-report.schema.json`: exact 1.3 branch and workbench definitions.
- Modify `contracts/v1/ai/analysis-projection.schema.json`: exact 2.1 projection branch.
- Modify `contracts/v1/ai/synthesis-output.schema.json`: exact 2.1 synthesis branch.
- Create `contracts/v1/examples/analysis-report-v1.3.valid.json`: complete valid 1.3 example.
- Create `contracts/v1/examples/analysis-projection-v2.1.valid.json`: complete valid 2.1 projection example.
- Create `contracts/v1/examples/synthesis-output-v2.1.valid.json`: complete valid 2.1 synthesis example.
- Modify `services/api/src/perfpilot_api/reports/projection.py`: include validated workbench and capability data in 2.1 projection.
- Modify `services/api/src/perfpilot_api/reports/semantics.py`: cross-reference, stable-order, source privacy, evidence locator, and score validation.
- Modify `services/api/tests/contract/test_ai_report_contracts.py`: new versions, legacy exactness, unknown keys, and negative semantic cases.
- Modify `services/api/tests/unit/test_ai_projection.py`: 2.1 projection tests.

### Task 1: Add exact versioned contract fixtures

**Files:**
- Create: `contracts/v1/examples/analysis-report-v1.3.valid.json`
- Create: `contracts/v1/examples/analysis-projection-v2.1.valid.json`
- Create: `contracts/v1/examples/synthesis-output-v2.1.valid.json`
- Modify: `services/api/tests/contract/test_ai_report_contracts.py`

- [ ] **Step 1: Write failing contract fixture tests**

Add the following tests beside the existing 1.2/2.0 example tests:

```python
def test_finding_workbench_examples_validate_as_closed_documents() -> None:
    report = _example("analysis-report-v1.3.valid.json")
    projection = _example("analysis-projection-v2.1.valid.json")
    synthesis = _example("synthesis-output-v2.1.valid.json")

    assert validate_contract("analysis-report", report)["schema_version"] == "1.3"
    assert validate_contract("analysis-projection", projection)["schema_version"] == "2.1"
    assert validate_contract("synthesis-output", synthesis)["schema_version"] == "2.1"


@pytest.mark.parametrize(
    ("contract", "fixture"),
    [
        ("analysis-report", "analysis-report-v1.3.valid.json"),
        ("analysis-projection", "analysis-projection-v2.1.valid.json"),
        ("synthesis-output", "synthesis-output-v2.1.valid.json"),
    ],
)
def test_finding_workbench_versions_reject_unknown_top_level_fields(
    contract: str,
    fixture: str,
) -> None:
    document = _example(fixture)
    document["private_path"] = "/Users/private/project"

    with pytest.raises(ValueError, match="^report contract is invalid$"):
        validate_contract(contract, document)


def test_legacy_report_and_synthesis_examples_remain_exact() -> None:
    assert validate_contract(
        "analysis-report", _example("analysis-report-v1.2.valid.json")
    )["schema_version"] == "1.2"
    assert validate_contract(
        "synthesis-output", _example("synthesis-output-v2.valid.json")
    )["schema_version"] == "2.0"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/contract/test_ai_report_contracts.py \
  -k 'finding_workbench_examples or finding_workbench_versions or legacy_report_and_synthesis_examples'
```

Expected: FAIL because the three 1.3/2.1 example files do not exist.

- [ ] **Step 3: Add complete example documents**

Create the three examples by preserving all identifiers from the current 1.2/2.0 fixtures and adding these exact new public sections:

```json
{
  "capabilities": {
    "trace": "available",
    "smartperfetto": "available",
    "source": "matched",
    "ai": "available"
  },
  "workbench": {
    "critical_path": [
      {
        "segment_id": "9a000000-0000-4000-8000-000000000001",
        "label": "Application 初始化",
        "start_ns": 1284000000,
        "end_ns": 1978000000,
        "duration_ns": 694000000,
        "evidence_ids": ["86000000-0000-4000-8000-000000000001"]
      }
    ],
    "metrics": [
      {
        "metric_id": "87000000-0000-4000-8000-000000000001",
        "name": "startup.application_init.duration",
        "value": 694,
        "unit": "ms",
        "aggregation": "single_sample",
        "scenario_type": "startup",
        "source": "smartperfetto",
        "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
        "quality": "available"
      }
    ],
    "evidence": [
      {
        "evidence_id": "86000000-0000-4000-8000-000000000001",
        "kind": "trace_interval",
        "scenario_type": "startup",
        "metric_ids": ["87000000-0000-4000-8000-000000000001"],
        "summary": "Application 初始化与主线程阻塞区间重叠。",
        "source": "smartperfetto",
        "locator": {
          "start_ns": 1284000000,
          "end_ns": 1978000000,
          "process": "com.rivotek.mediacenter",
          "thread": "main",
          "track": "Main Thread",
          "slice": "Application.onCreate",
          "query_id": "startup.main_thread_blocking"
        }
      }
    ],
    "findings": [
      {
        "finding_id": "85000000-0000-4000-8000-000000000001",
        "scenario_type": "startup",
        "title": "Application 初始化阻塞主线程",
        "problem": "冷启动首帧前存在明显主线程阻塞。",
        "impact": "直接增加首帧等待时间。",
        "mechanism": "同步初始化工作占用启动关键路径。",
        "root_cause": "Application 初始化阶段执行了可延后的同步工作。",
        "critical_path_contribution": 0.42,
        "priority": "p0",
        "priority_score": 88,
        "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
        "metric_ids": ["87000000-0000-4000-8000-000000000001"],
        "source_ref_ids": [],
        "status": "confirmed",
        "confidence": {
          "data_completeness": "complete",
          "evidence_grade": "E3",
          "attribution": "high",
          "statistical": "single_sample"
        },
        "confirmed_items": ["主线程阻塞与启动关键区间重叠"],
        "unconfirmed_items": ["单次样本尚未验证波动范围"],
        "retest_plan_id": "89000000-0000-4000-8000-000000000001"
      }
    ],
    "primary_finding_ids": ["85000000-0000-4000-8000-000000000001"],
    "retest_plans": [
      {
        "retest_plan_id": "89000000-0000-4000-8000-000000000001",
        "finding_id": "85000000-0000-4000-8000-000000000001",
        "scenario_type": "startup",
        "package_name": "com.rivotek.mediacenter",
        "duration_seconds": 15,
        "environment_fingerprint": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "metric_ids": ["87000000-0000-4000-8000-000000000001"],
        "pass_criteria": ["Application 初始化耗时至少下降 20%"],
        "notes": "使用相同设备、构建和冷启动步骤复测。"
      }
    ]
  }
}
```

The `analysis-projection-v2.1.valid.json` fixture must copy the same workbench values, but its AI capability is `"pending"` because synthesis has not run yet. The report fixture uses `"available"`. The `synthesis-output-v2.1.valid.json` fixture must reference only IDs present in that projection and must contain no server-owned priority score, Evidence, Metric, path, or deep-link fields.

- [ ] **Step 4: Rerun to keep a precise contract RED**

Run the command from Step 2.

Expected: FAIL with `report contract is invalid` because the schemas do not yet admit versions 1.3/2.1.

- [ ] **Step 5: Commit fixtures and tests**

```bash
git add contracts/v1/examples services/api/tests/contract/test_ai_report_contracts.py
git commit -m "test: define finding workbench contracts"
```

### Task 2: Implement deterministic Finding workbench assembly

**Files:**
- Create: `services/api/src/perfpilot_api/reports/finding_workbench.py`
- Create: `services/api/tests/unit/test_finding_workbench.py`

- [ ] **Step 1: Write deterministic ID, priority, and merge tests**

```python
from copy import deepcopy

from perfpilot_api.reports.finding_workbench import build_finding_workbench


def test_builder_is_stable_and_merges_same_root_cause() -> None:
    core = _normalized_core_with_duplicate_startup_root_causes()

    first = build_finding_workbench(
        core_document=core,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )
    second = build_finding_workbench(
        core_document=deepcopy(core),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert first == second
    assert len(first["findings"]) == 1
    finding = first["findings"][0]
    assert finding["finding_id"] == second["findings"][0]["finding_id"]
    assert finding["evidence_ids"] == sorted(set(finding["evidence_ids"]))
    assert first["primary_finding_ids"] == [finding["finding_id"]]


def test_priority_uses_server_owned_weighting() -> None:
    workbench = build_finding_workbench(
        core_document=_normalized_core_with_scored_findings(),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    by_title = {item["title"]: item for item in workbench["findings"]}
    assert by_title["关键路径主线程阻塞"]["priority_score"] == 88
    assert by_title["非关键后台波动"]["priority_score"] == 31
    assert workbench["primary_finding_ids"] == [
        by_title["关键路径主线程阻塞"]["finding_id"]
    ]


def test_e0_and_e1_findings_are_hypotheses_not_primary_findings() -> None:
    workbench = build_finding_workbench(
        core_document=_normalized_core_with_weak_finding(),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["status"] == "hypothesis"
    assert workbench["primary_finding_ids"] == []
```

Test fixtures must use only valid normalized report fields and explicit evidence intervals so the builder is tested against production-shaped input.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_workbench.py
```

Expected: collection ERROR with `ModuleNotFoundError: perfpilot_api.reports.finding_workbench`.

- [ ] **Step 3: Implement stable IDs and server-owned scoring**

Create `finding_workbench.py` with these public boundaries:

```python
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID, uuid5


_FINDING_NAMESPACE = UUID("4d8de355-1b14-4e48-9156-44f51f7ad1d3")
_EVIDENCE_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 4, "E4": 5}
_ATTRIBUTION_POINTS = {"low": 4, "medium": 12, "high": 20}


def _canonical_key(parts: tuple[str, ...]) -> str:
    normalized = "\u001f".join(part.strip().casefold() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def stable_finding_id(
    *,
    scenario_type: str,
    mechanism: str,
    root_cause_domain: str,
    responsible_component: str,
) -> str:
    key = _canonical_key(
        (scenario_type, mechanism, root_cause_domain, responsible_component)
    )
    return str(uuid5(_FINDING_NAMESPACE, key))


def priority_score(
    *,
    impact_points: int,
    evidence_grade: str,
    attribution: str,
    critical_path_points: int,
    reproducibility_points: int,
) -> int:
    evidence_points = _EVIDENCE_RANK[evidence_grade] + _ATTRIBUTION_POINTS[attribution]
    score = impact_points + evidence_points + critical_path_points + reproducibility_points
    if not 0 <= impact_points <= 40 or not 0 <= critical_path_points <= 20:
        raise ValueError("finding workbench input is invalid")
    if not 0 <= reproducibility_points <= 15 or not 0 <= score <= 100:
        raise ValueError("finding workbench input is invalid")
    return score


def build_finding_workbench(
    *,
    core_document: Mapping[str, object],
    source_context: Mapping[str, object] | None,
    package_name: str,
    duration_seconds: int,
    environment_fingerprint: str,
) -> dict[str, object]:
    scenario_reports = core_document.get("scenario_reports")
    if not isinstance(scenario_reports, list):
        raise ValueError("finding workbench input is invalid")
    findings = _merge_findings(
        scenario_reports=scenario_reports,
        source_context=source_context,
    )
    findings.sort(key=lambda item: (-int(item["priority_score"]), str(item["finding_id"])))
    primary = [
        str(item["finding_id"])
        for item in findings
        if item["status"] == "confirmed" and item["priority"] in {"p0", "p1"}
    ][:3]
    return {
        "critical_path": _critical_path(scenario_reports),
        "metrics": _metric_views(scenario_reports),
        "evidence": _evidence_views(scenario_reports),
        "findings": findings,
        "primary_finding_ids": primary,
        "retest_plans": _retest_plans(
            findings,
            package_name=package_name,
            duration_seconds=duration_seconds,
            environment_fingerprint=environment_fingerprint,
        ),
    }
```

Implement the private helpers with the following deterministic mappings. They reject malformed input with the single public message `finding workbench input is invalid`, sort all ID arrays, and merge only equal stable keys:

```python
_IMPACT_POINTS = {"critical": 40, "warning": 28, "healthy": 8, "informational": 4}
_CONFIDENCE = {"high": "high", "medium": "medium", "low": "low", "none": "low"}


def _evidence_grade(
    finding: Mapping[str, object],
    evidence_by_id: Mapping[str, Mapping[str, object]],
    *,
    has_strong_source: bool,
) -> str:
    ids = finding.get("evidence_ids")
    if not isinstance(ids, list) or not ids:
        return "E0"
    if has_strong_source:
        return "E4"
    located = all(
        isinstance(evidence_by_id.get(str(evidence_id), {}).get("interval_start_ns"), int)
        and isinstance(evidence_by_id.get(str(evidence_id), {}).get("interval_end_ns"), int)
        for evidence_id in ids
    )
    if finding.get("status") == "confirmed" and located:
        return "E3"
    if finding.get("status") == "confirmed":
        return "E2"
    return "E1"


def _priority(score: int) -> str:
    if score >= 80:
        return "p0"
    if score >= 60:
        return "p1"
    if score >= 40:
        return "p2"
    return "p3"


def _merge_findings(
    *,
    scenario_reports: list[object],
    source_context: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    fragments = source_context.get("fragments", []) if source_context else []
    if not isinstance(fragments, list):
        raise ValueError("finding workbench input is invalid")
    for scenario in scenario_reports:
        if not isinstance(scenario, Mapping):
            raise ValueError("finding workbench input is invalid")
        scenario_type = str(scenario.get("scenario_type"))
        evidence = scenario.get("evidence")
        findings = scenario.get("findings")
        if not isinstance(evidence, list) or not isinstance(findings, list):
            raise ValueError("finding workbench input is invalid")
        evidence_by_id = {
            str(item["evidence_id"]): item
            for item in evidence
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
        }
        for raw in findings:
            if not isinstance(raw, Mapping):
                raise ValueError("finding workbench input is invalid")
            original_id = str(raw.get("finding_id"))
            rule_id = str(raw.get("rule_id"))
            kind = str(raw.get("kind"))
            component = rule_id.rsplit(".", 1)[0]
            stable_id = stable_finding_id(
                scenario_type=scenario_type,
                mechanism=rule_id,
                root_cause_domain=kind,
                responsible_component=component,
            )
            strong_refs = sorted(
                str(fragment["source_ref_id"])
                for fragment in fragments
                if isinstance(fragment, Mapping)
                and fragment.get("match_grade") == "strong"
                and original_id in fragment.get("finding_ids", [])
                and isinstance(fragment.get("source_ref_id"), str)
            )
            grade = _evidence_grade(raw, evidence_by_id, has_strong_source=bool(strong_refs))
            attribution = _CONFIDENCE[str(raw.get("confidence"))]
            evidence_ids = sorted(str(value) for value in raw.get("evidence_ids", []))
            has_located_evidence = any(
                isinstance(evidence_by_id.get(evidence_id, {}).get("interval_start_ns"), int)
                for evidence_id in evidence_ids
            )
            score = priority_score(
                impact_points=_IMPACT_POINTS[str(raw.get("severity"))],
                evidence_grade=grade,
                attribution=attribution,
                critical_path_points=20 if has_located_evidence else 0,
                reproducibility_points=4,
            )
            candidate = {
                "finding_id": stable_id,
                "scenario_type": scenario_type,
                "title": str(raw.get("title")),
                "problem": str(raw.get("summary")),
                "impact": _impact_sentence(str(raw.get("severity"))),
                "mechanism": rule_id,
                "root_cause": str(raw.get("title")) if kind == "root_cause" else str(raw.get("summary")),
                "critical_path_contribution": 1.0 if has_located_evidence else 0.0,
                "priority": _priority(score),
                "priority_score": score,
                "evidence_ids": evidence_ids,
                "metric_ids": _metric_ids_for_evidence(scenario, evidence_ids),
                "source_ref_ids": strong_refs,
                "status": "confirmed" if grade in {"E2", "E3", "E4"} else "hypothesis",
                "confidence": {
                    "data_completeness": "complete" if scenario.get("core_state") == "completed" else "limited",
                    "evidence_grade": grade,
                    "attribution": attribution,
                    "statistical": "single_sample",
                },
                "confirmed_items": [str(raw.get("summary"))] if grade in {"E2", "E3", "E4"} else [],
                "unconfirmed_items": ["单次样本尚未验证波动范围"],
                "retest_plan_id": str(uuid5(_FINDING_NAMESPACE, f"retest:{stable_id}")),
            }
            current = merged.get(stable_id)
            if current is None:
                merged[stable_id] = candidate
            else:
                current["evidence_ids"] = sorted(set(current["evidence_ids"]) | set(evidence_ids))
                current["metric_ids"] = sorted(set(current["metric_ids"]) | set(candidate["metric_ids"]))
                current["source_ref_ids"] = sorted(set(current["source_ref_ids"]) | set(strong_refs))
                current["priority_score"] = max(int(current["priority_score"]), score)
                current["priority"] = _priority(int(current["priority_score"]))
    return list(merged.values())
```

`_metric_views` copies each normalized metric into the 1.3 shape, using `numeric_value` as `value` and mapping unavailable status to `None`. `_evidence_views` copies each Evidence and attaches `_locator`. `_critical_path` keeps Evidence with integer intervals, sets `duration_ns=end-start`, and sorts by `(start_ns, evidence_id)`. `_retest_plans` uses each Finding's `retest_plan_id`, the explicit package/duration/fingerprint arguments, its metric IDs, the normalized `retest` sentence as the pass criterion, and a fixed same-environment note.

- [ ] **Step 4: Run the unit tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/reports/finding_workbench.py \
  services/api/tests/unit/test_finding_workbench.py
git commit -m "feat: build deterministic finding workbench"
```

### Task 3: Add Evidence locators, capability projection, and source gates

**Files:**
- Modify: `services/api/src/perfpilot_api/reports/finding_workbench.py`
- Modify: `services/api/tests/unit/test_finding_workbench.py`

- [ ] **Step 1: Add failing locator and capability tests**

```python
def test_builder_projects_trace_locator_without_private_paths() -> None:
    workbench = build_finding_workbench(
        core_document=_normalized_core_with_trace_locator(),
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    evidence = workbench["evidence"][0]
    assert evidence["locator"] == {
        "start_ns": 1_284_000_000,
        "end_ns": 1_978_000_000,
        "process": "com.rivotek.mediacenter",
        "thread": "main",
        "track": "Main Thread",
        "slice": "Application.onCreate",
        "query_id": "startup.main_thread_blocking",
    }
    assert "/Users/" not in json.dumps(workbench)


def test_source_capability_requires_validated_strong_context() -> None:
    none = build_capabilities(core_document=_core(), source_context=None)
    weak = build_capabilities(core_document=_core(), source_context=_weak_context())
    strong = build_capabilities(core_document=_core(), source_context=_strong_context())

    assert none["source"] == "not_requested"
    assert weak["source"] == "mismatch"
    assert strong["source"] == "matched"


def test_weak_source_never_adds_source_refs_to_finding() -> None:
    workbench = build_finding_workbench(
        core_document=_core(),
        source_context=_weak_context_with_path_marker(),
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
    )

    assert workbench["findings"][0]["source_ref_ids"] == []
    assert "private/Startup.kt" not in json.dumps(workbench)
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_workbench.py \
  -k 'locator or capability or weak_source'
```

Expected: FAIL because locator and capability helpers do not exist or do not return the required shape.

- [ ] **Step 3: Implement exact locator and capability helpers**

Add:

```python
def build_capabilities(
    *,
    core_document: Mapping[str, object],
    source_context: Mapping[str, object] | None,
) -> dict[str, str]:
    source = "not_requested"
    if source_context is not None:
        source = "matched" if source_context.get("match_summary") == "strong" else "mismatch"
    return {
        "trace": "available",
        "smartperfetto": "available",
        "source": source,
        "ai": "pending",
    }


def _locator(evidence: Mapping[str, object]) -> dict[str, object]:
    start_ns = evidence.get("interval_start_ns")
    end_ns = evidence.get("interval_end_ns")
    fields = evidence.get("fields")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int) or end_ns < start_ns:
        raise ValueError("finding workbench input is invalid")
    if not isinstance(fields, Mapping):
        raise ValueError("finding workbench input is invalid")
    return {
        "start_ns": start_ns,
        "end_ns": end_ns,
        "process": fields.get("process"),
        "thread": fields.get("thread"),
        "track": fields.get("track"),
        "slice": fields.get("slice"),
        "query_id": evidence.get("query_id"),
    }
```

`_merge_findings` may copy `source_ref_ids` only when `source_context.match_summary == "strong"` and the referenced fragment links the same Finding and Evidence IDs.

- [ ] **Step 4: Run and verify GREEN**

Run the command from Step 2, then the complete file:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_workbench.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/reports/finding_workbench.py \
  services/api/tests/unit/test_finding_workbench.py
git commit -m "feat: bind findings to trace evidence"
```

### Task 4: Add exact 1.3/2.1 schemas and semantic closure

**Files:**
- Modify: `contracts/v1/reports/analysis-report.schema.json`
- Modify: `contracts/v1/ai/analysis-projection.schema.json`
- Modify: `contracts/v1/ai/synthesis-output.schema.json`
- Modify: `services/api/src/perfpilot_api/reports/semantics.py`
- Modify: `services/api/tests/contract/test_ai_report_contracts.py`

- [ ] **Step 1: Add semantic negative tests**

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_primary_finding",
        "evidence_without_locator",
        "score_out_of_range",
        "duplicate_stable_binding",
        "weak_source_ref",
        "metric_missing_evidence",
    ],
)
def test_v13_report_rejects_invalid_workbench_semantics(mutation: str) -> None:
    report = _example("analysis-report-v1.3.valid.json")
    _mutate_v13_report(report, mutation)

    with pytest.raises(ValueError, match="^report contract is invalid$"):
        validate_contract("analysis-report", report)


def test_v21_synthesis_cannot_override_server_priority_or_evidence() -> None:
    synthesis = _example("synthesis-output-v2.1.valid.json")
    synthesis["conclusions"][0]["priority_score"] = 100

    with pytest.raises(ValueError, match="^report contract is invalid$"):
        validate_contract("synthesis-output", synthesis)
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/contract/test_ai_report_contracts.py \
  -k 'v13_report or v21_synthesis or finding_workbench'
```

Expected: FAIL because the schemas and 1.3/2.1 semantic dispatch do not exist.

- [ ] **Step 3: Add version branches and closed definitions**

Add exact `if/then` branches for `schema_version == "1.3"` and `schema_version == "2.1"`. The new `$defs` must close these shapes:

```json
{
  "capabilitiesV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["trace", "smartperfetto", "source", "ai"],
    "properties": {
      "trace": {"enum": ["available", "unavailable"]},
      "smartperfetto": {"enum": ["available", "failed"]},
      "source": {"enum": ["matched", "mismatch", "unavailable", "not_requested"]},
      "ai": {"enum": ["available", "deterministic_fallback"]}
    }
  },
  "projectionCapabilitiesV21": {
    "type": "object",
    "additionalProperties": false,
    "required": ["trace", "smartperfetto", "source", "ai"],
    "properties": {
      "trace": {"enum": ["available", "unavailable"]},
      "smartperfetto": {"enum": ["available", "failed"]},
      "source": {"enum": ["matched", "mismatch", "unavailable", "not_requested"]},
      "ai": {"const": "pending"}
    }
  },
  "traceLocatorV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["start_ns", "end_ns", "process", "thread", "track", "slice", "query_id"],
    "properties": {
      "start_ns": {"type": "integer", "minimum": 0},
      "end_ns": {"type": "integer", "minimum": 0},
      "process": {"type": ["string", "null"], "maxLength": 255},
      "thread": {"type": ["string", "null"], "maxLength": 255},
      "track": {"type": ["string", "null"], "maxLength": 255},
      "slice": {"type": ["string", "null"], "maxLength": 255},
      "query_id": {"type": ["string", "null"], "maxLength": 128}
    }
  },
  "findingConfidenceV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["data_completeness", "evidence_grade", "attribution", "statistical"],
    "properties": {
      "data_completeness": {"enum": ["complete", "limited", "insufficient"]},
      "evidence_grade": {"enum": ["E0", "E1", "E2", "E3", "E4"]},
      "attribution": {"enum": ["low", "medium", "high"]},
      "statistical": {"enum": ["single_sample", "limited_samples", "supported"]}
    }
  },
  "workbenchMetricV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["metric_id", "name", "value", "unit", "aggregation", "scenario_type", "source", "evidence_ids", "quality"],
    "properties": {
      "metric_id": {"$ref": "#/$defs/sourceUuid"},
      "name": {"type": "string", "minLength": 1, "maxLength": 128},
      "value": {"type": ["number", "string", "null"]},
      "unit": {"type": ["string", "null"], "maxLength": 32},
      "aggregation": {"enum": ["single_sample", "min", "max", "mean", "median", "p95"]},
      "scenario_type": {"enum": ["startup", "scroll", "memory_cycle", "other"]},
      "source": {"type": "string", "minLength": 1, "maxLength": 128},
      "evidence_ids": {"type": "array", "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "quality": {"enum": ["available", "unavailable", "not_collected"]}
    }
  },
  "workbenchEvidenceV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["evidence_id", "kind", "scenario_type", "metric_ids", "summary", "source", "locator"],
    "properties": {
      "evidence_id": {"$ref": "#/$defs/sourceUuid"},
      "kind": {"enum": ["trace_interval", "metric", "source", "exclusion"]},
      "scenario_type": {"enum": ["startup", "scroll", "memory_cycle", "other"]},
      "metric_ids": {"type": "array", "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
      "source": {"type": "string", "minLength": 1, "maxLength": 128},
      "locator": {"$ref": "#/$defs/traceLocatorV13"}
    }
  },
  "workbenchFindingV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["finding_id", "scenario_type", "title", "problem", "impact", "mechanism", "root_cause", "critical_path_contribution", "priority", "priority_score", "evidence_ids", "metric_ids", "source_ref_ids", "status", "confidence", "confirmed_items", "unconfirmed_items", "retest_plan_id"],
    "properties": {
      "finding_id": {"$ref": "#/$defs/sourceUuid"},
      "scenario_type": {"enum": ["startup", "scroll", "memory_cycle", "other"]},
      "title": {"type": "string", "minLength": 1, "maxLength": 255},
      "problem": {"type": "string", "minLength": 1, "maxLength": 2000},
      "impact": {"type": "string", "minLength": 1, "maxLength": 2000},
      "mechanism": {"type": "string", "minLength": 1, "maxLength": 2000},
      "root_cause": {"type": "string", "minLength": 1, "maxLength": 2000},
      "critical_path_contribution": {"type": "number", "minimum": 0, "maximum": 1},
      "priority": {"enum": ["p0", "p1", "p2", "p3"]},
      "priority_score": {"type": "integer", "minimum": 0, "maximum": 100},
      "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "metric_ids": {"type": "array", "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "source_ref_ids": {"type": "array", "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "status": {"enum": ["confirmed", "hypothesis", "resolved", "improved", "unchanged", "regressed", "new"]},
      "confidence": {"$ref": "#/$defs/findingConfidenceV13"},
      "confirmed_items": {"type": "array", "maxItems": 20, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
      "unconfirmed_items": {"type": "array", "maxItems": 20, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
      "retest_plan_id": {"$ref": "#/$defs/sourceUuid"}
    }
  },
  "retestPlanV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["retest_plan_id", "finding_id", "scenario_type", "package_name", "duration_seconds", "environment_fingerprint", "metric_ids", "pass_criteria", "notes"],
    "properties": {
      "retest_plan_id": {"$ref": "#/$defs/sourceUuid"},
      "finding_id": {"$ref": "#/$defs/sourceUuid"},
      "scenario_type": {"enum": ["startup", "scroll", "memory_cycle", "other"]},
      "package_name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*(?:\\.[A-Za-z][A-Za-z0-9_]*)+$", "maxLength": 255},
      "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
      "environment_fingerprint": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
      "metric_ids": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "pass_criteria": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
      "notes": {"type": "string", "minLength": 1, "maxLength": 2000}
    }
  },
  "criticalPathSegmentV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["segment_id", "label", "start_ns", "end_ns", "duration_ns", "evidence_ids"],
    "properties": {
      "segment_id": {"$ref": "#/$defs/sourceUuid"},
      "label": {"type": "string", "minLength": 1, "maxLength": 255},
      "start_ns": {"type": "integer", "minimum": 0},
      "end_ns": {"type": "integer", "minimum": 0},
      "duration_ns": {"type": "integer", "minimum": 0},
      "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}}
    }
  },
  "findingWorkbenchV13": {
    "type": "object",
    "additionalProperties": false,
    "required": ["critical_path", "metrics", "evidence", "findings", "primary_finding_ids", "retest_plans"],
    "properties": {
      "critical_path": {"type": "array", "maxItems": 64, "items": {"$ref": "#/$defs/criticalPathSegmentV13"}},
      "metrics": {"type": "array", "maxItems": 256, "items": {"$ref": "#/$defs/workbenchMetricV13"}},
      "evidence": {"type": "array", "maxItems": 512, "items": {"$ref": "#/$defs/workbenchEvidenceV13"}},
      "findings": {"type": "array", "maxItems": 128, "items": {"$ref": "#/$defs/workbenchFindingV13"}},
      "primary_finding_ids": {"type": "array", "maxItems": 3, "uniqueItems": true, "items": {"$ref": "#/$defs/sourceUuid"}},
      "retest_plans": {"type": "array", "maxItems": 128, "items": {"$ref": "#/$defs/retestPlanV13"}}
    }
  }
}
```

The synthesis 2.1 schema must not contain server-owned `priority_score`, `Evidence`, `Metric`, `locator`, or capability fields.

- [ ] **Step 4: Add 1.3/2.1 semantic dispatch and cross-reference checks**

Extend `validate_source_aware_semantics`:

```python
if name == "analysis-projection" and schema_version == "2.1":
    _validate_workbench_document(document)
    _validate_projection(document)
    return
if name == "synthesis-output" and schema_version == "2.1":
    _validate_synthesis_v21(document)
    return
if name == "analysis-report" and schema_version == "1.3":
    _validate_workbench_document(document)
    _validate_report_v13(document)
    return
```

Implement the semantic closure directly:

```python
def _validate_workbench_document(document: dict[str, object]) -> None:
    capabilities = document.get("capabilities")
    workbench = document.get("workbench")
    if not isinstance(capabilities, dict) or not isinstance(workbench, dict):
        raise SourceAwareSemanticError
    metrics = workbench.get("metrics")
    evidence = workbench.get("evidence")
    findings = workbench.get("findings")
    retests = workbench.get("retest_plans")
    primary = workbench.get("primary_finding_ids")
    if not all(isinstance(value, list) for value in (metrics, evidence, findings, retests, primary)):
        raise SourceAwareSemanticError
    metric_ids = _unique_ids(metrics, "metric_id")
    evidence_ids = _unique_ids(evidence, "evidence_id")
    finding_ids = _unique_ids(findings, "finding_id")
    retest_ids = _unique_ids(retests, "retest_plan_id")
    for item in evidence:
        locator = item.get("locator") if isinstance(item, dict) else None
        if (
            not isinstance(locator, dict)
            or not isinstance(locator.get("start_ns"), int)
            or not isinstance(locator.get("end_ns"), int)
            or locator["end_ns"] < locator["start_ns"]
            or not set(item.get("metric_ids", [])).issubset(metric_ids)
        ):
            raise SourceAwareSemanticError
    eligible_primary: list[object] = []
    for item in findings:
        if not isinstance(item, dict):
            raise SourceAwareSemanticError
        if not set(item.get("evidence_ids", [])).issubset(evidence_ids):
            raise SourceAwareSemanticError
        if not set(item.get("metric_ids", [])).issubset(metric_ids):
            raise SourceAwareSemanticError
        if item.get("retest_plan_id") not in retest_ids:
            raise SourceAwareSemanticError
        source_refs = item.get("source_ref_ids")
        if capabilities.get("source") != "matched" and source_refs:
            raise SourceAwareSemanticError
        if item.get("status") == "confirmed" and item.get("priority") in {"p0", "p1"}:
            eligible_primary.append(item.get("finding_id"))
    expected_primary = eligible_primary[:3]
    if primary != expected_primary or not set(primary).issubset(finding_ids):
        raise SourceAwareSemanticError


def _unique_ids(items: list[object], field: str) -> set[object]:
    values = [item.get(field) for item in items if isinstance(item, dict)]
    if len(values) != len(items) or len(values) != len(set(values)):
        raise SourceAwareSemanticError
    return set(values)
```

The workbench builder sorts findings by `(-priority_score, finding_id)`, so `eligible_primary[:3]` verifies the server-owned stable order. Add the same cross-reference check to 2.1 projections and 1.3 reports.

- [ ] **Step 5: Run complete contract gates and commit**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/contract/test_ai_report_contracts.py \
  services/api/tests/unit/test_finding_workbench.py
.venv/bin/ruff check services/api/src/perfpilot_api/reports services/api/tests/contract services/api/tests/unit/test_finding_workbench.py
git diff --check
```

Expected: all tests pass; Ruff and diff check are clean.

```bash
git add contracts/v1 services/api/src/perfpilot_api/reports/semantics.py \
  services/api/tests/contract/test_ai_report_contracts.py
git commit -m "feat: close finding workbench contracts"
```

### Task 5: Project the deterministic workbench into AI 2.1 input

**Files:**
- Modify: `services/api/src/perfpilot_api/reports/projection.py`
- Modify: `services/api/tests/unit/test_ai_projection.py`

- [ ] **Step 1: Add failing projection tests**

```python
def test_projection_v21_contains_server_owned_workbench() -> None:
    projection = build_ai_projection(
        _normalized_report(),
        analysis_profile="startup",
        question=None,
        source_context=_strong_source_context(),
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
        schema_version="2.1",
    )

    document = projection.document
    assert document["schema_version"] == "2.1"
    assert document["capabilities"]["source"] == "matched"
    assert len(document["workbench"]["primary_finding_ids"]) <= 3
    assert document["workbench"]["evidence"][0]["locator"]["start_ns"] >= 0


def test_projection_v20_remains_byte_stable() -> None:
    projection = build_ai_projection(
        _normalized_report(),
        analysis_profile="startup",
        question=None,
        source_context=None,
        package_name="com.rivotek.mediacenter",
        duration_seconds=15,
        environment_fingerprint=ENVIRONMENT_FINGERPRINT,
        schema_version="2.0",
    )

    assert projection.canonical_bytes == _expected_v20_projection_bytes()
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_ai_projection.py \
  -k 'v21_contains or v20_remains'
```

Expected: FAIL because `build_ai_projection` does not accept `schema_version="2.1"` and does not project workbench data.

- [ ] **Step 3: Implement the version-gated projection**

Add an explicit keyword argument with a closed literal:

```python
def build_ai_projection(
    core: NormalizedTraceReport,
    *,
    analysis_profile: Literal["auto", "startup", "scroll"],
    question: str | None,
    source_context: dict[str, object] | None,
    package_name: str | None = None,
    duration_seconds: int | None = None,
    environment_fingerprint: str | None = None,
    schema_version: Literal["2.0", "2.1"] = "2.0",
    max_bytes: int = 256 * 1024,
) -> AIProjection:
    legacy = _build_v20_projection_from_validated_inputs(
        core=core,
        analysis_profile=analysis_profile,
        question=question,
        source_context=source_context,
        max_bytes=max_bytes,
    )
    if schema_version == "2.0":
        return legacy
    if (
        not isinstance(package_name, str)
        or not package_name
        or not isinstance(duration_seconds, int)
        or not 1 <= duration_seconds <= 3600
        or not isinstance(environment_fingerprint, str)
        or not environment_fingerprint.startswith("sha256:")
    ):
        raise ProjectionPrivacyError
    document = {
        **legacy.document,
        "schema_version": "2.1",
        "capabilities": build_capabilities(
            core_document=core.document,
            source_context=source_context,
        ),
        "workbench": build_finding_workbench(
            core_document=core.document,
            source_context=source_context,
            package_name=package_name,
            duration_seconds=duration_seconds,
            environment_fingerprint=environment_fingerprint,
        ),
    }
    canonical = canonical_json_bytes(validate_contract("analysis-projection", document))
    if len(canonical) > max_bytes:
        raise ProjectionSizeError
    return AIProjection(canonical_bytes=canonical, sha256_b64=_checksum(canonical))
```

Extract the current `build_ai_projection` body without changes into `_build_v20_projection_from_validated_inputs`; the existing `_checksum` helper remains the only checksum implementation. The legacy byte-stability test must continue to pass.

- [ ] **Step 4: Run full projection and contract tests**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_finding_workbench.py \
  services/api/tests/contract/test_ai_report_contracts.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/reports/projection.py \
  services/api/tests/unit/test_ai_projection.py
git commit -m "feat: project finding workbench evidence"
```

## Plan 1 completion gate

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/contract/test_ai_report_contracts.py \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_finding_workbench.py \
  services/api/tests/unit/test_smartperfetto_report_normalizer.py
.venv/bin/ruff check services/api/src/perfpilot_api/reports services/api/tests/contract services/api/tests/unit
git diff --check
```

Expected: all selected tests pass, no Ruff errors, and no whitespace errors.
