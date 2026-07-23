# PerfPilot Phase 3 Direct Trace Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authorized developer upload an existing startup or scroll Perfetto Trace, analyze it without a device or Agent, and receive the same evidence-backed report contract with honest capability and provenance limits.

**Architecture:** `analysis_mode=trace_upload` reuses the existing analysis parent, immutable upload slots, outbox, Worker claim, tracekit adapters, report persistence, and Web report route. The API validates scenario-specific metadata and advances the parent directly from `uploading` to `analyzing`; it creates no scenario child, lease, schedule event, or Agent request. The Worker derives a direct parent claim, validates package and measurement semantics against the Trace, records provided/missing/not-applicable provenance, and applies confidence ceilings when capture context is absent.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, PostgreSQL/SQLite adapters, Redis Streams/in-process queue, S3/local immutable storage, tracekit, Perfetto v56.1, React/Vinext, Web Crypto, pytest, Vitest, Playwright, Sites private hosting.

---

## File map

- Create `contracts/v1/analyses/trace-upload-request.schema.json`: scenario-discriminated create payload.
- Create `contracts/v1/analyses/trace-upload-manifest.schema.json`: finalized inputs and provenance.
- Modify API analysis models, state transitions, upload service, outbox routing, and report query.
- Create `tracekit/src/tracekit/adapters/trace_upload.py`: package/window/capability validation and adapter dispatch.
- Modify Worker claim resolution to support direct parent analysis.
- Modify `app/components/new-analysis-dialog.tsx` and add a Trace upload form/workflow.
- Add contract, integration, browser, fault, cloud, and local-mode tests.
- Preserve `AnalysisReport v1`, its single-scenario `AnalysisBundle v1`, and the existing Sites project configuration.

## Task 1: Publish the discriminated Trace-upload contract

**Files:**
- Create: `contracts/v1/analyses/trace-upload-request.schema.json`
- Create: `contracts/v1/analyses/trace-upload-manifest.schema.json`
- Create: `contracts/v1/examples/trace-upload-startup.valid.json`
- Create: `contracts/v1/examples/trace-upload-scroll.valid.json`
- Create: `contracts/v1/examples/trace-upload-memory.invalid.json`
- Create: `services/api/tests/contract/test_trace_upload_contract.py`
- Create: `tracekit/tests/contract/test_trace_upload_contract.py`
- Create: `app/lib/api/trace-upload-types.ts`
- Create: `tests/trace-upload-contract.test.ts`

- [ ] **Step 1: Define the exact valid examples**

Startup:

```json
{
  "schema_version": "1.0",
  "analysis_mode": "trace_upload",
  "scenario_type": "startup",
  "package": "com.example.gallery",
  "startup_mode": "cold",
  "capture_variant": "standard",
  "inputs": [
    {
      "kind": "trace",
      "filename": "launch.perfetto-trace",
      "mime": "application/octet-stream",
      "size": 1048576,
      "sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    }
  ]
}
```

Scroll:

```json
{
  "schema_version": "1.0",
  "analysis_mode": "trace_upload",
  "scenario_type": "scroll",
  "package": "com.example.gallery",
  "window_start_ns": 1000000000,
  "window_end_ns": 31000000000,
  "target_display_id": 0,
  "refresh_rate_hz": 120.0,
  "refresh_rate_mode": "fixed",
  "inputs": [
    {
      "kind": "trace",
      "filename": "scroll.perfetto-trace",
      "mime": "application/octet-stream",
      "size": 2097152,
      "sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    }
  ]
}
```

Invalid memory request:

```json
{
  "schema_version": "1.0",
  "analysis_mode": "trace_upload",
  "scenario_type": "memory_cycle",
  "package": "com.example.gallery",
  "inputs": []
}
```

- [ ] **Step 2: Write failing producer and consumer tests**

All three runtimes load the same schemas. Assert:

- startup requires package, Trace, and `startup_mode=cold|warm|hot`;
- `capture_variant=first` requires `startup_mode=cold`;
- `capture_variant=manual` never supplies an inferred startup mode;
- scroll requires package, an ordered positive window, target display, positive refresh rate, and `refresh_rate_mode=fixed|variable`;
- exactly one primary Trace exists;
- optional kinds are only `capture_manifest`, `apk`, `mapping`, `source_archive`, `native_symbols`, and `log`;
- `memory_cycle` is schema-invalid and maps to `unsupported_trace_scenario`;
- unknown fields and ambiguous top-level `mode` are rejected.

