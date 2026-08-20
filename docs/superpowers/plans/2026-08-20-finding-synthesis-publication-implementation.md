# Finding Synthesis and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate evidence-bound Chinese Finding explanations, provide a deterministic Chinese fallback, and atomically publish `AnalysisReport 1.3` without weakening source privacy or lifecycle guarantees.

**Architecture:** AI receives only the validated 2.1 projection and returns explanation fields and recommendations referencing server-owned IDs. A new deterministic fallback renders the same synthesis shape from the workbench when both AI attempts are invalid. A focused report composer converts the projection plus synthesis into 1.3 while the existing writer keeps 1.0–1.2 behavior unchanged.

**Tech Stack:** Python 3.12, JSON Schema, existing OpenAI-compatible provider interface, pytest, canonical JSON, SQLAlchemy report repository, Ruff.

---

## File responsibility map

- Create `services/api/src/perfpilot_api/ai/finding_fallback.py`: deterministic Chinese `Synthesis 2.1` generator.
- Create `services/api/tests/unit/test_finding_fallback.py`: deterministic wording, references, source privacy, and size tests.
- Create `services/api/src/perfpilot_api/reports/finding_report.py`: compose `AnalysisReport 1.3` from validated artifacts.
- Create `services/api/tests/unit/test_finding_report.py`: 1.3 composition and state invariants.
- Modify `services/api/src/perfpilot_api/ai/synthesis.py`: validate 2.1 references and four-part Chinese narratives.
- Modify `services/api/tests/unit/test_ai_synthesis_validator.py`: 2.1 validation and privacy tests.
- Create `services/api/src/perfpilot_api/ai/prompts/perfpilot-finding-workbench-v1.txt`: 2.1 prompt.
- Modify `services/api/src/perfpilot_api/reports/writer.py`: version-gated 1.3 composition call only.
- Modify `services/api/tests/unit/test_analysis_report_writer.py`: legacy unchanged and 1.3 publishing.
- Modify `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`: request 2.1, one retry, fallback, cancellation fences.
- Modify `services/api/src/perfpilot_api/workers/synthesis_runtime.py`: production wiring.
- Modify `services/api/src/perfpilot_api/local_app.py`: local Trace upload wiring and persisted resume.
- Modify `services/api/src/perfpilot_api/db/tenant/models/apps.py`: persist trace-upload package and duration used by RetestPlan.
- Create `services/api/migrations/tenant/versions/0010_finding_retest_context.py`: add closed retest context columns.
- Modify `services/api/src/perfpilot_api/api/analyses.py`: persist package and duration for production trace uploads.
- Modify `services/api/tests/integration/test_migrations.py`: tenant migration round-trip.
- Modify `services/api/tests/unit/test_synthesis_worker.py`: retry/fallback/cancel.
- Modify `services/api/tests/integration/test_local_app.py`: 1.3 local upload lifecycle.

### Task 1: Validate `Synthesis 2.1` against server-owned Finding data

**Files:**
- Modify: `services/api/src/perfpilot_api/ai/synthesis.py`
- Modify: `services/api/tests/unit/test_ai_synthesis_validator.py`

- [ ] **Step 1: Add failing 2.1 validator tests**

```python
def test_v21_requires_four_part_chinese_explanation_for_every_primary_finding() -> None:
    projection = _projection_v21()
    candidate = _candidate_v21()

    validated = validate_synthesis_output(projection=projection, candidate=candidate)

    assert validated.document["schema_version"] == "2.1"
    assert [item["finding_id"] for item in validated.document["conclusions"]] == (
        projection.document["workbench"]["primary_finding_ids"]
    )


def test_v21_rejects_ai_owned_priority_score_and_locator() -> None:
    candidate = _candidate_v21()
    candidate["conclusions"][0]["priority_score"] = 100
    candidate["conclusions"][0]["locator"] = {"start_ns": 1, "end_ns": 2}

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        validate_synthesis_output(projection=_projection_v21(), candidate=candidate)


def test_v21_rejects_unknown_evidence_and_retest_metrics() -> None:
    candidate = _candidate_v21()
    candidate["conclusions"][0]["evidence_ids"] = [UNKNOWN_ID]
    candidate["retest_plan"][0]["metric_ids"] = [UNKNOWN_ID]

    with pytest.raises(ValueError, match="^AI synthesis output is invalid$"):
        validate_synthesis_output(projection=_projection_v21(), candidate=candidate)
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  -k 'v21_requires or v21_rejects'
```

