# PerfPilot Trace Upload Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first honest browser workflow that creates a tenant-scoped Trace analysis, uploads its declared immutable inputs, starts the pinned SmartPerfetto engine, and shows real task state without demo fallback.

**Architecture:** Keep the existing Vinext UI and FastAPI control plane. The browser sends only small same-origin JSON requests through the Worker and uploads bytes directly to signed object-storage URLs. The API persists the closed Trace request in the routed tenant database, validates every upload against that request, and hands only finalized artifact claims to the existing `EngineExecutionService`.

**Tech Stack:** React 19, TypeScript 5.9, Vinext/Cloudflare Worker, FastAPI, Pydantic 2, SQLAlchemy 2, PostgreSQL, S3-compatible storage, SmartPerfetto Adapter, Vitest, pytest.

---

## File responsibilities

- `contracts/v1/analyses/*.schema.json`: closed public create/status shapes for `trace_upload`.
- `services/api/src/perfpilot_api/api/analyses.py`: HTTP validation and stable error mapping only.
- `services/api/src/perfpilot_api/services/analyses.py`: Trace parent creation, request hashing, input authorization, and parent-state projection.
- `services/api/src/perfpilot_api/db/tenant/models/apps.py`: tenant-private Trace profile and declared input manifest.
- `services/api/migrations/tenant/versions/0005_trace_upload_inputs.py`: forward/backward tenant schema migration.
- `services/api/src/perfpilot_api/services/trace_executions.py`: build an engine input manifest from finalized tenant artifacts and drive the existing SmartPerfetto execution service.
- `worker/index.ts`: same-origin `/api/v1/*` JSON proxy; it never proxies artifact bytes.
- `app/lib/perfpilot-api.ts`: browser API boundary, CSRF handling, hashing, signed PUT, finalize, and status polling.
- `app/components/trace-upload-form.tsx`: accessible Trace form and progress UI.
- `app/components/new-analysis-dialog.tsx`: mode selection and dialog lifecycle only.

### Task 1: Freeze and persist the Trace analysis request

**Files:**
- Modify: `contracts/v1/analyses/create-request.schema.json`
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/db/tenant/models/apps.py`
- Create: `services/api/migrations/tenant/versions/0005_trace_upload_inputs.py`
- Modify: `services/api/tests/unit/test_analysis_contracts.py`
- Modify: `services/api/tests/integration/test_analysis_api.py`
- Modify: `services/api/tests/integration/test_analysis_repository.py`
- Modify: `services/api/tests/integration/test_migrations.py`

- [ ] **Step 1: Write contract tests that define the closed request**

Add a valid `trace_upload` request with `analysis_profile` in `auto|startup|scroll`, an optional trimmed question, and an `inputs` array containing exactly one `trace` plus at most one of each allowed optional kind. Assert that duplicate kinds, a missing Trace, unknown fields, non-canonical SHA-256, and unsupported profiles fail validation.

```python
payload = {
    "schema_version": "1.0",
    "analysis_mode": "trace_upload",
    "analysis_profile": "auto",
    "question": "为什么滑动卡顿？",
    "inputs": [{
        "kind": "trace",
        "mime": "application/octet-stream",
        "size": 4096,
        "sha256_b64": _sha(),
    }],
}
validator.validate(payload)
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run:

```bash
uv run --package perfpilot-api pytest services/api/tests/unit/test_analysis_contracts.py -q
```

Expected: the valid Trace request fails because the schema currently accepts only `device` and `memory_upload`.

- [ ] **Step 3: Add persistence and migration tests**

Require `Analysis.analysis_profile` and `Analysis.input_manifest` only for `trace_upload`. The JSON manifest is tenant-private and contains only `kind`, `mime`, `size`, and canonical checksum. Verify upgrade, downgrade preflight, ORM constraints, and that non-Trace analyses reject these fields.

- [ ] **Step 4: Implement the minimal Trace creation service**

Add `canonical_trace_analysis_request_hash()` and `create_trace_analysis()`. Normalize the question with the same Python `str.strip()` rule used by memory uploads, sort the manifest by the fixed kind order before hashing, reserve the control job idempotently, insert the tenant parent, and complete the control reservation. A replay with the same key and semantically identical reordered inputs returns the same analysis; changed bytes return `idempotency_conflict`.

