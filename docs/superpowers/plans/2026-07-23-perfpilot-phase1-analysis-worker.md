# PerfPilot Phase 1 Tracekit and Analysis Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing Android performance scripts into an installable, deterministic `tracekit` and a claim-based Worker that validates samples, analyzes startup/scroll/memory artifacts, and persists evidence-backed reports.

**Architecture:** `tracekit` owns immutable input models, Perfetto execution, SQL resources, per-scenario adapters, metrics, evidence rules, and a structured CLI. The Worker consumes opaque Redis events, obtains a claim from the private control API, downloads exact artifact versions through version-bound URLs, calls `tracekit` directly, and posts a versioned report before completing the claim. The API derives and owns every tenant route; the Worker never receives a database credential or bucket. The lightweight validator and full analyzer share the same trace-health and sample-validity functions.

**Tech Stack:** Python 3.12, uv workspace, Pydantic 2.13.x, Perfetto trace_processor v56.1, checked-in SQL resources, pytest, PostgreSQL, Redis Streams, S3-compatible storage.

---

## File map

- Create `tracekit/pyproject.toml`: installable package and resource declarations.
- Create `tracekit/src/tracekit/contracts.py`: `AnalysisBundle`, metric, finding, evidence, confidence, and provenance models.
- Create `tracekit/src/tracekit/perfetto/`: pinned tool resolver, query runner, trace health, and SQL resource loader.
- Create `tracekit/src/tracekit/adapters/`: startup, scroll, and memory adapters.
- Create `tracekit/src/tracekit/rules/`: versioned rules, evidence evaluation, and recommendation rendering.
- Create `tracekit/src/tracekit/cli.py`: stable JSON machine interface and legacy launcher.
- Create `tracekit/src/tracekit/resources/{sql,capture,perfetto}/`: migrated immutable resources.
- Create `tracekit/src/tracekit/legacy/`: compatibility-only report and Excel code.
- Create `tracekit/tests/{conftest,factories}.py`: deterministic Trace, bundle, rule, CLI fixtures, and value builders.
- Create `tracekit/tests/{unit,contract,integration,testdata}/`: adapters, SQL, CLI, and reproducibility tests.
- Create `services/trace-worker/pyproject.toml`: Worker package.
- Create `services/trace-worker/src/perfpilot_worker/`: control client, artifact workspace, consumers, persistence, and CLI.
- Create `services/trace-worker/tests/`: validator, claim, crash recovery, sandbox, and persistence tests.
- Create `services/trace-worker/Dockerfile`: network-restricted runtime with pre-fetched Perfetto binary.

## Task 1: Migrate the existing tools into an installable package

**Files:**
- Modify: `pyproject.toml`
- Create: `tracekit/pyproject.toml`
- Create: `tracekit/src/tracekit/__init__.py`
- Create: `tracekit/src/tracekit/resources/`
- Create: `tracekit/src/tracekit/legacy/`
- Create: `tracekit/tests/conftest.py`
- Create: `tracekit/tests/factories.py`
- Create: `tracekit/tests/unit/test_package_resources.py`
- Create: `tracekit/tests/integration/test_capture_serial.py`
- Create: `tracekit/tests/legacy/test_report_pipeline.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add `tracekit` to the uv workspace**

```toml
[tool.uv.workspace]
members = ["services/api", "tracekit"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["services/api/tests", "tracekit/tests"]
```

Treat both arrays as append-only examples: preserve any Agent member/test path already integrated from the parallel Packet 3 worktree.

```toml
# tracekit/pyproject.toml
[project]
name = "perfpilot-tracekit"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "openpyxl>=3.1,<4",
  "pydantic>=2.13.4,<2.14",
]

[project.scripts]
tracekit = "tracekit.cli:main"