- [ ] **Step 3: Run RED**

```bash
uv run --package perfpilot-api pytest services/api/tests/contract/test_trace_upload_contract.py -q
uv run --package perfpilot-tracekit pytest tracekit/tests/contract/test_trace_upload_contract.py -q
npm run test:unit -- tests/trace-upload-contract.test.ts
```

Expected: FAIL because the Trace-upload schemas and TypeScript types do not exist.

- [ ] **Step 4: Implement and freeze the schemas**

Use a `oneOf` discriminator on `scenario_type`; set `additionalProperties: false` at every object boundary. Keep byte counts as non-negative integers, nanoseconds as integers, refresh rate as a finite positive number, and checksum as exactly 32 bytes encoded with standard Base64. Every artifact includes client filename for display only; object keys remain server-generated.

`trace-upload-manifest` records each finalized artifact ID/version/hash, the explicit scenario semantics, API validation result, and provenance presence:

```text
provided | derived_from_trace | not_provided | not_applicable
```

Do not add another report schema or a new finding-status enum.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest services/api/tests/contract/test_trace_upload_contract.py -q
uv run --package perfpilot-tracekit pytest tracekit/tests/contract/test_trace_upload_contract.py -q
npm run test:unit -- tests/trace-upload-contract.test.ts
git add contracts/v1/analyses/trace-upload-request.schema.json contracts/v1/analyses/trace-upload-manifest.schema.json contracts/v1/examples/trace-upload-startup.valid.json contracts/v1/examples/trace-upload-scroll.valid.json contracts/v1/examples/trace-upload-memory.invalid.json services/api/tests/contract/test_trace_upload_contract.py tracekit/tests/contract/test_trace_upload_contract.py app/lib/api/trace-upload-types.ts tests/trace-upload-contract.test.ts
git commit -m "feat: define direct Trace upload contracts"
```

## Task 2: Add direct-parent API orchestration and immutable uploads

**Files:**
- Modify: `services/api/src/perfpilot_api/domain/states.py`
- Modify: `services/api/src/perfpilot_api/domain/transitions.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/services/uploads.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/db/control/models.py`
- Modify: `services/api/src/perfpilot_api/db/tenant/models.py`
- Create: `services/api/tests/integration/test_trace_upload_api.py`
- Create: `services/api/tests/integration/test_trace_upload_routing.py`
- Create: `services/api/tests/unit/test_trace_upload_states.py`
- Modify: `services/api/tests/conftest.py`
- Create: `services/api/tests/trace_upload_factories.py`

- [ ] **Step 1: Define Trace-upload fixtures before the tests consume them**

`trace_upload_factories.py` exports fixed IDs plus `finalize_body` and deterministic request/manifest value builders. Extend the already discovered `services/api/tests/conftest.py` with `startup_request`, `scroll_request`, `memory_request`, `trace_slot_put`, `capture_manifest_put`, `team_session`, `other_team_session`, `control_inspector`, `tenant_inspector`, `outbox_inspector`, and `device_queue_spy`. Tests explicitly import helpers and receive fixtures as parameters. Every request uses fixed UUIDs and deterministic bytes; no test calls a real Agent or object store.

- [ ] **Step 2: Write failing API behavior tests**

```python
def test_finalize_trace_upload_creates_only_analysis_work(
    api_client,
    team_session,
    startup_request,
    trace_slot_put,
    control_inspector,
    outbox_inspector,
    device_queue_spy,
) -> None:
    created = api_client.post(
        f"/v1/teams/{team_session.team_id}/analyses",
        json=startup_request,
        headers={**team_session.state_headers, "Idempotency-Key": "trace-idem-1"},
    ).json()
    trace_slot_put(created["uploads"][0])
    finalized = api_client.post(
        f"/v1/teams/{team_session.team_id}/analyses/{created['analysis_id']}/finalize-upload",
        json=finalize_body(created),
        headers=team_session.state_headers,
    )
    assert finalized.json()["state"] == "analyzing"
    assert control_inspector.scenario_jobs(created["analysis_id"]) == []
    assert control_inspector.agent_leases(created["analysis_id"]) == []
    assert outbox_inspector.types(created["analysis_id"]) == ["analysis_requested"]
    assert device_queue_spy.events == []