- [ ] **Step 5: Expose Trace create/status responses**

Add the Pydantic request branch and return `analysis_profile`, normalized `question`, and `input_uploads` only for `trace_upload`. Trace analyses have no device scenarios, APK-only field, lease, or sample counts. Do not return object keys or signed URLs from status reads.

- [ ] **Step 6: Run focused backend tests and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/integration/test_analysis_api.py \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q
uv run ruff check services/api/src/perfpilot_api services/api/tests
git diff --check
git add contracts/v1/analyses services/api/src/perfpilot_api services/api/migrations/tenant/versions services/api/tests
git commit -m "feat: create trace upload analyses"
git push -u origin feature/perfpilot-trace-upload-web
```

### Task 2: Bind immutable uploads to the declared Trace manifest

**Files:**
- Modify: `services/api/src/perfpilot_api/services/uploads.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/api/uploads.py`
- Modify: `services/api/tests/integration/test_upload_repository.py`
- Modify: `services/api/tests/integration/test_analysis_api.py`

- [ ] **Step 1: Write failing manifest-authorization tests**

For a Trace parent, require upload-slot idempotency key `input-<kind>` and an exact descriptor match against the persisted manifest. Assert that extra kinds, changed MIME/size/checksum, duplicate kinds, cross-team IDs, and direct object substitution are rejected without revealing whether another tenant owns the artifact.

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/integration/test_upload_repository.py \
  services/api/tests/integration/test_analysis_api.py -q
```

Expected: current generic upload slots accept descriptors that were not declared by the Trace request.

- [ ] **Step 3: Enforce the manifest and project progress**

Validate inside the routed tenant transaction before reserving a slot. Transition the tenant and control parent from `created` to `uploading` on the first authorized slot. Return every declared input as `awaiting_upload`, `pending`, or `finalized`; never mint a new signed URL during a status GET.

- [ ] **Step 4: Make finalization idempotent and readiness-aware**

After each exact finalize, recompute readiness from tenant artifacts. Only the required Trace gates SmartPerfetto submission; optional declared inputs that are still pending remain visible and are not silently omitted. Re-finalizing the same immutable object returns the same artifact.

- [ ] **Step 5: Run tests and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/integration/test_upload_repository.py \
  services/api/tests/integration/test_analysis_api.py -q
uv run ruff check services/api/src/perfpilot_api services/api/tests
git diff --check
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: bind trace inputs to immutable uploads"
git push
```

### Task 3: Start and project the SmartPerfetto execution

**Files:**
- Create: `services/api/src/perfpilot_api/services/trace_executions.py`
- Create: `services/api/src/perfpilot_api/workers/trace_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/api/src/perfpilot_api/config.py`
- Create: `services/api/tests/unit/test_trace_execution_service.py`
- Create: `services/api/tests/integration/test_trace_orchestrator.py`

- [ ] **Step 1: Write failing orchestration tests**

Given a finalized Trace, assert one deterministic input manifest hash, one SmartPerfetto attempt, the persisted tenant resource version, and `EngineInput(kind="trace", ...)` built from a short-lived version-bound download authorization. Assert no device, lease, scenario, or Agent records are created.

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_trace_execution_service.py \
  services/api/tests/integration/test_trace_orchestrator.py -q
```

Expected: `TraceExecutionService` and its worker entry point do not exist.

- [ ] **Step 3: Implement submission and recovery**

Build the execution seed from the engine lock, tenant route version, canonical manifest hash, and canonical config hash. Reuse `EngineExecutionService.create_attempt()`, `submit_attempt()`, and `step()`. Persist enough state that a restarted worker resumes the existing attempt rather than creating a second upstream run.

- [ ] **Step 4: Project stable parent states**

Map an allocated/submitted/running execution to parent `analyzing`; map completed to `completed`; map insufficient data to `partially_completed`; map terminal SmartPerfetto failure to `failed`; preserve retryable states. Update control and tenant rows with version-checked writes.

- [ ] **Step 5: Run tests and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_trace_execution_service.py \
  services/api/tests/integration/test_trace_orchestrator.py \
  services/api/tests/unit/test_engine_execution_service.py -q
