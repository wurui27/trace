# Dashboard Latest Report Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real, tenant-scoped “latest analysis report” entry to the desktop dashboard and open the existing final report route from that entry.

**Architecture:** Add one read-only analyses collection endpoint to both the production API and local runtime. Keep tenant isolation in the existing team router and tenant database, sort report-bearing analyses newest-first, and expose the same response shape locally. A small client component resolves the current team, loads one report summary plus its report metadata, and renders loading, available, empty, and error states without changing the existing static dashboard data.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, pytest, React, TypeScript, Vinext, Vitest, Testing Library, CSS.

---

### Task 1: Add the production latest-report query

**Files:**
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Test: `services/api/tests/unit/test_analysis_service.py`
- Test: `services/api/tests/integration/test_analysis_api.py`

- [ ] **Step 1: Write failing service and API tests**

Cover tenant-scoped results, `report_available=true`, newest-first ordering, `limit=1`, and bounds validation.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run the exact test nodes added in Step 1 and confirm failure because list support does not exist.

- [ ] **Step 3: Implement the repository and service query**

Add a repository operation that selects non-deleted analyses with a top-level persisted report, ordered by `Analysis.created_at DESC, Analysis.id DESC`, with a bounded limit. Add a service method that validates `1 <= limit <= 20` and loads the corresponding `AnalysisView` records.

- [ ] **Step 4: Expose the collection route**

Add `GET /v1/teams/{team_id}/analyses?report_available=true&limit=1`, preserve existing authorization and no-store behavior, and return:

```json
{
  "schema_version": "1.0",
  "analyses": []
}
```

- [ ] **Step 5: Run focused tests and confirm GREEN**

### Task 2: Add equivalent local-runtime discovery and persistence

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/integration/test_local_app.py`
- Test: `services/api/tests/unit/test_local_analysis_store.py`

- [ ] **Step 1: Write failing local API and restore tests**

Cover newest report selection, exclusion of analyses without a final report, persistence of `created_at`, and deterministic restoration of legacy state without `created_at`.

- [ ] **Step 2: Run the focused tests and confirm RED**

- [ ] **Step 3: Persist and restore creation time**

Store an ISO-8601 UTC `created_at`. For legacy state, prefer the persisted report generation timestamp; otherwise use a stable earliest timestamp rather than the current time.

- [ ] **Step 4: Implement the local collection route**

Return the same schema as production, filtered to analyses with reports and sorted newest-first.

- [ ] **Step 5: Run focused tests and confirm GREEN**

### Task 3: Add the typed web client method

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Test: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: Write failing response-validation and request tests**

Cover the exact collection schema, nested analysis validation, query parameters, empty results, malformed payloads, and abort propagation.

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `npm test -- tests/perfpilot-api.test.ts`

- [ ] **Step 3: Implement `AnalysisListResponse` and `PerfPilotClient.analyses`**

Request `/api/v1/teams/{team_id}/analyses?report_available=true&limit=1` and validate the response before exposing it to UI code.

- [ ] **Step 4: Run the focused test and confirm GREEN**

### Task 4: Build and integrate the dashboard entry

**Files:**
- Create: `app/components/latest-analysis-report-entry.tsx`
- Modify: `app/components/dashboard.tsx`
- Modify: `app/globals.css`
- Create: `tests/latest-analysis-report-entry.test.tsx`
- Modify: `tests/rendered-html.test.mjs`

- [ ] **Step 1: Write failing component tests**

Cover the loading skeleton, real report link, metadata, empty state, error state, and cancellation on unmount. Ensure no hard-coded analysis ID is rendered.

- [ ] **Step 2: Run the focused component tests and confirm RED**

Run: `npm test -- tests/latest-analysis-report-entry.test.tsx`

- [ ] **Step 3: Implement the client leaf**

Resolve the current team using `me()`, query one available analysis, fetch its final report metadata, and render exactly one primary action linking to `/analyses/{analysis_id}/report`.

- [ ] **Step 4: Integrate immediately below the dashboard title**

Keep the existing desktop light theme, radius scale, icon family, and accent color. Use a compact horizontal strip with CSS-only hover/active feedback and no mobile-specific feature work.

- [ ] **Step 5: Run component and SSR tests and confirm GREEN**

### Task 5: Verify the complete path

**Files:**
- Verify only unless a test exposes a defect.

- [ ] **Step 1: Run backend focused and full tests**

Run the changed backend test files, then the full backend suite.

- [ ] **Step 2: Run frontend tests, type/build checks, and lint**

Run the full Vitest suite, SSR checks, production build, ESLint, and Ruff using the repository’s existing commands.

- [ ] **Step 3: Browser-test the real flow**

Open the desktop dashboard, confirm the real latest report is shown, click “打开报告”, and verify the existing full report route renders the corresponding analysis ID.

- [ ] **Step 4: Run the visual pre-flight**

Check copy, contrast, focus visibility, one-line CTA, loading/empty/error states, stable layout, no duplicate report CTA, and no hard-coded demo report data.