Expected: FAIL because synthesis validation only indexes projection 2.0 and does not enforce 2.1 workbench references.

- [ ] **Step 3: Extend the projection index and semantic validator**

Extend `_ProjectionIndex` with these fields:

```python
workbench_finding_ids: frozenset[str]
primary_finding_ids: tuple[str, ...]
workbench_evidence_ids: frozenset[str]
retest_metric_ids: Mapping[str, frozenset[str]]
```

Add a version-gated validator:

```python
def _validate_v21(document: dict[str, object], index: _ProjectionIndex) -> None:
    conclusions = document.get("conclusions")
    if not isinstance(conclusions, list):
        raise SynthesisValidationError
    conclusion_ids = tuple(
        str(item.get("finding_id"))
        for item in conclusions
        if isinstance(item, dict)
    )
    if conclusion_ids != index.primary_finding_ids:
        raise SynthesisValidationError
    for conclusion in conclusions:
        if not isinstance(conclusion, dict):
            raise SynthesisValidationError
        finding_id = conclusion.get("finding_id")
        evidence_ids = conclusion.get("evidence_ids")
        if finding_id not in index.workbench_finding_ids or not _known_ids(
            evidence_ids, index.workbench_evidence_ids
        ):
            raise SynthesisValidationError
    _validate_conclusions(document, index)
    _validate_source_fixes(document, index)
```

Call `_validate_v21` only when both projection and candidate are version 2.1. Preserve the existing 2.0 branch byte-for-byte.

- [ ] **Step 4: Run full synthesis validator tests**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_ai_synthesis_validator.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/ai/synthesis.py \
  services/api/tests/unit/test_ai_synthesis_validator.py
git commit -m "feat: validate finding synthesis output"
```

### Task 2: Add the Finding-focused Chinese prompt

**Files:**
- Create: `services/api/src/perfpilot_api/ai/prompts/perfpilot-finding-workbench-v1.txt`
- Modify: `services/api/tests/unit/test_synthesis_worker.py`

- [ ] **Step 1: Add a failing prompt contract test**

```python
def test_finding_prompt_forbids_new_facts_and_requires_clear_chinese_structure() -> None:
    prompt = load_prompt_template("perfpilot-finding-workbench-v1")

    assert "不得创建新的 finding_id、evidence_id、metric_id" in prompt
    assert "问题点" in prompt
    assert "为什么会有这个问题" in prompt
    assert "结合源码判断的根因" in prompt
    assert "修改建议" in prompt
    assert "只能引用 projection.workbench" in prompt
    assert "主要叙述使用中文" in prompt
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_synthesis_worker.py \
  -k finding_prompt
```

Expected: FAIL because the prompt file does not exist.

- [ ] **Step 3: Create the complete prompt**

The prompt file must contain this exact policy section before the JSON schema instructions:

```text
你是 PerfPilot 的性能分析解释器。projection.workbench 是唯一事实来源。

必须遵守：
1. 不得创建新的 finding_id、evidence_id、metric_id、source_ref_id 或数值。
2. 只能引用 projection.workbench 中存在并且关联闭合的 ID。
3. 每条主要问题固定回答：问题点、为什么会有这个问题、结合源码判断的根因、修改建议。
4. 主要叙述使用中文；Android、Binder、Jank、Perfetto、SQL 等定义明确的术语可以保留英文。
5. source capability 不是 matched 时，不得输出文件路径、类名、方法名、行号或 Diff。
6. 修改建议必须说明预期收益范围、改动成本、风险和复测指标；不能承诺未经证据支持的精确收益。
7. 不得把 unavailable、not_collected 或 missing 写成 0，也不得写成“没有问题”。
8. 只返回一个符合 Synthesis 2.1 的 JSON 对象，不返回 Markdown 包裹或解释文字。
```

- [ ] **Step 4: Run and verify GREEN, then commit**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_synthesis_worker.py -k finding_prompt
git add services/api/src/perfpilot_api/ai/prompts/perfpilot-finding-workbench-v1.txt \
  services/api/tests/unit/test_synthesis_worker.py
git commit -m "feat: prompt finding-centered Chinese reports"
```

