# Background Analysis And Dashboard Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Trace uploads leave the modal as soon as the backend accepts them, show and cancel the real active analysis from the dashboard, and project the latest SmartPerfetto/PerfPilot report into every dashboard result section.

**Architecture:** Keep the API as the source of truth. The local runtime exposes one active analysis and an idempotent cancel endpoint, persists cancellation intent, cancels the SmartPerfetto session before stopping its local task, and rejects a second active analysis. The browser upload function ends after finalization; a dashboard controller restores and polls the active analysis. A pure report projector converts the existing Analysis Report 1.1 contract into dashboard-safe values, preferring AI synthesis but falling back to SmartPerfetto findings and never inventing missing thresholds or metrics.

**Tech Stack:** FastAPI, asyncio, SmartPerfetto HTTP adapter, Pydantic, pytest, React, TypeScript, Vinext, Vitest, Testing Library, CSS.

**Scope note:** This plan makes the current loopback/local product fully operational. The production PostgreSQL/worker cancellation coordinator remains a separate deployment task; the browser contract and state semantics introduced here are intentionally compatible with that later implementation.

---

### Task 1: Add active-analysis discovery and coordinated local cancellation

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Write failing local integration tests**

Add a blocking fake gateway with `cancel_calls`. Cover:

- `GET /v1/teams/{team_id}/analyses?status=active&limit=1` returns the newest non-terminal analysis and excludes completed reports;
- a second create returns HTTP 409 while the first analysis is active;
- `POST /v1/teams/{team_id}/analyses/{analysis_id}/cancel` requires CSRF, calls the gateway once, returns `canceled`, and is idempotent;
- a late gateway result cannot replace `canceled`;
- canceling an already completed analysis preserves its report.

The test gateway contract becomes:

```python
class _FakeSmartPerfettoGateway:
    async def cancel(self, run: LocalEngineRun) -> None:
        self.cancel_calls.append(run)
```

- [ ] **Step 2: Run the focused backend test and confirm RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/python -m pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -q
```

Expected failure: the protocol has no `cancel`, `status=active` is rejected or returns report history, and the cancel route is 404.

- [ ] **Step 3: Extend the gateway and persisted local analysis state**

In `LocalAnalysisGateway`, add:

```python
async def cancel(self, run: LocalEngineRun) -> None: ...
```

In `SmartPerfettoLocalGateway.cancel`, call:

```text
POST /api/workspaces/{workspace_id}/agent/{session_id}/cancel
```

Validate the response with the existing `SmartPerfettoCancelResponse` contract. Add `cancel_requested_at: datetime | None` to `_LocalAnalysis`, persist and restore it, and include its ISO timestamp (or `null`) in `response()`.

- [ ] **Step 4: Implement the runtime state transitions**

Add constants for active and terminal states, then implement:

```python
async def active_analyses(self, *, limit: int) -> tuple[_LocalAnalysis, ...]: ...
async def cancel(self, analysis: _LocalAnalysis) -> tuple[_LocalAnalysis, bool]: ...
```

`cancel()` must write the intent first, cancel the external SmartPerfetto run when present, cancel/await the local task, mark all unfinished stages `canceled`, persist the terminal state, and let a terminal completion win if it was already committed. `_execute`, `_execute_run`, and `_fail_analysis` must check cancellation before every late state write.

Inside `create()`, check under the existing lock for any active analysis and return HTTP 409 before inserting another one.

- [ ] **Step 5: Expose the local routes**

Keep the existing report query compatible and add:

```text
GET  /v1/teams/{team_id}/analyses?status=active&limit=1
POST /v1/teams/{team_id}/analyses/{analysis_id}/cancel
```

Return 202 for a newly accepted cancel and 200 for an already terminal analysis. Both responses use `runtime.response()`.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run the command from Step 2, then:

```bash
.venv/bin/ruff check services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py
```

- [ ] **Step 7: Commit only the local backend boundary**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py
git commit -m "feat: coordinate local analysis cancellation"
```

### Task 2: Add browser active/cancel methods and end submission after upload

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: Write failing browser API tests**

Change the upload test so it expects no sleep and exactly one post-finalize `analysis()` read. Add request/validation tests for:

```typescript
client.activeAnalyses(teamId, 1, signal)
client.cancelAnalysis(teamId, analysisId, signal)
```

Validate `created_at`, nullable `cancel_requested_at`, team identity, active states, and the exact URLs. Ensure the existing `analyses()` report-history parser still requires `report_available: true`.

- [ ] **Step 2: Run the focused frontend test and confirm RED**

```bash
npm run test:unit -- tests/perfpilot-api.test.ts
```