def test_trace_memory_is_stably_rejected(api_client, team_session, memory_request) -> None:
    response = api_client.post(
        f"/v1/teams/{team_session.team_id}/analyses",
        json=memory_request,
        headers={**team_session.state_headers, "Idempotency-Key": "trace-memory-1"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_trace_scenario"
```

Also test duplicate idempotency, changed-body conflict, cross-team finalize, missing slot, unfinalized optional input, checksum mismatch, expired slot, cancel during upload, cancel during analysis, delete, quota, and concurrent finalize.

- [ ] **Step 3: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_trace_upload_states.py \
  services/api/tests/integration/test_trace_upload_api.py \
  services/api/tests/integration/test_trace_upload_routing.py -q
```

Expected: FAIL because the API accepts only device analysis creation.

- [ ] **Step 4: Implement direct-parent states**

Trace-upload uses only:

```text
creating → created → uploading → analyzing → completed
                                      ↘ failed
                         ↘ cancel_requested → canceled
```

Keep the existing delete/tombstone lifecycle. `GET .../analyses/{analysis_id}` returns an empty `scenarios` array, parent-level progress, upload summaries, claim summary, and `report_available`. It must not fabricate a scenario child to satisfy an existing UI assumption.

The create transaction writes the parent/control mapping, tenant analysis row, and required upload slots. Finalize validates every declared required and optional slot, stores one immutable Trace-upload manifest, performs `uploading → analyzing`, and writes one `analysis_requested` outbox event in the same transaction. No schedule outbox event is legal for this mode.

- [ ] **Step 5: Enforce routing and upload limits**

Derive tenant storage only from the authenticated team and saved analysis mapping. Accept primary Trace types `.perfetto-trace`, `.trace`, `.pb`, or `application/octet-stream` within the configured limit. Validate optional archive MIME/size before issuing slots; archive content safety remains the Worker’s responsibility. A filename, package, object key, bucket, database URL, or team ID in client metadata never controls routing.

- [ ] **Step 6: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_trace_upload_states.py \
  services/api/tests/integration/test_trace_upload_api.py \
  services/api/tests/integration/test_trace_upload_routing.py -q
git add services/api/src/perfpilot_api services/api/tests/conftest.py services/api/tests/trace_upload_factories.py services/api/tests/unit/test_trace_upload_states.py services/api/tests/integration/test_trace_upload_api.py services/api/tests/integration/test_trace_upload_routing.py
git commit -m "feat: orchestrate direct Trace analyses"
```

## Task 3: Validate uploaded Trace semantics in tracekit

**Files:**
- Create: `tracekit/src/tracekit/adapters/trace_upload.py`
- Modify: `tracekit/src/tracekit/adapters/startup.py`
- Modify: `tracekit/src/tracekit/adapters/scroll.py`
- Modify: `tracekit/src/tracekit/perfetto/health.py`
- Modify: `tracekit/src/tracekit/contracts.py`
- Create: `tracekit/src/tracekit/symbols.py`
- Modify: `tracekit/tests/conftest.py`
- Create: `tracekit/tests/unit/test_trace_upload_adapter.py`
- Create: `tracekit/tests/unit/test_symbols.py`
- Create: `tracekit/tests/integration/test_trace_upload_startup.py`
- Create: `tracekit/tests/integration/test_trace_upload_scroll.py`
- Create: `tracekit/tests/testdata/trace-upload/README.md`

- [ ] **Step 1: Write failing semantic-validation tests**

First extend `tracekit/tests/conftest.py` with `trace_upload_adapter`, `launch_trace`, and `scroll_trace`. The two Trace fixtures come from the Phase 1 checksum-verified protected raw fixtures and expose true bounds/package/display metadata; they never change the primary files.

```python
def test_startup_package_and_mode_must_exist_in_trace(
    trace_upload_adapter, launch_trace
) -> None:
    with pytest.raises(InvalidCapture, match="startup_not_found"):
        trace_upload_adapter.analyze_startup(
            launch_trace,
            package="com.other.package",
            startup_mode="cold",
            capture_variant="standard",
        )


def test_scroll_window_is_clipped_neither_silently_nor_approximately(
    trace_upload_adapter, scroll_trace
) -> None:
    with pytest.raises(InvalidCapture, match="measurement_window_outside_trace"):
        trace_upload_adapter.analyze_scroll(
            scroll_trace,
            package="com.example.gallery",
            window_start_ns=scroll_trace.end_ns - 1,
            window_end_ns=scroll_trace.end_ns + 30_000_000_000,
            target_display_id=0,
            refresh_rate_hz=120.0,
            refresh_rate_mode="fixed",
        )
```

Add tests for exact `android_startups.startup_type`, package/UPID resolution, ambiguous multiple processes, missing frame timeline capability, absent target display, invalid Trace/data loss, variable refresh metadata, and window duration.

In `test_symbols.py`, prove that absent mapping/symbol files preserve numeric results, a hash-verified mapping can add a method-level location, malformed or mismatched symbols become explicit unavailable provenance, and no source archive file is executed or treated as a trusted path.

- [ ] **Step 2: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_trace_upload_adapter.py \
  tracekit/tests/unit/test_symbols.py \
  tracekit/tests/integration/test_trace_upload_startup.py \
  tracekit/tests/integration/test_trace_upload_scroll.py -q
```

Expected: FAIL because a direct-upload adapter has not been implemented.

- [ ] **Step 3: Implement startup validation with Perfetto stdlib**

Use exact modules and tables:

```sql
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE android.startup.time_to_display;
```

Resolve the target process through package-associated process metadata and UPID, not an unqualified process-name substring. Query `android_startups` and require `startup_type` to match the explicit `startup_mode`. Preserve `capture_variant` only as provenance; it never overrides startup semantics. Qualify every selected column, handle `dur = -1`, and use `GLOB` only where a pattern is truly required.

- [ ] **Step 4: Implement scroll validation with Perfetto stdlib**

Use:

```sql
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
```

Resolve package to UPID/UTID and target display explicitly. Require the requested interval to be wholly inside Trace bounds and to contain applicable frames. Keep these metrics separate:

- frame overrun duration/distribution from `android_frames_overrun`;
- slow-frame ratio against the declared threshold;
- refresh-normalized missed-vsync severity using the declared display mode/rate.

Do not label one metric as another. Record whether refresh metadata came from the Trace, request, or capture manifest.

- [ ] **Step 5: Apply honest provenance ceilings**

For every contextual input, record `provided`, `derived_from_trace`, `not_provided`, or `not_applicable`. When the capture manifest is absent:

- findings proven entirely by Trace evidence may remain `confirmed`;
- findings requiring device or scenario context are capped at `suspected`;
- findings requiring an unverifiable measurement window or environment gate become `insufficient_data`;
- malformed, truncated, package-mismatched, or capability-incompatible input becomes `invalid_capture`.

The adapter must never infer thermal state, process-cold preparation, user journey, or cleanup from their absence.

`symbols.py` treats mapping and Native symbols as untrusted, immutable inputs to pinned offline symbolization. It accepts only files finalized in the analysis manifest, applies size/count/time limits, and links a method-level location only when a symbol result and Trace address or Slice agree. A source archive is used only to resolve a normalized relative display path after archive-safety validation; it is never imported, compiled, or executed. The report does not promise a source line when the evidence proves only a method, Slice, thread, frame, or interval.

- [ ] **Step 6: Run GREEN and commit**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-tracekit pytest \
  tracekit/tests/unit/test_trace_upload_adapter.py \
  tracekit/tests/unit/test_symbols.py \
  tracekit/tests/integration/test_trace_upload_startup.py \
  tracekit/tests/integration/test_trace_upload_scroll.py -q
git add tracekit/src/tracekit/adapters tracekit/src/tracekit/perfetto/health.py tracekit/src/tracekit/contracts.py tracekit/src/tracekit/symbols.py tracekit/tests/conftest.py tracekit/tests/unit/test_trace_upload_adapter.py tracekit/tests/unit/test_symbols.py tracekit/tests/integration/test_trace_upload_startup.py tracekit/tests/integration/test_trace_upload_scroll.py tracekit/tests/testdata/trace-upload/README.md
git commit -m "feat: analyze uploaded startup and scroll traces"
```

## Task 4: Add direct-parent Worker claims and recovery

**Files:**
- Modify: `services/trace-worker/src/perfpilot_worker/consumers/analysis.py`
- Modify: `services/trace-worker/src/perfpilot_worker/control_client.py`
- Modify: `services/trace-worker/src/perfpilot_worker/workspace.py`
- Modify: `services/trace-worker/src/perfpilot_worker/persistence.py`
- Modify: `services/trace-worker/tests/conftest.py`
- Modify: `services/trace-worker/tests/factories.py`
- Create: `services/trace-worker/tests/test_trace_upload_consumer.py`
- Create: `services/trace-worker/tests/test_trace_upload_recovery.py`
- Create: `services/trace-worker/tests/test_trace_upload_archive_safety.py`

- [ ] **Step 1: Write failing claim and no-Agent tests**

Extend the Phase 1 Worker factories with `trace_upload_event()` and the existing `fake_control`, `worker`, and `worker_factory` fixtures with direct-parent calls. The event contains only schema/event/subject identifiers. Tests import the event factory explicitly and receive Worker/control objects as pytest fixtures.

```python
async def test_trace_event_claims_parent_and_never_reads_scenario(
    worker, fake_control, trace_upload_event
) -> None:
    await worker.handle(trace_upload_event)
    assert fake_control.calls == [
        "claim_analysis",
        "get_analysis_inputs",
        "save_report",
        "complete_analysis_claim",
    ]
    assert "get_scenario" not in fake_control.calls
    assert "agent" not in " ".join(fake_control.calls)


async def test_restart_after_report_write_is_idempotent(
    worker_factory, fake_control, trace_upload_event
) -> None:
    with pytest.raises(SimulatedCrash):
        await worker_factory(crash_after_report=True).handle(trace_upload_event)
    await worker_factory().handle(trace_upload_event)
    assert fake_control.report_write_count(trace_upload_event.subject_id) == 1
```

- [ ] **Step 2: Run RED**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-trace-worker pytest \
  services/trace-worker/tests/test_trace_upload_consumer.py \
  services/trace-worker/tests/test_trace_upload_recovery.py \
  services/trace-worker/tests/test_trace_upload_archive_safety.py -q
```

Expected: FAIL because the Worker assumes every analysis event belongs to a scenario job.

- [ ] **Step 3: Implement claim dispatch by authoritative subject type**

Accept `subject_type=analysis` only when the control API returns `analysis_mode=trace_upload`; accept the existing scenario subject only for device work. Never choose mode from untrusted event fields. A direct claim returns opaque artifact IDs/versions and scenario semantics, not a tenant DSN or bucket.

Download each exact object version into a claim-specific workspace, verify size/checksum/manifest, inspect optional archives with the existing count/expanded-size/path/symlink limits, call the direct-upload adapter, save one stable report ID, complete the direct parent claim, commit inbox state, then ACK. A cancellation or deletion CAS that wins before completion prevents report publication and triggers workspace cleanup.

- [ ] **Step 4: Add cloud and local delivery coverage**

Run the same direct-parent consumer tests with Redis/PostgreSQL/S3 adapters and with SQLite/local files/in-process queue. Inject crashes before report write, after report write, after claim completion, and before ACK. Every case creates at most one report version for the claim and reaches an authoritative terminal state after recovery.

- [ ] **Step 5: Run GREEN and commit**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv run --package perfpilot-trace-worker pytest \
  services/trace-worker/tests/test_trace_upload_consumer.py \
  services/trace-worker/tests/test_trace_upload_recovery.py \
  services/trace-worker/tests/test_trace_upload_archive_safety.py -q
git add services/trace-worker/src/perfpilot_worker services/trace-worker/tests/conftest.py services/trace-worker/tests/factories.py services/trace-worker/tests/test_trace_upload_consumer.py services/trace-worker/tests/test_trace_upload_recovery.py services/trace-worker/tests/test_trace_upload_archive_safety.py
git commit -m "feat: process direct Trace analysis claims"
```

## Task 5: Enable the Trace upload Web workflow

**Files:**
- Modify: `app/components/new-analysis-dialog.tsx`
- Create: `app/components/trace-upload-form.tsx`
- Create: `app/components/trace-upload-fields.tsx`
- Create: `app/lib/analysis/create-trace-analysis.ts`
- Create: `tests/trace-upload-form.test.tsx`
- Create: `tests/create-trace-analysis.test.ts`
- Create: `tests/trace-upload-factories.ts`
- Modify: `tests/new-analysis-dialog.test.tsx`
- Modify: `tests/e2e/web-analysis.spec.ts`

- [ ] **Step 1: Write failing form semantics tests**

Create `tests/trace-upload-factories.ts` first. It exports contract-valid startup/scroll input values and strict `fakeApi`/upload doubles; every unexpected device, Agent, lease, schedule, or scenario call throws. Both test modules import these helpers explicitly.

```tsx
it("requires explicit startup mode and never uses capture variant as mode", async () => {
  render(<TraceUploadForm api={fakeApi()} activeTeamId="team-1" />);
  await userEvent.selectOptions(screen.getByLabelText("场景"), "startup");
  await userEvent.selectOptions(screen.getByLabelText("采集方式"), "manual");
  await userEvent.click(screen.getByRole("button", { name: "开始分析" }));
  expect(screen.getByText("请选择冷启动、温启动或热启动")).toBeInTheDocument();
});

it("renders exact scroll metadata fields", async () => {
  render(<TraceUploadForm api={fakeApi()} activeTeamId="team-1" />);
  await userEvent.selectOptions(screen.getByLabelText("场景"), "scroll");
  for (const label of [
    "应用包名",
    "测量开始时间（纳秒）",
    "测量结束时间（纳秒）",
    "目标 Display ID",
    "刷新率（Hz）",
    "刷新率模式",
  ]) {
    expect(screen.getByLabelText(label)).toBeInTheDocument();
  }
});
```

- [ ] **Step 2: Write the failing workflow test**

Select one primary Trace and two optional files. Assert that the browser hashes all three, creates one analysis, uploads each to its matching slot with the required signed headers, finalizes the exact set once, and navigates to the existing analysis route. Assert that it never calls a device, Agent, lease, or scenario endpoint.

- [ ] **Step 3: Run RED**

```bash
npm run test:unit -- \
  tests/trace-upload-form.test.tsx \
  tests/create-trace-analysis.test.ts \
  tests/new-analysis-dialog.test.tsx
```

Expected: FAIL because the Trace tab is still disabled from Phase 1.

- [ ] **Step 4: Implement accessible discriminated fields**

Enable the Trace tab. Both scenarios require Trace and package. Startup requires an independent startup-mode control and optional capture variant. Scroll requires integer start/end nanoseconds, integer target display, finite positive refresh rate, and fixed/variable mode. Optional uploads are capture manifest, APK, mapping, source archive, Native symbols, and log.

Client validation improves feedback but does not replace API validation. Show one progress row per file and never expose presigned URLs. Retain the same idempotency key across retry. Use the existing progress and report routes; those routes handle an empty child-scenario list by rendering parent-level Trace analysis state.

Hash and upload at most two files concurrently so large optional archives cannot exhaust browser memory or network sockets. Canceling aborts readers and PUT requests; resuming asks the API which immutable slots are already finalized before transferring bytes again.

Do not offer `memory_cycle`. Add explanatory text that memory analysis requires the device/local memory-cycle bundle.

- [ ] **Step 5: Add browser integration coverage**

Exercise startup and scroll uploads, malformed API error mapping, package mismatch leading to `invalid_capture`, missing manifest confidence ceilings, refresh recovery, cancel during analysis, artifact download, and API outage without mock fallback.

- [ ] **Step 6: Run GREEN and commit**

```bash
npm run test:unit -- \
  tests/trace-upload-form.test.tsx \
  tests/create-trace-analysis.test.ts \
  tests/new-analysis-dialog.test.tsx
npm run test:e2e -- tests/e2e/web-analysis.spec.ts
npm run lint
git add app/components/new-analysis-dialog.tsx app/components/trace-upload-form.tsx app/components/trace-upload-fields.tsx app/lib/analysis/create-trace-analysis.ts tests/trace-upload-factories.ts tests/trace-upload-form.test.tsx tests/create-trace-analysis.test.ts tests/new-analysis-dialog.test.tsx tests/e2e/web-analysis.spec.ts
git commit -m "feat: add direct Trace upload interface"
```

## Task 6: Pass cross-runtime acceptance, push, and deploy the exact Sites version

**Files:**
- Create: `tests/e2e/trace-upload-acceptance.spec.ts`
- Create: `tests/e2e/fixtures/trace-upload/README.md`
- Create: `docs/acceptance/trace-upload.md`
- Modify: `.github/workflows/platform-ci.yml`

- [ ] **Step 1: Add deterministic acceptance fixtures**

Reuse the protected raw Trace fixtures registered in `tracekit/tests/testdata/SHA256SUMS`; never copy them into the repository or Sites source:

```text
launch_light.pftrace
6c5479fd1b765ee4d29692c43a8204b972bc0f97eb373aca98ea7e11e99fd8b4

scroll_Standard-AOSP-App-Without-PreAnimation.pftrace
df7eebc5c6ff19b48331f8f1bca346612d86a5ae26eae202d46842a83f87a653
```

The protected fixture artifact also contains a hash-bound metadata companion with the true package, window, display, and startup facts. The fixture setup verifies all hashes before use and never downloads an unpinned Trace. If those true values cannot satisfy a scenario contract, reject that fixture and capture an approved replacement; do not invent values to make a test pass.

- [ ] **Step 2: Add cross-runtime acceptance assertions**

Run startup and scroll through both cloud adapters and local adapters. For each:

- create and finalize immutable uploads;
- prove zero scenario jobs, leases, schedule events, and Agent calls;
- recover from one Worker crash;
- reach the correct parent terminal state;
- validate the parent response against `AnalysisReport v1` and its one scenario entry against `AnalysisBundle v1`;
- verify package, window/startup semantics, input hashes, Perfetto/tool/SQL/rule versions, capability status, and provenance presence;
- verify missing capture manifest applies the required confidence ceiling;
- download the retained primary Trace through the authorized artifact route;
- reject cross-team reads and direct object substitution.

Also submit `memory_cycle` and require `unsupported_trace_scenario`.

- [ ] **Step 3: Run the complete Phase 3 gate**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv sync --locked --all-packages --all-extras --dev
uv run ruff check services tracekit agents local-runtime
uv run pytest -q
npm ci
npm run lint
npm test
npm run test:e2e
git diff --check
```

Expected: every command exits `0`; no raw customer data, credentials, object URLs, or generated reports are staged.

- [ ] **Step 4: Commit and fast-forward push**

```bash
git add tests/e2e/trace-upload-acceptance.spec.ts tests/e2e/fixtures/trace-upload/README.md docs/acceptance/trace-upload.md .github/workflows/platform-ci.yml
git commit -m "test: verify direct Trace analysis"
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
git status --short
```

Expected: the push is fast-forward, SHAs match, and the worktree is clean.

- [ ] **Step 5: Roll out the exact pushed API and Worker source**

On the private acceptance host, fetch and verify `local_sha`, build the API and Worker images from that detached tree, run forward migrations, and replace services only after readiness succeeds. Verify direct-parent claim support, protected fixture access, queue recovery, and the existing device-mode smoke test before changing the Web. If readiness or migration verification fails, retain the prior service image digests and do not deploy the new Sites version.

- [ ] **Step 6: Save and deploy the exact pushed Sites source**

Read `.openai/hosting.json`, reuse its opaque existing project ID, and confirm `d1` and `r2` remain null. Upload the exact tree at `local_sha`, save a Sites version whose `commit_sha` is `local_sha`, and deploy only that version with private access. Poll to a terminal deployment state. Do not create another site or deploy an unsaved/unpushed tree.

- [ ] **Step 7: Run private production smoke checks**

Through the deployed Web and same-origin proxy, perform a non-customer startup fixture upload, observe `uploading → analyzing → terminal`, open its report, and confirm the API reports no scenario job or lease. Verify the deployed version SHA equals local and remote `main`, record the version/deployment IDs and URL, and confirm `git status --short` is empty.