uv run ruff check services/api/src/perfpilot_api services/api/tests
git diff --check
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: orchestrate trace analysis executions"
git push
```

### Task 4: Connect the existing dialog to the real API

**Files:**
- Create: `app/lib/perfpilot-api.ts`
- Create: `app/components/trace-upload-form.tsx`
- Modify: `app/components/new-analysis-dialog.tsx`
- Modify: `worker/index.ts`
- Create: `tests/perfpilot-api.test.ts`
- Create: `tests/trace-upload-form.test.tsx`
- Modify: `tests/new-analysis-dialog.test.tsx`

- [ ] **Step 1: Write failing browser workflow tests**

Select one Trace and optional attachments. Assert the client hashes files, creates one Trace parent, reserves exact slots, PUTs with only the required signed headers, finalizes each upload, keeps the same idempotency keys across retry, and polls the returned analysis ID. Assert that no device endpoint is called.

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- \
  tests/perfpilot-api.test.ts \
  tests/trace-upload-form.test.tsx \
  tests/new-analysis-dialog.test.tsx
```

Expected: the submit button is disabled and the browser API module does not exist.

- [ ] **Step 3: Implement the same-origin JSON boundary**

Proxy only `/api/v1/*` JSON requests to the configured FastAPI origin, preserve method/body/cookies, add the existing HMAC proxy signature, and strip hop-by-hop headers. Reject missing production origin/signing configuration. Signed object PUT URLs remain direct browser requests and never pass through the Worker.

- [ ] **Step 4: Implement bounded hashing and upload**

Hash files with Web Crypto, use the browser-reported MIME only when allowed and otherwise choose the contract MIME, upload at most two files concurrently, surface per-file progress/state, and abort hashing/PUT/polling when the user cancels. Do not log signed URLs or file contents.

- [ ] **Step 5: Enable the Trace form honestly**

Keep “真机自动测试” visibly unavailable. Enable “上传 Trace 分析”, require Trace and profile, retain optional APK/source/mapping/symbol inputs, add optional memory evidence and question, and replace the disabled footer with `开始分析` only in upload mode. On success, show the real analysis ID and progress instead of closing into demo data.

- [ ] **Step 6: Run tests and commit**

```bash
npm run test:unit -- \
  tests/perfpilot-api.test.ts \
  tests/trace-upload-form.test.tsx \
  tests/new-analysis-dialog.test.tsx
npm run lint
npm run build
git diff --check
git add app worker tests
git commit -m "feat: upload traces from the web"
git push
```

### Task 5: Show real status and finish the delivery gate

**Files:**
- Create: `app/analyses/[id]/page.tsx`
- Create: `app/components/analysis-progress.tsx`
- Create: `tests/analysis-progress.test.tsx`
- Modify: `tests/rendered-html.test.mjs`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing real-state rendering tests**

Cover `created`, `uploading`, `analyzing`, `completed`, `partially_completed`, `failed`, and `canceled`. Assert no fixture metric or demo problem appears when the API is unavailable.

- [ ] **Step 2: Implement the analysis progress route**

Render declared input status, SmartPerfetto state, stable failure text, retryability, and report availability from the API response. Poll only non-terminal analyses with bounded backoff and stop on navigation or cancellation.

- [ ] **Step 3: Run the complete gate**

```bash
uv run ruff check services/api/src/perfpilot_api services/api/tests
uv run --package perfpilot-api pytest -q
npm run lint
npm test
git diff --check
```

- [ ] **Step 4: Commit, push, and create the PR**

```bash
git add app tests .github/workflows/ci.yml
git commit -m "feat: show live trace analysis progress"
git push
gh pr create \
  --base main \
  --head feature/perfpilot-trace-upload-web \
  --title "feat: connect the trace upload workflow" \
  --body-file docs/superpowers/plans/2026-08-03-perfpilot-trace-upload-vertical-slice.md
```

- [ ] **Step 5: Deploy only after the API target is available**

Use the existing Sites project in `.openai/hosting.json`. Keep D1 and R2 null because PostgreSQL and the tenant S3 store remain authoritative. Deploy the exact pushed SHA only after the configured FastAPI origin passes health and a private startup Trace reaches a terminal state.