Expected failure: the two client methods and response fields do not exist, and the current submitter continues polling terminal state.

- [ ] **Step 3: Implement the typed client contract**

Add to `AnalysisResponse`:

```typescript
readonly created_at?: string;
readonly cancel_requested_at?: string | null;
```

Add list parsing modes so report history requires a report while active history requires a non-terminal state. Extend `PerfPilotClient` with `activeAnalyses` and `cancelAnalysis`; the cancel request uses `POST` and the existing CSRF header path.

- [ ] **Step 4: Replace terminal polling with enqueue semantics**

Rename the operational function to:

```typescript
export async function enqueueTraceAnalysis(...): Promise<SubmittedTraceAnalysis>
```

After all uploads finalize, read the analysis exactly once and return it. Keep `submitTraceAnalysis` as a compatibility alias during this migration. Replace the terminal phases with `submitted`; remove the sleep dependency and terminal polling loop.

- [ ] **Step 5: Run the focused test and confirm GREEN**

```bash
npm run test:unit -- tests/perfpilot-api.test.ts
```

### Task 3: Close the modal and render the real active task card

**Files:**
- Modify: `app/components/trace-upload-form.tsx`
- Modify: `app/components/new-analysis-dialog.tsx`
- Create: `app/components/active-analysis-task-card.tsx`
- Modify: `app/components/dashboard.tsx`
- Modify: `app/components/latest-analysis-report-entry.tsx`
- Modify: `app/globals.css`
- Modify: `tests/trace-upload-form.test.tsx`
- Create: `tests/active-analysis-task-card.test.tsx`
- Create: `tests/dashboard-analysis-coordinator.test.tsx`
- Modify: `tests/latest-analysis-report-entry.test.tsx`

- [ ] **Step 1: Write failing component tests**

Cover:

- `TraceUploadForm` invokes `onSubmitted(result)` and does not render an analysis-result panel;
- `NewAnalysisDialog` closes after `onSubmitted`;
- the dashboard restores one active task without `localStorage`;
- the task card shows the actual SmartPerfetto/AI/report stage, elapsed time, the fixed 3–8 minute guidance, and a real detail link;
- polling retains the last state during retryable network errors and stops on terminal state;
- cancel requires `window.confirm`, disables repeated clicks, calls `cancelAnalysis`, and only displays canceled after the API returns it;
- an active task disables a second analysis submission;
- completion refreshes the latest report entry.

- [ ] **Step 2: Run focused component tests and confirm RED**

```bash
npm run test:unit -- tests/trace-upload-form.test.tsx tests/active-analysis-task-card.test.tsx tests/dashboard-analysis-coordinator.test.tsx tests/latest-analysis-report-entry.test.tsx
```

- [ ] **Step 3: Implement the submission callback boundary**

`TraceUploadForm` uses `enqueueTraceAnalysis`, accepts:

```typescript
readonly onSubmitted?: (result: SubmittedTraceAnalysis) => void;
```

and calls it immediately after acceptance. `NewAnalysisDialog` forwards the result, closes itself, and supports a disabled/current-analysis state from the dashboard.

- [ ] **Step 4: Implement `ActiveAnalysisTaskCard`**

Keep it presentational. Export one stage-label function so tests can cover every state. Render no fake percentage. Show a details disclosure with all four server stages, `/analyses/{analysis_id}`, and a secondary cancel button.

- [ ] **Step 5: Implement the dashboard coordinator**

Make `Dashboard` a client controller. On mount, resolve the team and `activeAnalyses(..., 1)`. Poll the known analysis by ID every two seconds with bounded retry, retaining the last successful state. On submit, install the returned analysis immediately. On terminal completion, refresh the latest report. On authoritative cancellation, keep the canceled card for three seconds, then remove it.

Refactor `LatestAnalysisReportEntry` just enough to report its loaded `LatestReportSnapshot` to the dashboard and accept a refresh token. Remove its duplicate empty-state `NewAnalysisDialog`; the page header remains the single submission entry.

- [ ] **Step 6: Add focused task-card styles**

Place the card below `.page-header` and above `.latest-report-entry`. Reuse the existing radius, border, foreground, muted text, and accent tokens. Add visible keyboard focus and a compact four-stage details row; do not add a mobile product variant.

- [ ] **Step 7: Run focused tests and commit the browser task boundary**