Expected: PASS and a single prompt commit.

### Task 3: Implement deterministic Chinese fallback

**Files:**
- Create: `services/api/src/perfpilot_api/ai/finding_fallback.py`
- Create: `services/api/tests/unit/test_finding_fallback.py`

- [ ] **Step 1: Write failing deterministic fallback tests**

```python
def test_fallback_is_deterministic_and_references_only_projection_ids() -> None:
    projection = _projection_v21()

    first = build_deterministic_finding_synthesis(projection)
    second = build_deterministic_finding_synthesis(projection)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.document["schema_version"] == "2.1"
    assert [item["finding_id"] for item in first.document["conclusions"]] == (
        projection.document["workbench"]["primary_finding_ids"]
    )
    assert "问题" in first.document["executive_summary"]


def test_fallback_never_leaks_weak_source_locations() -> None:
    projection = _weak_source_projection_with_private_marker()

    result = build_deterministic_finding_synthesis(projection)

    serialized = result.canonical_bytes.decode("utf-8")
    assert "private/Startup.kt" not in serialized
    assert result.document["source_fixes"] == []
    assert all(not item["source_ref_ids"] for item in result.document["conclusions"])
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_fallback.py
```

Expected: collection ERROR with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the fallback builder**

Create:

```python
from __future__ import annotations

from perfpilot_api.ai.synthesis import AISynthesisOutput, validate_synthesis_output
from perfpilot_api.reports.projection import AIProjection
from uuid import UUID, uuid5


_FALLBACK_NAMESPACE = UUID("a9216c29-b0af-47c2-99b5-289d6ec8e455")


def build_deterministic_finding_synthesis(
    projection: AIProjection,
) -> AISynthesisOutput:
    document = projection.document
    workbench = document["workbench"]
    finding_by_id = {
        item["finding_id"]: item
        for item in workbench["findings"]
    }
    conclusions = []
    for finding_id in workbench["primary_finding_ids"]:
        finding = finding_by_id[finding_id]
        conclusions.append(
            {
                "finding_id": finding_id,
                "evidence_ids": list(finding["evidence_ids"]),
                "source_ref_ids": list(finding["source_ref_ids"]),
                "problem": str(finding["problem"]),
                "cause": str(finding["mechanism"]),
                "source_root_cause": _source_root_cause(document, finding),
                "recommendation": _recommendation(finding),
            }
        )
    candidate = {
        "schema_version": "2.1",
        "verdict": "分析完成",
        "executive_summary": _executive_summary(conclusions),
        "key_metric_ids": _key_metric_ids(workbench),
        "conclusions": conclusions,
        "top_findings": _top_findings(conclusions),
        "recommendations": _recommendations(conclusions, workbench),
        "source_fixes": [],
        "retest_plan": _retest_plan(workbench),
        "limitations": _limitations(document),
    }
    return validate_synthesis_output(projection=projection, candidate=candidate)
```

Use these exact deterministic helper rules:

```python
def _source_root_cause(document: dict[str, object], finding: dict[str, object]) -> str:
    capabilities = document["capabilities"]
    if capabilities["source"] != "matched" or not finding["source_ref_ids"]:
        return "本次没有经过验证的匹配源码，暂不能定位到具体文件或实现。"
    return str(finding["root_cause"])


def _executive_summary(conclusions: list[dict[str, object]]) -> str:
    return (
        f"本次分析确认 {len(conclusions)} 个主要性能问题。"
        "以下结论均来自 SmartPerfetto 指标和可定位 Trace 证据。"
    )


def _key_metric_ids(workbench: dict[str, object]) -> list[str]:
    primary = set(workbench["primary_finding_ids"])
    findings = [item for item in workbench["findings"] if item["finding_id"] in primary]
    return sorted({metric_id for item in findings for metric_id in item["metric_ids"]})[:3]


def _top_findings(conclusions: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "finding_id": item["finding_id"],
            "evidence_ids": list(item["evidence_ids"]),
            "user_impact": str(item["problem"]),
        }
        for item in conclusions
    ]


def _recommendations(
    conclusions: list[dict[str, object]],
    workbench: dict[str, object],
) -> list[dict[str, object]]:
    priority_by_id = {
        item["finding_id"]: item["priority"]
        for item in workbench["findings"]
    }
    return [
        {
            "priority": priority_by_id[item["finding_id"]],
            "title": str(item["problem"]),
            "action": str(item["recommendation"]),
            "expected_effect": "按对应复测指标确认改善幅度。",
            "finding_ids": [item["finding_id"]],
            "evidence_ids": list(item["evidence_ids"]),
        }
        for item in conclusions
    ]


def _retest_plan(workbench: dict[str, object]) -> list[dict[str, object]]:
    primary = set(workbench["primary_finding_ids"])
    return [
        {
            "mode": "verify_metric",
            "scenario_type": item["scenario_type"],
            "metric_ids": list(item["metric_ids"]),
            "limitation_ids": [],
            "steps": str(item["notes"]),
            "success_condition": "improve_from_baseline",
            "failure_condition": "threshold_missed",
        }
        for item in workbench["retest_plans"]
        if item["finding_id"] in primary
    ]


def _limitations(document: dict[str, object]) -> list[dict[str, object]]:
    source = document["capabilities"]["source"]
    if source == "matched":
        return []
    return [{"limitation_id": str(uuid5(_FALLBACK_NAMESPACE, f"source:{source}")), "summary": "源码未匹配，本次不提供文件位置或 Diff。"}]
```

These helpers read only fields already present in `projection.document`. They never interpolate a path or symbol unless `capabilities.source == "matched"` and the referenced source ref is strong.

- [ ] **Step 4: Run validator, privacy, and size tests**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_fallback.py \
  services/api/tests/unit/test_ai_synthesis_validator.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/ai/finding_fallback.py \
  services/api/tests/unit/test_finding_fallback.py
git commit -m "feat: add deterministic Chinese report fallback"
```

### Task 4: Compose and validate `AnalysisReport 1.3`

**Files:**
- Create: `services/api/src/perfpilot_api/reports/finding_report.py`
- Create: `services/api/tests/unit/test_finding_report.py`
- Modify: `services/api/src/perfpilot_api/reports/writer.py`
- Modify: `services/api/tests/unit/test_analysis_report_writer.py`

- [ ] **Step 1: Add failing composer tests**

```python
def test_composer_builds_v13_from_validated_projection_and_synthesis() -> None:
    result = compose_finding_report(
        base_report=_base_v12_report(),
        projection=_projection_v21().document,
        synthesis=_synthesis_v21().document,
        report_version=3,
    )

    assert result["schema_version"] == "1.3"
    assert result["state"] == "completed"
    assert result["capabilities"]["ai"] == "available"
    assert result["workbench"] == _projection_v21().document["workbench"]
    assert result["synthesis"]["output"]["schema_version"] == "2.1"


def test_composer_marks_deterministic_fallback_without_partial_state() -> None:
    result = compose_finding_report(
        base_report=_base_v12_report(),
        projection=_projection_v21().document,
        synthesis=_fallback_v21().document,
        report_version=3,
        ai_mode="deterministic_fallback",
    )

    assert result["state"] == "completed"
    assert result["capabilities"]["ai"] == "deterministic_fallback"
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_report.py
```

Expected: collection ERROR because `finding_report.py` does not exist.

- [ ] **Step 3: Implement the focused composer**

```python
def compose_finding_report(
    *,
    base_report: dict[str, object],
    projection: dict[str, object],
    synthesis: dict[str, object],
    report_version: int,
    ai_mode: Literal["available", "deterministic_fallback"] = "available",
) -> dict[str, object]:
    validated_projection = validate_contract("analysis-projection", projection)
    validated_synthesis = validate_contract("synthesis-output", synthesis)
    if validated_projection["schema_version"] != "2.1":
        raise FindingReportError
    if validated_synthesis["schema_version"] != "2.1":
        raise FindingReportError
    document = {
        **base_report,
        "schema_version": "1.3",
        "report_version": report_version,
        "state": "completed",
        "capabilities": {
            **validated_projection["capabilities"],
            "ai": ai_mode,
        },
        "workbench": validated_projection["workbench"],
        "synthesis": {
            **base_report["synthesis"],
            "state": "completed",
            "output": validated_synthesis,
            "failure_code": None,
        },
    }
    return validate_contract("analysis-report", document)