[build-system]
requires = ["uv_build>=0.11.25,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-root = "src"
```

- [ ] **Step 2: Write the failing resource test**

```python
from importlib.resources import files


def test_required_resources_are_packaged() -> None:
    root = files("tracekit.resources")
    assert (root / "sql" / "01_cold_start.sql").is_file()
    assert (root / "capture" / "trace_app.sh").is_file()
    assert (root / "capture" / "scroll_test.sh").is_file()
    assert (root / "perfetto" / "trace_processor").is_file()
```

- [ ] **Step 3: Run RED**

```bash
uv lock
uv sync --locked --all-packages --dev
uv run --package perfpilot-tracekit pytest tracekit/tests/unit/test_package_resources.py -q
```

Expected: FAIL because the package resources do not exist.

- [ ] **Step 4: Perform the mechanical migration**

Copy the current source once, preserving content but excluding caches and generated output:

```bash
mkdir -p tracekit/src/tracekit/resources/sql
mkdir -p tracekit/src/tracekit/resources/capture
mkdir -p tracekit/src/tracekit/resources/perfetto
mkdir -p tracekit/src/tracekit/legacy
cp /Users/ray/Desktop/trace/tools/analysis/*.sql tracekit/src/tracekit/resources/sql/
cp /Users/ray/Desktop/trace/tools/analysis/trace_processor tracekit/src/tracekit/resources/perfetto/
cp /Users/ray/Desktop/trace/tools/capture/*.sh tracekit/src/tracekit/resources/capture/
cp /Users/ray/Desktop/trace/tools/capture/*.py tracekit/src/tracekit/resources/capture/
cp /Users/ray/Desktop/trace/tools/{run_all,report_model,report_catalog,excel_writer,rebuild_report,import_diagnostic}.py \
  tracekit/src/tracekit/legacy/
cp /Users/ray/Desktop/trace/tests/test_report_pipeline.py \
  tracekit/tests/legacy/test_report_pipeline.py
```

Add `__init__.py` files and replace `from tools...` imports with `from tracekit.legacy...`. Replace hard-coded project paths with `importlib.resources.files("tracekit.resources")`. The legacy test must import `tracekit.legacy`, not `/Users/ray/Desktop/trace/tools`.

Before calling these resources from an Agent, write `test_capture_serial.py`: each capture entry point exits with code `2` before invoking ADB when `--serial` is absent; with `--serial R3CN30TEST` and an injected fake ADB executable, every recorded device command begins `adb -s R3CN30TEST`. Refactor the migrated scripts around one quoted `adb_for_device` helper and argument arrays. Do not accept a shell fragment as serial, package, component, run ID, or output root.

- [ ] **Step 5: Define shared tracekit fixtures before adapter tests**

Create `tracekit/tests/factories.py` with repository/resource path constants and deterministic helpers `load_schema`, `load_example`, `sql_paths`, `binary_manifest`, `healthy_trace`, `startup_row`, `process_row`, `complete_window`, `crash_evidence`, `metric`, `finding_by_rule`, `write_memory_bundle`, `incomplete_evidence`, and `complete_evidence`. Every test module explicitly imports the helpers it calls.

Create `tracekit/tests/conftest.py` with:

- a deterministic `cli_runner` that invokes `tracekit.cli.main` without a shell;
- valid `startup_bundle` and `memory_bundle` fixtures with fixed IDs/timestamps;
- a `rule_engine` loaded from the checked-in v1 rules;
- decompression fixtures that verify `SHA256SUMS` before exposing the launch and scroll Trace paths.

Helpers whose production modules do not exist yet may return plain immutable test records until the corresponding task replaces them with real models. Tests must not read a developer output directory, depend on wall-clock time, or use an unverified Trace.

All production imports in `conftest.py` are lazy inside the relevant fixture body. Early package/resource tests must collect before adapters, rule modules, or Perfetto runners have been created.

- [ ] **Step 6: Run GREEN and commit**

```bash
uv lock
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_package_resources.py \
  tracekit/tests/integration/test_capture_serial.py \
  tracekit/tests/legacy/test_report_pipeline.py -q
git add pyproject.toml uv.lock tracekit
git commit -m "refactor: package Android performance tools"
```

## Task 2: Define `AnalysisBundle v1` and the machine CLI

**Files:**
- Create: `tracekit/src/tracekit/contracts.py`
- Create: `tracekit/src/tracekit/cli.py`
- Create: `tracekit/tests/contract/test_analysis_bundle.py`
- Create: `tracekit/tests/integration/test_cli.py`
- Verify unchanged: `contracts/v1/reports/{analysis-bundle,analysis-report}.schema.json`
- Create: `contracts/v1/examples/analysis-bundle.valid.json`

- [ ] **Step 1: Write failing contract tests**

```python
def test_bundle_uses_only_four_finding_states() -> None:
    schema = load_schema("reports/analysis-bundle.schema.json")
    states = schema["$defs"]["finding"]["properties"]["status"]["enum"]
    assert states == ["confirmed", "suspected", "insufficient_data", "invalid_capture"]


def test_partial_report_preserves_completed_and_failed_scenarios() -> None:
    schema = load_schema("reports/analysis-report.schema.json")
    payload = load_example("analysis-report.partial.valid.json")
    jsonschema.Draft202012Validator(schema).validate(payload)
    assert [item["result_state"] for item in payload["scenario_reports"]] == [
        "completed", "failed", "completed"
    ]


def test_cli_writes_json_not_terminal_text(tmp_path: Path) -> None:
    source = (
        Path(__file__).parents[3]
        / "contracts/v1/examples/analysis-bundle.valid.json"
    )
    output = tmp_path / "bundle.json"
    result = cli_runner(
        [
            "validate-bundle",
            "--input-json",
            str(source),
            "--contract-version",
            "1",
            "--output-json",
            str(output),
        ]
    )
    assert result.returncode == 0
    AnalysisBundle.model_validate_json(output.read_text())
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/contract/test_analysis_bundle.py \
  tracekit/tests/integration/test_cli.py -q
```

Expected: FAIL because `AnalysisBundle` and the CLI are absent.

- [ ] **Step 3: Implement immutable report models**

```python
class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    evidence_id: UUID
    source: str
    query_id: str | None
    interval_start_ns: int | None
    interval_end_ns: int | None
    fields: dict[str, JsonValue]


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    finding_id: UUID
    rule_id: str
    status: Literal[
        "confirmed", "suspected", "insufficient_data", "invalid_capture"
    ]
    severity: Literal["critical", "warning", "healthy", "informational"]
    confidence: Literal["high", "medium", "low", "none"]
    title: str
    evidence_ids: tuple[UUID, ...]
    recommendation: str | None
    retest: str | None


class AnalysisBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    scenario_type: Literal["cold_start", "scroll", "memory_cycle", "startup"]
    valid_measurement: bool
    metrics: tuple[Metric, ...]
    findings: tuple[Finding, ...]
    evidence: tuple[Evidence, ...]
    provenance: Provenance


class ReportFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    message: str
    retryable: bool


class ScenarioReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scenario_type: Literal["cold_start", "scroll", "memory_cycle", "startup"]
    result_state: Literal["completed", "failed", "canceled"]
    device_group_id: UUID | None
    device_group_reason: str | None
    bundle: AnalysisBundle | None
    failure: ReportFailure | None


class AnalysisReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    analysis_id: UUID
    analysis_mode: Literal["device", "trace_upload"]
    state: Literal["completed", "partially_completed", "failed", "canceled"]
    report_version: int
    generated_at: datetime
    scenario_reports: tuple[ScenarioReport, ...]
```

`Metric` must include stable name, unit, state (`measured`, `insufficient_data`, `unavailable`, `query_failed`), numeric value or null, aggregation, sample count, and source evidence IDs. `Provenance` includes all applicable artifact hashes, tool versions, SQL bundle hash, rule version, Worker image digest, and explicit `not_provided`/`not_applicable` reasons. Model validators enforce the completed/failure invariant and require exactly one of `device_group_id` or `device_group_reason`; the implementation must not alter the already-published schema files in this packet.

- [ ] **Step 4: Implement the first stable CLI command**

```python
def validate_bundle(args: argparse.Namespace) -> int:
    if args.contract_version != "1":
        raise UnsupportedContractVersion(args.contract_version)
    bundle = AnalysisBundle.model_validate_json(
        Path(args.input_json).read_text(encoding="utf-8")
    )
    write_json_atomically(Path(args.output_json), bundle.model_dump_json(indent=2))
    return 0
```

Write atomically through a sibling temporary file and `os.replace`. Return stable non-zero exit codes for invalid input and unsupported contract version. Task 5 registers `analyze --adapter startup`, Task 6 registers `scroll`, and Task 7 registers `memory-cycle`; each registration adds a CLI integration case before its GREEN commit. This ordering prevents the Task 2 CLI from importing adapters that do not exist yet.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/contract/test_analysis_bundle.py \
  tracekit/tests/integration/test_cli.py -q
git add tracekit/src/tracekit contracts/v1/examples/analysis-bundle.valid.json tracekit/tests
git commit -m "feat: add structured analysis bundle"
```

## Task 3: Pin Perfetto and validate SQL resources

**Files:**
- Create: `tracekit/src/tracekit/perfetto/{binary,query,resources}.py`
- Create: `tracekit/tests/unit/test_sql_resources.py`
- Create: `tracekit/tests/integration/test_perfetto_queries.py`
- Create: `tracekit/tests/testdata/{README.md,SHA256SUMS}`
- Modify: `.gitignore`

- [ ] **Step 1: Register protected Trace fixtures without copying them into Git**

The local Trace files contain application/device runtime metadata and have no checked-in redistribution grant. Do not copy, compress, stage, or upload them to GitHub. Add `*.pftrace`, `*.pftrace.gz`, `*.perfetto-trace`, and `tracekit/tests/testdata/private/` to `.gitignore`.

Write `tracekit/tests/testdata/SHA256SUMS` with the verified raw-file hashes:

```text
6c5479fd1b765ee4d29692c43a8204b972bc0f97eb373aca98ea7e11e99fd8b4  launch_light.pftrace
df7eebc5c6ff19b48331f8f1bca346612d86a5ae26eae202d46842a83f87a653  scroll_Standard-AOSP-App-Without-PreAnimation.pftrace
```

`README.md` documents that `PERFPILOT_TEST_TRACE_DIR` must point to an access-controlled directory containing those exact filenames. The fixture loader hashes both files before opening either one and fails the protected integration gate on a missing/mismatched file; it never turns the test into a skip. CI obtains the same encrypted private fixture artifact through a protected secret available only to the trusted main-branch job and verifies these hashes before testing.

- [ ] **Step 2: Write failing SQL guard tests**

```python
def test_sql_is_idempotent_and_avoids_like() -> None:
    for path in sql_paths():
        sql = path.read_text(encoding="utf-8")
        assert " LIKE " not in sql.upper(), path.name
        assert "ts + dur" not in sql or "dur = -1" in sql, path.name
        assert " pid =" not in sql.lower(), path.name
        assert " tid =" not in sql.lower(), path.name


def test_perfetto_wrapper_is_v56_1_and_checksum_pinned() -> None:
    manifest = binary_manifest()
    assert manifest.release == "v56.1"
    assert all(len(item.sha256) == 64 for item in manifest.platforms)
```

- [ ] **Step 3: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_sql_resources.py \
  tracekit/tests/integration/test_perfetto_queries.py -q
```

Expected: FAIL until resource loading, SQL guards, and query execution exist.

- [ ] **Step 4: Implement the runner**

`PerfettoRunner` resolves the packaged v56.1 wrapper, requires a pre-fetched checksum-verified binary in production, executes a standalone query with a timeout and byte cap, and parses CSV strictly. Production mode must never download. Image build mode may execute the pinned wrapper once to populate the image.

Queries must use the documented modules and tables:

```text
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE android.startup.time_to_display;
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
```

Use `android_startups` for package/startup type, `android_startup_processes` for UPID, `android_startup_time_to_display` for TTID/TTFD, `android_frames` for frame identity, and `android_frame_stats.overrun` for deadline misses. Use UPID/UTID joins, `GLOB`, qualified aliases, and safe `dur=-1` handling.

- [ ] **Step 5: Run GREEN and commit**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_sql_resources.py \
  tracekit/tests/integration/test_perfetto_queries.py -q
git add tracekit/src/tracekit/perfetto tracekit/tests/unit/test_sql_resources.py tracekit/tests/integration/test_perfetto_queries.py tracekit/tests/testdata/README.md tracekit/tests/testdata/SHA256SUMS .gitignore
git commit -m "test: pin Perfetto analysis fixtures"
```

## Task 4: Implement shared trace health and sample validation

**Files:**
- Create: `tracekit/src/tracekit/perfetto/health.py`
- Create: `tracekit/src/tracekit/validation.py`
- Create: `tracekit/tests/unit/test_validation.py`
- Create: `contracts/v1/reports/trace-health.schema.json`

- [ ] **Step 1: Write failing validity tests**

```python
def test_startup_requires_exact_package_type_and_observed_pid() -> None:
    verdict = validate_startup(
        health=healthy_trace(),
        startup_rows=[startup_row(package="com.demo", startup_type="cold", upid=7)],
        expected_package="com.demo",
        startup_mode="cold",
        observed_pid=123,
        process_rows=[process_row(upid=7, pid=124)],
    )
    assert verdict.code == "observed_pid_mismatch"
    assert not verdict.valid


def test_zero_frame_healthy_scroll_is_not_retried_away() -> None:
    verdict = validate_scroll(
        health=healthy_trace(),
        window=complete_window(seconds=30),
        frame_rows=[],
        automation=crash_evidence(),
    )
    assert verdict.valid
    assert verdict.outcome == "crash"
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-tracekit pytest tracekit/tests/unit/test_validation.py -q
```

Expected: FAIL because validators are absent.

- [ ] **Step 3: Implement `TraceHealth`**

Record parse state, trace bounds, package/process/UPID/PID/UTID resolution, required tables, data sources, scenario window coverage, ftrace/buffer loss, unfinished slices, FrameTimeline coverage, target display, and refresh mode. Missing required evidence returns `invalid_capture` or `insufficient_data`; missing optional sensors return `unavailable`, never zero.

- [ ] **Step 4: Implement shared verdicts**

Startup requires exact package, exact `startup_type == startup_mode`, one target startup, matching observed PID, valid TTID, and full window coverage. Scroll requires a healthy trace, complete 30-second window, process/page/gesture proof, and segmented motion or explicit crash/ANR evidence. It has no minimum-frame gate. Memory requires one baseline plus ten after-exit records, one PID, successful postconditions, and parseable required fields.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-tracekit pytest tracekit/tests/unit/test_validation.py -q
git add tracekit/src/tracekit contracts/v1/reports tracekit/tests/unit/test_validation.py
git commit -m "feat: validate performance evidence"
```

## Task 5: Implement the startup adapter

**Files:**
- Create: `tracekit/src/tracekit/adapters/startup.py`
- Create: `tracekit/src/tracekit/resources/sql/startup/`
- Create: `tracekit/tests/unit/test_startup_adapter.py`
- Create: `tracekit/tests/integration/test_startup_trace.py`
- Modify: `tracekit/tests/integration/test_cli.py`

- [ ] **Step 1: Write failing adapter tests**

```python
def test_first_capture_maps_to_cold_startup_type() -> None:
    request = StartupRequest(
        package="com.demo", startup_mode="cold", capture_variant="first"
    )
    assert request.expected_startup_type == "cold"


def test_missing_ttfd_is_unavailable_not_zero(startup_bundle) -> None:
    ttfd = metric(startup_bundle, "time_to_full_display_ms")
    assert ttfd.value is None
    assert ttfd.state == "unavailable"
```

- [ ] **Step 2: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_startup_adapter.py \
  tracekit/tests/integration/test_startup_trace.py -q
```

Expected: startup adapter import fails.

- [ ] **Step 3: Implement one-attempt analysis**

Return TTID, optional TTFD, main/UI and RenderThread CPU, scheduling delay, longest main-thread slices, Binder waits, GC/lock evidence, CPU frequency coverage, and provenance. Select the unique `android_startups` row by package and `startup_mode`; never infer a type from `capture_variant=manual`.

Register `analyze --adapter startup --input INPUT --contract-version 1 --output-json OUTPUT` in the CLI and add an integration case using the verified launch fixture. The command calls `analyze_startup_attempt` and atomically writes only `AnalysisBundle v1` JSON.

- [ ] **Step 4: Implement five-sample aggregation**

Aggregate only server-valid attempts. Preserve every value, median, bad-direction P90, min, max, coefficient of variation, valid count, and expected count. Fewer than five valid attempts yields a partial bundle and a failed scenario result, not fabricated values.

- [ ] **Step 5: Run GREEN and commit**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_startup_adapter.py \
  tracekit/tests/integration/test_startup_trace.py -q
git add tracekit/src/tracekit/adapters/startup.py tracekit/src/tracekit/resources/sql/startup tracekit/tests
git commit -m "feat: analyze Android startup traces"
```

## Task 6: Implement the scroll adapter with unambiguous metrics

**Files:**
- Create: `tracekit/src/tracekit/adapters/scroll.py`
- Create: `tracekit/src/tracekit/resources/sql/scroll/`
- Create: `tracekit/tests/unit/test_scroll_metrics.py`
- Create: `tracekit/tests/integration/test_scroll_trace.py`
- Modify: `tracekit/tests/integration/test_cli.py`

- [ ] **Step 1: Write failing metric tests**

```python
def test_overrun_metrics_have_distinct_denominators() -> None:
    metrics = compute_scroll_metrics(
        overruns_ns=[-4_000_000, 1_000_000, 9_000_000, 40_000_000],
        motion_frame_count=4,
        motion_duration_ns=2_000_000_000,
    )
    assert metrics.deadline_miss_rate == 0.75
    assert metrics.p95_positive_overrun_ms > 0
    assert metrics.p95_late_frame_overrun_ms >= metrics.p95_positive_overrun_ms
    assert metrics.motion_fps == 2.0


def test_static_window_has_no_motion_fps() -> None:
    assert compute_scroll_metrics([], 0, 0).motion_fps is None
```

- [ ] **Step 2: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_scroll_metrics.py \
  tracekit/tests/integration/test_scroll_trace.py -q
```

Expected: metric function and adapter are absent.

- [ ] **Step 3: Implement the formulas**

```python
deadline_miss_rate = count(overrun_ns > 0) / count(all_frames)
p95_positive_overrun = p95(max(overrun_ns, 0) for every frame)
p95_late_frame_overrun = p95(overrun_ns for late frames only)
motion_fps = target_app_frames_in_confirmed_motion / confirmed_motion_seconds
```

Filter by scenario window, target UPID, target display, and matching FrameTimeline rows. A healthy zero-frame trace with crash/ANR evidence remains a valid stability result. Never reuse the legacy `P95O` label.

- [ ] **Step 4: Add root-cause evidence**

For late frames, persist normalized frame IDs, intervals, UI/RenderThread UTIDs, CPU/wait state, overlapping slices, Binder or lock dependencies, SurfaceFlinger evidence, and data-loss exclusions. A single longest slice can produce only `suspected` root cause.

Register `analyze --adapter scroll` through the same CLI argument and atomic-output path, and add a case using the verified scroll fixture.

- [ ] **Step 5: Run GREEN and commit**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_scroll_metrics.py \
  tracekit/tests/integration/test_scroll_trace.py -q
git add tracekit/src/tracekit/adapters/scroll.py tracekit/src/tracekit/resources/sql/scroll tracekit/tests
git commit -m "feat: analyze Android scroll traces"
```

## Task 7: Implement the memory-cycle adapter

**Files:**
- Create: `tracekit/src/tracekit/adapters/memory.py`
- Create: `tracekit/tests/unit/test_memory_adapter.py`
- Modify: `tracekit/tests/integration/test_cli.py`
- Copy: `/Users/ray/Desktop/trace/tests/test_memory_cycle_test.py` to `tracekit/tests/legacy/test_memory_cycle.py`

- [ ] **Step 1: Write failing memory semantics tests**

```python
def test_growth_without_heap_evidence_is_suspected(memory_bundle) -> None:
    finding = finding_by_rule(memory_bundle, "memory.not_recovered")
    assert finding.status == "suspected"
    assert "泄漏" not in finding.title


def test_missing_round_invalidates_bundle(tmp_path: Path) -> None:
    write_memory_bundle(tmp_path, rounds=9)
    result = analyze_memory_cycle(tmp_path)
    assert not result.valid_measurement
    assert result.findings[0].status == "invalid_capture"
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_memory_adapter.py \
  tracekit/tests/legacy/test_memory_cycle.py -q
```

Expected: memory adapter import fails.

- [ ] **Step 3: Implement bundle parsing**

Validate `metadata.json`, `summary.json`, `memory_cycles.csv`, baseline and ten raw meminfo files, and `agent.log`. Preserve null fields. Emit Java Heap, Native Heap, Graphics, TOTAL PSS, Views, ViewRootImpl, Activities, baseline deltas, final deltas, peaks, and all ten round values.

- [ ] **Step 4: Implement evidence limits**

Confirm only observable symptoms such as growth or failure to recover. Without a heap graph, LeakCanary result, or retained-object path, memory leak root cause cannot exceed `suspected`. PID change, postcondition failure, missing round, or unparsable required data returns `invalid_capture`.

Register `analyze --adapter memory-cycle`, add a CLI case built from `write_memory_bundle(..., rounds=10)`, and assert that the output validates as `AnalysisBundle v1`.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_memory_adapter.py \
  tracekit/tests/legacy/test_memory_cycle.py -q
git add tracekit/src/tracekit/adapters/memory.py tracekit/tests
git commit -m "feat: analyze memory-cycle bundles"
```

## Task 8: Add the versioned evidence rule engine

**Files:**
- Create: `tracekit/src/tracekit/rules/{models,loader,evaluator}.py`
- Create: `tracekit/src/tracekit/resources/rules/v1/*.json`
- Create: `tracekit/tests/unit/test_rules.py`

- [ ] **Step 1: Write failing confidence tests**

```python
def test_confirmed_root_cause_requires_all_evidence_and_exclusions(rule_engine) -> None:
    result = rule_engine.evaluate("startup.main_thread_binder", incomplete_evidence())
    assert result.status == "suspected"


def test_data_loss_blocks_confirmed_result(rule_engine) -> None:
    result = rule_engine.evaluate(
        "scroll.main_thread_cpu", complete_evidence(trace_data_loss=True)
    )
    assert result.status == "insufficient_data"
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-tracekit pytest tracekit/tests/unit/test_rules.py -q
```

Expected: rule loader and evaluator do not exist.

- [ ] **Step 3: Implement immutable rules**

Each rule JSON has immutable `rule_id`, version, scenario types, required metric/evidence IDs, exclusions, symptom or root-cause kind, severity, confidence ceiling, recommendation, and retest method. Rules may confirm symptoms from thresholds. Root causes require a concrete window, CPU-versus-wall-clock classification, thread state, dependency chain, environment/data-loss exclusions, and a repeated signature across valid samples.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run --package perfpilot-tracekit pytest tracekit/tests/unit/test_rules.py -q
git add tracekit/src/tracekit/rules tracekit/src/tracekit/resources/rules tracekit/tests/unit/test_rules.py
git commit -m "feat: add evidence-backed performance rules"
```

## Task 9: Build validator and full-analysis Worker consumers

**Files:**
- Modify: `pyproject.toml`
- Create: `services/trace-worker/pyproject.toml`
- Create: `services/trace-worker/src/perfpilot_worker/{config,control_client,workspace,persistence}.py`
- Create: `services/trace-worker/src/perfpilot_worker/consumers/{base,sample_validator,analysis}.py`
- Create: `services/trace-worker/src/perfpilot_worker/main.py`
- Create: `services/trace-worker/tests/conftest.py`
- Create: `services/trace-worker/tests/factories.py`
- Create: `services/trace-worker/tests/{test_validator_consumer,test_analysis_consumer,test_crash_recovery}.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Create: `services/api/tests/integration/test_report_api.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add the Worker workspace package**

The Worker depends on `perfpilot-tracekit` through:

```toml
[project]
name = "perfpilot-trace-worker"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "httpx>=0.28,<0.29",
  "perfpilot-tracekit",
  "pydantic>=2.13.4,<2.14",
  "pydantic-settings>=2.12,<3",
  "redis[hiredis]>=8.0.1,<8.1",
]

[tool.uv.sources]
perfpilot-tracekit = { workspace = true }
```

Append `services/trace-worker` to root workspace members, append `services/trace-worker/tests` to the root pytest `testpaths` without removing Agent entries, and expose `perfpilot-trace-worker`.

- [ ] **Step 2: Define Worker fixtures before consumer tests**

Create `services/trace-worker/tests/factories.py` with fixed `EVENT_ID`, `REPORT_ID`, analysis/scenario/sample IDs, `sample_validation_event()`, `analysis_event()`, and `SimulatedCrash`; every test module imports the names it uses.

Create `services/trace-worker/tests/conftest.py` and provide:

- `fake_control`: an ordered call recorder implementing claim, route, artifact, verdict, report, completion, and ACK boundaries;
- `worker` and `worker_factory`: consumers with an in-memory object source, deterministic clock, verified tracekit fixtures, and injectable crash checkpoints;
- deterministic valid and malicious artifact archives for Task 10, including absolute path, parent traversal, symlink, file-count, and expanded-byte cases.

Every fake rejects a caller-supplied database URL, bucket name, object key, or team override so the tests preserve the production routing boundary.

- [ ] **Step 3: Write failing claim tests**

```python
async def test_validator_persists_verdict_before_ack(worker, fake_control) -> None:
    await worker.handle(sample_validation_event())
    assert fake_control.transitions == ["claim", "save_verdict", "complete_claim"]
    assert worker.acked_event_ids == [EVENT_ID]


async def test_worker_restart_reuses_report_id(worker_factory, fake_control) -> None:
    first = worker_factory(crash_after_report=True)
    with pytest.raises(SimulatedCrash):
        await first.handle(analysis_event())
    second = worker_factory()
    await second.handle(analysis_event())
    assert fake_control.report_write_count(REPORT_ID) == 1
```

Add an API integration case that seeds two completed scenario bundles and one failed scenario summary in a team database, calls `GET .../report`, validates `AnalysisReport v1`, and proves the completed siblings remain present. Repeat with another team session and require `404 resource_not_found`.

- [ ] **Step 4: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv lock
uv run --package perfpilot-trace-worker pytest services/trace-worker/tests -q
uv run --package perfpilot-api pytest services/api/tests/integration/test_report_api.py -q
```

Expected: Worker package is missing and the API cannot yet assemble the report envelope.

- [ ] **Step 5: Implement the consumers**

The validator claims through `/internal/v1/worker`, downloads only finalized sample artifacts, calls shared health/validity functions, and posts one authoritative verdict with a stable `verdict_id`. The API writes that verdict through the derived tenant route, completes `sample_validation_claim`, and updates opaque control counters before the Worker ACKs. A crash between tenant verdict and control completion reuses the same verdict ID and cannot count the sample twice.

The full Worker obtains inputs through the same private API, downloads exact versions into a per-claim workspace, validates hashes/manifests, chooses one adapter, posts the scenario bundle with a stable `report_id`, completes the claim, and then ACKs. The API assembles the latest immutable scenario versions into `AnalysisReport v1` without rewriting a bundle. Neither consumer accepts or receives a caller-supplied DSN, credential, bucket, or object key.

For startup and scroll, the full Worker recomputes sample validity from the same immutable inputs and compares it with every saved validator verdict. Any disagreement completes neither report nor success transition; it records `validator_disagreement`, fails the scenario as a system error, and emits an operational alert for the sample IDs.

- [ ] **Step 6: Run GREEN and commit**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv lock
uv run --package perfpilot-trace-worker pytest services/trace-worker/tests -q
uv run --package perfpilot-api pytest services/api/tests/integration/test_report_api.py -q
git add pyproject.toml uv.lock services/trace-worker services/api/src/perfpilot_api/api/analyses.py services/api/tests/integration/test_report_api.py
git commit -m "feat: add claim-based analysis workers"
```

## Task 10: Lock reproducibility and sandbox the Worker

**Files:**
- Create: `services/trace-worker/Dockerfile`
- Create: `services/trace-worker/tests/integration/test_reproducibility.py`
- Create: `services/trace-worker/tests/integration/test_archive_safety.py`
- Modify: `.github/workflows/platform-ci.yml`
- Modify: `infra/compose.yaml`

- [ ] **Step 1: Write reproducibility and archive tests**

Analyze the same immutable fixture twice and compare metrics, findings, evidence, and recommendations after removing report ID, generation time, and temporary paths. Add archives containing absolute paths, `..`, symlinks, excessive file counts, and excessive expanded bytes; every malicious archive must be rejected before extraction.

- [ ] **Step 2: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-trace-worker pytest \
  services/trace-worker/tests/integration/test_reproducibility.py \
  services/trace-worker/tests/integration/test_archive_safety.py -q
```

Expected: sandbox and deterministic provenance assertions fail.

- [ ] **Step 3: Build the restricted image**

Use a non-root UID, read-only root filesystem, per-job writable mount, no runtime package download, CPU/memory/file/process/time limits, and no default internet route. During image build, execute the pinned v56.1 wrapper once and verify the platform binary checksum. Record the final image digest in report provenance.

- [ ] **Step 4: Run packet verification**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv sync --locked --all-packages --dev
uv run ruff check tracekit services/trace-worker
uv run --package perfpilot-tracekit pytest tracekit/tests -q
uv run --package perfpilot-trace-worker pytest services/trace-worker/tests -q
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit and push**

```bash
git add services/trace-worker infra/compose.yaml .github/workflows/platform-ci.yml
git commit -m "build: sandbox PerfPilot analysis worker"
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
git status --short
```

Expected: local and remote SHAs match and status is empty.