```bash
npm run test:unit -- tests/perfpilot-api.test.ts tests/trace-upload-form.test.tsx tests/active-analysis-task-card.test.tsx tests/dashboard-analysis-coordinator.test.tsx tests/latest-analysis-report-entry.test.tsx
git add app/lib/perfpilot-api.ts app/components/trace-upload-form.tsx app/components/new-analysis-dialog.tsx app/components/active-analysis-task-card.tsx app/components/dashboard.tsx app/components/latest-analysis-report-entry.tsx app/globals.css tests/perfpilot-api.test.ts tests/trace-upload-form.test.tsx tests/active-analysis-task-card.test.tsx tests/dashboard-analysis-coordinator.test.tsx tests/latest-analysis-report-entry.test.tsx
git commit -m "feat: move trace analysis into dashboard background task"
```

### Task 4: Project the final report into every dashboard result slot

**Files:**
- Create: `app/lib/dashboard-report.ts`
- Modify: `app/components/dashboard.tsx`
- Modify: `app/components/latest-analysis-report-entry.tsx`
- Modify: `app/globals.css`
- Create: `tests/dashboard-report.test.ts`
- Modify: `tests/dashboard-analysis-coordinator.test.tsx`

- [ ] **Step 1: Write failing pure projection tests**

Build fixtures for a completed AI report, a SmartPerfetto-only partial report, and a report with missing metric families. Assert:

- AI `executive_summary` wins when available;
- otherwise the highest-severity/highest-confidence SmartPerfetto finding becomes the conclusion;
- startup duration, TTID, TTFD, sample count, main-thread time/share, and CPU metrics are selected by stable metric names;
- absent threshold renders `未配置阈值`;
- absent smoothness or memory evidence renders `本次 Trace 未采集`;
- top findings link to `/analyses/{analysis_id}/report#finding-{finding_id}`;
- markdown markers are removed from dashboard copy;
- evidence, metric, source-verification, failed-stage, and AI-state counts come from the real response.

- [ ] **Step 2: Run the projector test and confirm RED**

```bash
npm run test:unit -- tests/dashboard-report.test.ts
```

- [ ] **Step 3: Implement `projectDashboardReport`**

Export a pure function:

```typescript
export function projectDashboardReport(
  snapshot: LatestReportSnapshot,
): DashboardReportProjection
```

Flatten only non-null scenario bundles. Select exact known metric names first and conservative suffix matches second. Format values without changing units. Never synthesize cold/warm/hot startup classes, device consistency, thermal state, or target values that are absent from the contract.

- [ ] **Step 4: Bind the projection into the existing dashboard layout**

Replace the hard-coded empty content in these existing sections while preserving their positions:

- `.conclusion-hero`: AI summary or SmartPerfetto fallback conclusion;
- `.core-overview`: startup, TTID/TTFD, main-thread, CPU, and honest missing states;
- `.focus-section`: up to three real findings with severity/confidence and report anchors;
- `.data-credibility`: real sample count, available metric count, evidence count, source verification, failed stages, and AI status.

When no report exists, keep the complete layout and its current empty placeholders.

- [ ] **Step 5: Run focused dashboard tests and commit the projection boundary**

```bash
npm run test:unit -- tests/dashboard-report.test.ts tests/dashboard-analysis-coordinator.test.tsx tests/latest-analysis-report-entry.test.tsx
git add app/lib/dashboard-report.ts app/components/dashboard.tsx app/components/latest-analysis-report-entry.tsx app/globals.css tests/dashboard-report.test.ts tests/dashboard-analysis-coordinator.test.tsx tests/latest-analysis-report-entry.test.tsx
git commit -m "fix: populate dashboard from the latest trace report"
```

### Task 5: Verify the complete local path

**Files:**
- Verify only unless a test exposes a defect.

- [ ] **Step 1: Run backend verification**

```bash
PYTHONPATH=services/api/src .venv/bin/python -m pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -q
PYTHONPATH=services/api/src .venv/bin/python -m pytest -p no:cacheprovider services/api/tests -q
.venv/bin/ruff check services/api/src services/api/tests
```

- [ ] **Step 2: Run frontend verification**

```bash
npm run test:unit
npm run test:ssr
npm run lint
```

- [ ] **Step 3: Restart the clean local stack and test the real API**

`npm run dev:restart` intentionally removes local analysis history. Upload a Trace and verify:

1. the modal closes after upload acceptance;
2. the dashboard task card survives a browser refresh;
3. cancel stops the SmartPerfetto session and reaches `canceled`;
4. a completed run refreshes the latest-report entry;
5. conclusion, core metrics, focus findings, and credibility values all use the generated report;
6. the report link opens `/analyses/{analysis_id}/report`.

- [ ] **Step 4: Inspect commit boundaries**

```bash
git show --stat --oneline HEAD~2..HEAD
git status --short
```

Confirm only the explicitly staged files entered each commit and unrelated dirty worktree changes remain untouched.