```

The composer must deep-copy inputs through canonical validation and must never mutate the 1.2 base document.

- [ ] **Step 4: Version-gate the existing writer**

Add `report_schema_version: Literal["1.2", "1.3"] = "1.2"` to `AnalysisReportWriteRequest`. Call `compose_finding_report` only for 1.3. Keep the existing 1.0–1.2 path unchanged and assert it with the current byte/hash tests.

- [ ] **Step 5: Run writer tests and commit**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_finding_report.py \
  services/api/tests/unit/test_analysis_report_writer.py
git add services/api/src/perfpilot_api/reports/finding_report.py \
  services/api/src/perfpilot_api/reports/writer.py \
  services/api/tests/unit/test_finding_report.py \
  services/api/tests/unit/test_analysis_report_writer.py
git commit -m "feat: publish finding workbench reports"
```

Expected: PASS; legacy writer hashes remain unchanged.

### Task 5: Wire one retry, deterministic fallback, cancellation, and resume

**Files:**
- Modify: `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/workers/synthesis_runtime.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/src/perfpilot_api/db/tenant/models/apps.py`
- Create: `services/api/migrations/tenant/versions/0010_finding_retest_context.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/tests/integration/test_migrations.py`
- Modify: `services/api/tests/unit/test_synthesis_worker.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Add failing lifecycle tests**

```python
async def test_two_invalid_ai_candidates_publish_deterministic_v13_report() -> None:
    provider = FakeProvider([_invalid_candidate(), _invalid_candidate()])

    report = await _run_v21_synthesis(provider=provider)

    assert provider.calls == 2
    assert report["schema_version"] == "1.3"
    assert report["state"] == "completed"
    assert report["capabilities"]["ai"] == "deterministic_fallback"
    assert report["synthesis"]["output"]["conclusions"]


async def test_cancel_after_first_ai_attempt_prevents_fallback_publication() -> None:
    provider = BlockingProvider()
    run = asyncio.create_task(_run_v21_synthesis(provider=provider))
    await provider.started.wait()

    await _cancel_analysis()
    provider.release.set()

    with pytest.raises(asyncio.CancelledError):
        await run
    assert await _latest_report() is None


def test_restart_resumes_v21_from_persisted_projection_without_smartperfetto_repeat() -> None:
    first = _runtime_that_stops_after_projection()
    first.run_until_projection_persisted()

    second = _reopened_runtime()
    report = second.wait_for_report()

    assert second.smartperfetto_submissions == 0
    assert report["schema_version"] == "1.3"
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_synthesis_worker.py \
  services/api/tests/integration/test_local_app.py \
  -k 'deterministic_v13 or prevents_fallback or resumes_v21'
```

Expected: FAIL because production still requests 2.0/1.2 and treats two invalid AI candidates as failed synthesis.

- [ ] **Step 3: Implement the bounded retry and fallback boundary**

In `SynthesisPipeline._advance`, replace only the exhausted `SynthesisValidationError` branch with this exact control flow while preserving the existing durable `begin_invocation` / `finish_invocation_failure` boundaries:

```python
try:
    validated = validate_synthesis_output(
        projection=projection,
        candidate=candidate.candidate_json,
    )
    ai_mode = "available"
except SynthesisValidationError:
    retry = attempt < 2
    if retry:
        await self._repository.finish_invocation_failure(
            team_id=record.team_id,
            analysis_id=record.analysis_id,
            execution_id=record.id,
            attempt_number=attempt,
            stable_error_code="ai_output_invalid",
            latency_ms=candidate.latency_ms,
            exhausted=False,
            generated_at=None,
            now=now,
            fence=fence,
        )
        return SynthesisStepResult("pending", 1)
    await self._assert_not_canceled(record)
    validated = self._fallback_builder(projection)
    ai_mode = "deterministic_fallback"
```

Persist `ai_mode` beside the bound candidate result so the final writer can distinguish provider output from deterministic fallback. Persist `projection_artifact_id`, `projection_sha256`, prompt version, generation, and cancellation state before the provider call. Recheck cancellation before fallback construction and before report publication.

- [ ] **Step 4: Wire local and production runtime to request 2.1/1.3**

Persist the retest context before wiring the pipeline. Add nullable `target_package_name` and `duration_seconds` columns; existing rows remain readable with both null, while the create API requires both for newly created Trace uploads. The migration begins with:

```python
revision = "0010_finding_retest_context"
down_revision = "0009_source_context_state"


def upgrade() -> None:
    op.add_column("analyses", sa.Column("target_package_name", sa.String(255)))
    op.add_column("analyses", sa.Column("duration_seconds", sa.Integer()))
    op.create_check_constraint(
        "ck_analyses_retest_context_pair",
        "analyses",
        "(target_package_name IS NULL AND duration_seconds IS NULL) OR "
        "(target_package_name IS NOT NULL AND duration_seconds BETWEEN 1 AND 3600)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analyses_retest_context_pair", "analyses", type_="check")
    op.drop_column("analyses", "duration_seconds")
    op.drop_column("analyses", "target_package_name")
```

Extend `SynthesisAnalysisContext`:

```python
@dataclass(frozen=True, slots=True)
class SynthesisAnalysisContext:
    analysis_profile: Literal["auto", "startup", "scroll"]
    question: str | None
    analysis_mode: Literal["trace_upload", "device"]
    package_name: str
    duration_seconds: int
    environment_fingerprint: str
```

For new trace uploads, `api/analyses.py` and `local_app.py` persist the request package and duration. `SQLAlchemySynthesisAnalysisContextRepository.load` computes `environment_fingerprint` from canonical JSON containing package, duration, analysis profile, normalizer version, engine ID, adapter version, source contract, and canonical artifact checksum; prefix the lowercase SHA-256 hex with `sha256:`.

Then add explicit constructor arguments to `SynthesisPipeline`:

```python
SynthesisPipeline(
    repository=repository,
    canonical_reader=canonical_reader,
    artifact_store=synthesis_artifacts,
    provider=provider,
    report_writer=AnalysisReportWriter(tenant_router=artifacts.tenant_router),
    analysis_contexts=analysis_contexts,
    memory_sources=memory_sources,
    source_contexts=source_contexts,
    parent_projector=parent_projector,
    projection_schema_version="2.1",
    report_schema_version="1.3",
    fallback_builder=build_deterministic_finding_synthesis,
    max_projection_bytes=settings.ai_max_projection_bytes,
)
```

Store these constructor values on `_projection_schema_version`, `_report_schema_version`, and `_fallback_builder`. Pass `schema_version=self._projection_schema_version`, `package_name=context.package_name`, `duration_seconds=context.duration_seconds`, and `environment_fingerprint=context.environment_fingerprint` into the existing projection builder. Pass `report_schema_version=self._report_schema_version` into `AnalysisReportWriteRequest`. The persisted-resume path must load and validate the exact projection artifact instead of rebuilding it source-less.

- [ ] **Step 5: Run lifecycle gates and commit**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_synthesis_worker.py \
  services/api/tests/unit/test_synthesis_worker_runtime.py \
  services/api/tests/unit/test_analysis_report_writer.py \
  services/api/tests/integration/test_local_app.py \
  -k 'synthesis or report or cancel or restart or v13'
.venv/bin/pytest -q services/api/tests/integration/test_migrations.py -k finding_retest_context
.venv/bin/ruff check services/api/src/perfpilot_api services/api/tests
git diff --check
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: orchestrate finding report publication"
```

Expected: all focused lifecycle tests pass; no late report is published after cancellation.

## Plan 2 completion gate

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  services/api/tests/unit/test_finding_fallback.py \
  services/api/tests/unit/test_finding_report.py \
  services/api/tests/unit/test_analysis_report_writer.py \
  services/api/tests/unit/test_synthesis_worker.py \
  services/api/tests/integration/test_synthesis_orchestrator.py \
  services/api/tests/integration/test_local_app.py \
  -k 'finding or synthesis or report or cancel or restart'
.venv/bin/ruff check services/api/src/perfpilot_api services/api/tests
git diff --check
```

Expected: all selected tests pass and static checks are clean.
