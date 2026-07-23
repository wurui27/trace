# PerfPilot Phase 1 Web Integration and Real-Device Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the accepted PerfPilot interface to the real control plane, preserve secure same-origin browser behavior, display trustworthy task and report data, and pass the complete RKGallery real-device acceptance gate.

**Architecture:** The existing Sites/Vinext application remains the browser UI. Its Cloudflare Worker proxies only small `/api/v1/...` JSON requests to FastAPI and authenticates each hop with an HMAC signature. The browser uploads large artifacts directly to a presigned object-store URL, polls task state every two seconds until terminal, and renders only contract-validated API data. Phase 1 finishes with a private Sites deployment whose source commit is the exact pushed GitHub SHA and with one recorded real-device RKGallery acceptance run.

**Tech Stack:** React 19, Next.js 16/Vinext, TypeScript 5.9, Cloudflare Workers, Web Crypto, `@noble/hashes` 2.2.0, Vitest, Testing Library, Node test runner, Playwright, FastAPI contract fixtures, Sites private hosting.

---

## File map

- Create `worker/api-proxy.ts`: allowlisted same-origin proxy, request signing, response cookie rewriting, and timeouts.
- Modify `worker/index.ts`: route `/api/v1/...` before image and application handlers.
- Create `app/lib/api/`: contract types, HTTP client, CSRF state, upload helper, polling, and view adapters.
- Create `app/login/`: product login route and accessible form.
- Modify `app/components/new-analysis-dialog.tsx`: real device-mode upload workflow with no fake device data.
- Create `app/teams/[teamId]/analyses/[analysisId]/`: task progress and report routes.
- Modify existing dashboard/problem components to accept `AnalysisBundle v1` view models.
- Create `app/admin/`: Agent and device health pages restricted by the backend role.
- Create `tests/fixtures/contracts/`: checked-in API examples copied from `contracts/v1/examples`.
- Create `tests/e2e/`: browser integration and RKGallery acceptance tests.
- Modify `package.json`, `package-lock.json`, `vitest.config.ts`, and `.github/workflows/platform-ci.yml`.
- Preserve `.openai/hosting.json` exactly, including its existing project ID and `d1: null`, `r2: null`.

## Task 1: Add the signed same-origin API proxy

**Files:**
- Create: `worker/api-proxy.ts`
- Create: `tests/api-proxy.test.ts`
- Modify: `worker/index.ts`

- [ ] **Step 1: Write failing proxy tests**

```ts
import { describe, expect, it, vi } from "vitest";
import { proxyApiRequest } from "../worker/api-proxy";

const env = {
  PERFPILOT_API_ORIGIN: "https://api.perfpilot.test",
  PERFPILOT_PROXY_SECRET: "proxy-test-secret-with-at-least-32-bytes",
};

it("signs the exact upstream method path and body", async () => {
  const upstream = vi.fn(async (request: Request) => {
    expect(request.url).toBe("https://api.perfpilot.test/v1/me?include=teams");
    expect(request.headers.get("x-perfpilot-proxy-signature")).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(request.headers.get("x-perfpilot-proxy-timestamp")).toBe("1784736000");
    expect(request.headers.get("x-request-id")).toBe("req-fixed");
    return new Response('{"schema_version":"1.0"}', { status: 200 });
  });
  const response = await proxyApiRequest(
    new Request("https://web.test/api/v1/me?include=teams"),
    env,
    { fetch: upstream, nowSeconds: () => 1784736000, requestId: () => "req-fixed" },
  );
  expect(response.status).toBe(200);
  expect(upstream).toHaveBeenCalledOnce();
});

it("removes Domain from every Set-Cookie value", async () => {
  const response = await proxyApiRequest(
    new Request("https://web.test/api/v1/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    }),
    env,
    {
      fetch: async () => new Response("{}", {
        headers: [["set-cookie", "perfpilot_session=abc; Domain=api.test; Path=/; Secure; HttpOnly"]],
      }),
      nowSeconds: () => 1784736000,
      requestId: () => "req-cookie",
    },
  );
  expect(response.headers.get("set-cookie")).toBe(
    "perfpilot_session=abc; Path=/; Secure; HttpOnly",
  );
});
```

Also cover rejection of paths outside `/api/v1/`, encoded slash/backslash/dot traversal, methods outside `GET|POST|PUT|PATCH|DELETE|HEAD`, bodies over 1 MiB, disallowed forwarding headers, missing/malformed proxy configuration, upstream timeouts, and client attempts to provide proxy identity headers.

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/api-proxy.test.ts
```

Expected: FAIL because `worker/api-proxy.ts` does not exist.

- [ ] **Step 3: Implement the exact signing and proxy boundary**

Define:

```ts
export interface ProxyEnv {
  PERFPILOT_API_ORIGIN: string;
  PERFPILOT_PROXY_SECRET: string;
}

export interface ProxyDependencies {
  fetch: typeof globalThis.fetch;
  nowSeconds: () => number;
  requestId: () => string;
}
```

Strip `/api` and retain `/v1/...` plus its query string upstream. Hash the raw request bytes with SHA-256. Sign this newline-delimited value with HMAC-SHA256 and Base64URL without padding:

```text
timestamp
request_id
METHOD
/v1/path?raw=query
lowercase_hex_body_sha256
```

Use the upstream pathname plus the URL’s unmodified raw query string in the signature; do not sort, decode, or re-encode query parameters between signing and forwarding. Generate a new request ID unless an incoming `x-request-id` matches `^[A-Za-z0-9._:-]{1,128}$`. Remove all incoming `forwarded`, `x-forwarded-*`, `x-perfpilot-*`, `host`, and `content-length` headers before adding server-owned values. Forward only `accept`, `content-type`, `cookie`, `origin`, `referer`, `user-agent`, `x-csrf-token`, and `idempotency-key`. Abort upstream requests after 15 seconds. Never add wildcard CORS headers.

Rewrite each `Set-Cookie` value independently, remove only its `Domain` attribute, and preserve `Path`, `Secure`, `HttpOnly`, `SameSite`, `Max-Age`, and `Expires`.

- [ ] **Step 4: Route the proxy before Vinext**

In `worker/index.ts`, extend `Env` with the two required secrets and call:

```ts
if (url.pathname.startsWith("/api/v1/")) {
  return proxyApiRequest(request, env);
}
```

Do not modify the image optimization or application handler branches. Remove the unused required `DB` binding from the TypeScript `Env` interface; do not add D1 or R2 bindings to `.openai/hosting.json`.

- [ ] **Step 5: Run GREEN and commit**

```bash
npm run test:unit -- tests/api-proxy.test.ts
npm run lint
git add worker/api-proxy.ts worker/index.ts tests/api-proxy.test.ts
git commit -m "feat: add signed same-origin API proxy"
```

## Task 2: Add contract-validated browser API and upload clients

**Files:**
- Create: `app/lib/api/types.ts`
- Create: `app/lib/api/errors.ts`
- Create: `app/lib/api/client.ts`
- Create: `app/lib/api/csrf.ts`
- Create: `app/lib/api/hash.ts`
- Create: `app/lib/api/uploads.ts`
- Create: `app/lib/api/polling.ts`
- Create: `tests/api-client.test.ts`
- Create: `tests/upload-client.test.ts`
- Create: `tests/factories.ts`
- Create: `tests/fixtures/contracts/`
- Modify: `package.json`
- Modify: `package-lock.json`

- [ ] **Step 1: Check in the frontend contract fixtures**

Copy only valid, non-secret examples from `contracts/v1/examples/` into `tests/fixtures/contracts/` during implementation. The fixture copy is test data; `contracts/v1` remains authoritative. Add a test that serializes each copied fixture and verifies the source and copy are byte-identical.

Create `tests/factories.ts` with deterministic exports `analysisResponse`, `createRequest`, `uploadSlot`, `streamingFileWithThrowingArrayBuffer`, `fakeUploadApi`, `fakeSessionApi`, `apiError`, `fakeAnalysisApi`, `sequencedAnalysisApi`, `partiallyCompletedFixture`, `analysisBundleFixture`, `insufficientDataBundle`, `partiallyCompletedReportFixture`, `teamA`, and `teamMemberSession`. Each fake records ordered calls, accepts injected responses, and throws on an undeclared endpoint. Every test module explicitly imports the factories it uses; local spies such as `mockReplace` are declared in that test before rendering.

- [ ] **Step 2: Write failing API client tests**

```ts
it("sends cookies CSRF and idempotency through same-origin api only", async () => {
  const fetcher = vi.fn(async () => Response.json(analysisResponse));
  const client = createApiClient({ fetcher, csrfToken: () => "csrf-test" });
  await client.createDeviceAnalysis("team-1", createRequest, "idem-1");
  const [url, init] = fetcher.mock.calls[0];
  expect(url).toBe("/api/v1/teams/team-1/analyses");
  expect(init.credentials).toBe("same-origin");
  expect(new Headers(init.headers).get("x-csrf-token")).toBe("csrf-test");
  expect(new Headers(init.headers).get("idempotency-key")).toBe("idem-1");
});

it("raises a typed stable API error without parsing message", async () => {
  const client = createApiClient({
    fetcher: async () => Response.json(
      {
        schema_version: "1.0",
        error: {
          code: "tenant_store_unavailable",
          message: "展示文本会变化",
          retryable: true,
          request_id: "req-1",
        },
      },
      { status: 503 },
    ),
    csrfToken: () => undefined,
  });
  await expect(client.me()).rejects.toMatchObject({
    code: "tenant_store_unavailable",
    retryable: true,
    requestId: "req-1",
  });
});
```

- [ ] **Step 3: Write the failing direct-upload test**

```ts
it("uses every signed header and finalizes the same checksum and size", async () => {
  const api = fakeUploadApi();
  const file = new File([new Uint8Array([1, 2, 3])], "fixture.apk", {
    type: "application/vnd.android.package-archive",
  });
  await uploadArtifact({ api, file, slot: uploadSlot, fetcher: api.objectFetch });
  expect(api.putHeaders()).toEqual(uploadSlot.required_headers);
  expect(api.finalizeBody()).toEqual({
    upload_id: uploadSlot.upload_id,
    sha256_b64: await sha256Base64(file),
    size: 3,
  });
});

it("hashes a large file incrementally without calling file.arrayBuffer", async () => {
  const file = streamingFileWithThrowingArrayBuffer(9 * 1024 * 1024);
  const digest = await sha256Base64(file);
  expect(digest).toBe(file.expectedSha256Base64);
  expect(file.maxReadSize).toBeLessThanOrEqual(4 * 1024 * 1024);
});
```

- [ ] **Step 4: Run RED**

```bash
npm run test:unit -- tests/api-client.test.ts tests/upload-client.test.ts
```

Expected: FAIL because `app/lib/api` has not been created.

- [ ] **Step 5: Implement the clients**

Model UUIDs as opaque `string` values and preserve the contract names exactly. Define `ApiError`, `SessionResponse`, `MeResponse`, `UploadSlot`, `AnalysisResponse`, `ScenarioProgress`, `AnalysisReport`, `ScenarioReport`, `AnalysisBundle`, `Metric`, `Finding`, `Evidence`, and `ArtifactSummary` from the checked-in schemas. Reject an unsupported major `schema_version`.

`requestJson` must:

- resolve only relative URLs beginning `/api/v1/`;
- use `credentials: "same-origin"`;
- add CSRF only to state-changing calls;
- set `Idempotency-Key` only when supplied by the caller;
- accept JSON only and enforce a 10 MiB response limit;
- throw `ApiError` from stable `code`, `retryable`, and `request_id`;
- map malformed success and error bodies to `invalid_api_response`.

Install the browser-compatible hash package exactly:

```bash
npm install --save-exact @noble/hashes@2.2.0
```

`sha256Base64(file)` imports `sha256` from `@noble/hashes/sha2.js`, reads `File.stream()` incrementally, splits any larger read into pieces no larger than 4 MiB, calls `sha256.create().update(chunk)` for each piece, and converts the final 32-byte digest to standard Base64. It honors `AbortSignal`, releases the stream reader, and never calls `file.arrayBuffer()`.

`uploadArtifact` uses that incremental digest, verifies the returned slot repeats the expected size/checksum/MIME, performs the presigned `PUT` directly, and calls `finalize-upload` with the same values. A definite expired-authorization response requests a replacement slot with the same artifact descriptor and idempotency key; ambiguous network failure first asks the API for current slot/finalize state and never blindly creates a second object. It must never log the presigned URL.

`pollUntilTerminal` waits two seconds between successful reads, uses bounded exponential delay up to 15 seconds for retryable network errors, stops immediately on unmount/abort, and stops for `completed`, `partially_completed`, `failed`, `canceled`, or `deleted`.

- [ ] **Step 6: Run GREEN and commit**

```bash
npm run test:unit -- tests/api-client.test.ts tests/upload-client.test.ts
npm run lint
git add app/lib/api tests/api-client.test.ts tests/upload-client.test.ts tests/factories.ts tests/fixtures/contracts package.json package-lock.json
git commit -m "feat: add typed browser API client"
```

## Task 3: Replace the development shell with product session login

**Files:**
- Create: `app/login/page.tsx`
- Create: `app/login/login-form.tsx`
- Create: `app/components/session-gate.tsx`
- Create: `app/lib/api/session.ts`
- Create: `tests/login-form.test.tsx`
- Create: `tests/session-gate.test.tsx`
- Modify: `app/layout.tsx`
- Modify: `app/components/app-shell.tsx`
- Verify unchanged: `app/chatgpt-auth.ts`

- [ ] **Step 1: Write failing session tests**

```tsx
it("gets a pre-auth CSRF token before posting credentials", async () => {
  const api = fakeSessionApi();
  await userEvent.type(screen.getByLabelText("账号"), "platform-admin");
  await userEvent.type(screen.getByLabelText("密码"), "development-secret");
  await userEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(api.calls).toEqual(["csrf", "login", "me"]);
  expect(screen.getByText("选择团队")).toBeInTheDocument();
});

it("redirects an unauthenticated protected route to login", async () => {
  render(<SessionGate loadSession={async () => { throw apiError("unauthenticated"); }} />);
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/login"));
});
```

Also assert that the password input value and login request body never appear in rendered errors, console calls, analytics calls, or persisted browser storage.

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/login-form.test.tsx tests/session-gate.test.tsx
```

Expected: FAIL because the login route and session gate are absent.

- [ ] **Step 3: Implement login and session refresh**

The form has account and password fields, an error summary with `role="alert"`, a single busy submit state, and no password visibility in URLs or storage. Submit `GET /auth/csrf`, then `POST /auth/login`, then `GET /me`; replace history with the selected team home.

`SessionGate` holds only the current user, team summaries, active team ID, and CSRF token in memory. Refresh `/me` after navigation and on window focus, but never poll it continuously. A `401 unauthenticated` clears state and navigates to `/login`; `403 role_forbidden` renders an access-denied state without logging out.

Keep the existing, currently unimported `app/chatgpt-auth.ts` unchanged as a Sites-specific utility. The private Sites access policy supplies the outer hosting gate; product authorization uses FastAPI session and team roles. Add a static test that no product route imports `requireChatGPTUser` or infers a product role from Sites identity headers.

- [ ] **Step 4: Run GREEN and commit**

```bash
npm run test:unit -- tests/login-form.test.tsx tests/session-gate.test.tsx
npm run test:ssr
git add app/login app/components/session-gate.tsx app/lib/api/session.ts app/layout.tsx app/components/app-shell.tsx tests/login-form.test.tsx tests/session-gate.test.tsx
git commit -m "feat: connect product session login"
```

## Task 4: Implement the real APK analysis workflow

**Files:**
- Modify: `app/components/new-analysis-dialog.tsx`
- Create: `app/components/analysis-upload-progress.tsx`
- Create: `app/lib/analysis/create-device-analysis.ts`
- Modify: `tests/new-analysis-dialog.test.tsx`
- Create: `tests/create-device-analysis.test.ts`

- [ ] **Step 1: Replace mock expectations with failing workflow tests**

```tsx
it("creates and uploads one device analysis with all three fixed scenarios", async () => {
  const api = fakeAnalysisApi();
  render(<NewAnalysisDialog api={api} activeTeamId="team-1" />);
  await userEvent.upload(
    screen.getByLabelText("APK 文件"),
    new File([new Uint8Array([1, 2, 3])], "RKGallery.apk", {
      type: "application/vnd.android.package-archive",
    }),
  );
  await userEvent.click(screen.getByRole("button", { name: "开始分析" }));
  expect(api.createBody()).toMatchObject({
    schema_version: "1.0",
    analysis_mode: "device",
    scenarios: ["cold_start", "scroll", "memory_cycle"],
  });
  expect(api.objectPutCount()).toBe(1);
  expect(api.finalizeCount()).toBe(1);
});

it("does not render a fabricated Pixel device or selectable fake scenario", () => {
  render(<NewAnalysisDialog api={fakeAnalysisApi()} activeTeamId="team-1" />);
  expect(screen.queryByText(/Pixel 8/)).not.toBeInTheDocument();
  expect(screen.getByText("自动选择兼容设备")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/new-analysis-dialog.test.tsx tests/create-device-analysis.test.ts
```

Expected: FAIL because the current dialog is a disabled mock form.

- [ ] **Step 3: Implement the device workflow**

Phase 1 exposes only the device tab; render the Trace-upload tab as disabled with the explicit text “第三阶段开放”. Require one `.apk` file, validate non-zero size and the API-advertised maximum, and let the scheduler choose a compatible device. Optional device constraints may contain only backend-provided labels; never show hard-coded devices.

Use one generated UUID as the idempotency key for the entire submit attempt:

1. hash the APK;
2. create `analysis_mode=device` with APK MIME, bytes, checksum, the fixed three scenarios, and the RKGallery fixture reference when selected;
3. upload to the returned slot;
4. call `finalize-upload`;
5. navigate to `/teams/{teamId}/analyses/{analysisId}`.

Retry after a network error with the same idempotency key. Generate a new key only after a terminal API error or a successful submission. Expose distinct “计算校验值”, “创建任务”, “上传 APK”, and “启动分析” progress labels. Canceling the dialog aborts browser work but does not delete a server task already created.

- [ ] **Step 4: Run GREEN and commit**

```bash
npm run test:unit -- tests/new-analysis-dialog.test.tsx tests/create-device-analysis.test.ts
npm run lint
git add app/components/new-analysis-dialog.tsx app/components/analysis-upload-progress.tsx app/lib/analysis/create-device-analysis.ts tests/new-analysis-dialog.test.tsx tests/create-device-analysis.test.ts
git commit -m "feat: submit real device analyses"
```

## Task 5: Add task progress, cancellation, and partial completion

**Files:**
- Create: `app/teams/[teamId]/analyses/[analysisId]/page.tsx`
- Create: `app/teams/[teamId]/analyses/[analysisId]/analysis-progress.tsx`
- Create: `app/components/scenario-progress-card.tsx`
- Create: `app/lib/analysis/progress.ts`
- Create: `tests/analysis-progress.test.tsx`
- Create: `tests/polling.test.ts`

- [ ] **Step 1: Write failing progress tests**

```tsx
it("polls every two seconds and stops at partial completion", async () => {
  vi.useFakeTimers();
  const api = sequencedAnalysisApi(["queued", "running", "partially_completed"]);
  render(<AnalysisProgress api={api} teamId="team-1" analysisId="analysis-1" />);
  await vi.advanceTimersByTimeAsync(4_100);
  expect(api.readCount()).toBe(3);
  await vi.advanceTimersByTimeAsync(10_000);
  expect(api.readCount()).toBe(3);
  expect(screen.getByText("部分完成")).toBeInTheDocument();
});

it("keeps successful scenarios visible when another scenario fails", async () => {
  render(<AnalysisProgress initial={partiallyCompletedFixture} />);
  expect(screen.getByText("冷启动 · 已完成")).toBeInTheDocument();
  expect(screen.getByText("连续滑动 · 失败")).toBeInTheDocument();
  expect(screen.getByText("内存循环 · 已完成")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/analysis-progress.test.tsx tests/polling.test.ts
```

Expected: FAIL because task routes and polling orchestration are absent.

- [ ] **Step 3: Implement progress semantics**

Render parent state, each child state, valid/attempt counts, environment gate, lease/device summary safe for team users, timestamps, and stable failure code. Map states centrally in `progress.ts`; do not infer completion from percentage. Display a determinate percentage only when the contract supplies a count denominator.

Poll every two seconds while non-terminal. Pause while the document is hidden and immediately refresh when visible. Abort on unmount and route change. A retryable API failure preserves the last successful snapshot with a stale-data banner. A non-retryable error replaces the live region with its actionable state.

Cancellation asks for confirmation, calls `POST .../cancel` once with CSRF, then continues polling until the authoritative state becomes terminal. It does not optimistically label the task `canceled`.

- [ ] **Step 4: Run GREEN and commit**

```bash
npm run test:unit -- tests/analysis-progress.test.tsx tests/polling.test.ts
npm run test:ssr
git add app/teams app/components/scenario-progress-card.tsx app/lib/analysis/progress.ts tests/analysis-progress.test.tsx tests/polling.test.ts
git commit -m "feat: show authoritative analysis progress"
```

## Task 6: Adapt `AnalysisBundle v1` into the accepted report interface

**Files:**
- Create: `app/lib/report/analysis-bundle-adapter.ts`
- Create: `app/lib/report/formatters.ts`
- Create: `app/teams/[teamId]/analyses/[analysisId]/report/page.tsx`
- Create: `app/teams/[teamId]/analyses/[analysisId]/report/report-view.tsx`
- Modify: `app/components/dashboard.tsx`
- Modify: `app/components/problem-detail.tsx`
- Modify: `app/lib/performance-data.ts`
- Modify: `app/problems/page.tsx`
- Modify: `app/problems/[id]/page.tsx`
- Create: `tests/report-adapter.test.ts`
- Create: `tests/report-view.test.tsx`

- [ ] **Step 1: Write failing provenance and finding tests**

```ts
it("preserves status confidence evidence and versions", () => {
  const view = adaptAnalysisBundle(analysisBundleFixture);
  expect(view.findings[0]).toMatchObject({
    status: "confirmed",
    confidence: "high",
    evidenceCount: 2,
  });
  expect(view.provenance).toMatchObject({
    tracekitVersion: analysisBundleFixture.provenance.tracekit_version,
    sqlBundleVersion: analysisBundleFixture.provenance.sql_bundle_version,
    rulesVersion: analysisBundleFixture.provenance.rules_version,
  });
});

it("never converts insufficient data into an optimization conclusion", () => {
  const view = adaptAnalysisBundle(insufficientDataBundle);
  expect(view.findings[0].status).toBe("insufficient_data");
  expect(view.findings[0].recommendation).toBeUndefined();
});

it("keeps completed scenario reports when one sibling failed", () => {
  const view = adaptAnalysisReport(partiallyCompletedReportFixture);
  expect(view.scenarios.map((item) => item.resultState)).toEqual([
    "completed", "failed", "completed",
  ]);
  expect(view.scenarios[0].bundle).toBeDefined();
  expect(view.scenarios[2].bundle).toBeDefined();
});
```

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/report-adapter.test.ts tests/report-view.test.tsx
```

Expected: FAIL because the current dashboard imports static `performance-data`.

- [ ] **Step 3: Implement a lossless adapter**

`adaptAnalysisReport` preserves the parent state, device grouping, and ordered scenario entries, then delegates each non-null bundle to `adaptAnalysisBundle`. A failed scenario never removes or relabels a successful sibling, and metrics/findings from different device groups are never combined into one causal conclusion.

Map the four finding statuses exactly: `confirmed`, `suspected`, `insufficient_data`, and `invalid_capture`. Keep scenario, sample identity, units, metric definition, threshold, interval, query ID, source artifact, confidence ceiling, exclusions, recommendation, retest method, tool version, SQL bundle, rule version, and input hashes. Never synthesize a root cause or recommendation from a numeric threshold in the browser.

Format values only at the final rendering boundary. Preserve the raw number and unit for accessible labels and downloads. Use scenario-specific wording so frame overrun, slow-frame ratio, refresh-normalized missed-vsync severity, launch timing, and memory deltas cannot be confused.

Remove the production export of mock reports from `app/lib/performance-data.ts`. Test fixtures may remain under `tests/fixtures`; the application must show explicit empty, loading, permission, unavailable, invalid-capture, and missing-report states.

- [ ] **Step 4: Add artifact downloads**

The download button first calls the membership-bound API route, then navigates to the returned short-lived URL. Never render or cache a bucket key, bucket name, or presigned URL in server HTML, local storage, logs, or analytics.

- [ ] **Step 5: Run GREEN and commit**

```bash
npm run test:unit -- tests/report-adapter.test.ts tests/report-view.test.tsx tests/problem-detail-contract.test.ts
npm run test:ssr
git add app/lib/report app/teams app/components/dashboard.tsx app/components/problem-detail.tsx app/lib/performance-data.ts app/problems tests/report-adapter.test.ts tests/report-view.test.tsx
git commit -m "feat: render evidence-backed analysis reports"
```

## Task 7: Connect team navigation and administrator operations

**Files:**
- Create: `app/teams/page.tsx`
- Create: `app/teams/[teamId]/page.tsx`
- Create: `app/admin/page.tsx`
- Create: `app/admin/agents/page.tsx`
- Create: `app/admin/devices/page.tsx`
- Create: `app/components/team-switcher.tsx`
- Create: `app/components/admin-health-table.tsx`
- Create: `tests/team-routing.test.tsx`
- Create: `tests/admin-rbac.test.tsx`
- Modify: `app/components/app-shell.tsx`
- Modify: `app/page.tsx`
- Modify: `app/tests/page.tsx`
- Modify: `app/scenarios/page.tsx`
- Modify: `app/comparisons/page.tsx`

- [ ] **Step 1: Write failing routing and role tests**

```tsx
it("uses only a team returned by me", async () => {
  render(<TeamSwitcher teams={[teamA]} activeTeamId="team-a" />);
  expect(screen.getByRole("option", { name: teamA.name })).toHaveValue("team-a");
  expect(screen.queryByText("team-b")).not.toBeInTheDocument();
});

it("does not render admin navigation for a team member", () => {
  render(<AppShell session={teamMemberSession} />);
  expect(screen.queryByRole("link", { name: "平台管理" })).not.toBeInTheDocument();
});
```

The API test fixture must also return `403 role_forbidden` if a member manually navigates to an admin URL; UI hiding is not the authorization boundary.

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/team-routing.test.tsx tests/admin-rbac.test.tsx
```

Expected: FAIL because team-aware and administrator routes are absent.

- [ ] **Step 3: Implement team-bound navigation**

Every team resource link contains the active `teamId`; every API method receives that ID separately and validates it against `/me`. Switching teams clears task/report caches before navigation. Unknown or removed team IDs render a permission state and never retry another tenant automatically.

Administrator pages show provision state, Agent health, token version, device serial fingerprint, capabilities, active lease, thermal/battery/storage status, and quarantine state. Registration-code and token-rotation results are shown exactly once and never persisted by the browser. Destructive or identity-changing actions require confirmation and server CSRF validation.

Remove every demo count/result from the legacy `/tests`, `/scenarios`, and `/comparisons` routes. `/tests` becomes a session-aware entry to the active team; `/scenarios` shows only recipe data returned by an implemented API or an explicit empty state; `/comparisons` states that comparison is outside the approved first-phase API and exposes no fabricated chart. Legacy `/problems/...` routes redirect through the active team to the team-bound report route and never fetch an analysis by ID without a team authorization check.

- [ ] **Step 4: Run GREEN and commit**

```bash
npm run test:unit -- tests/team-routing.test.tsx tests/admin-rbac.test.tsx
npm run lint
git add app/teams app/admin app/components/team-switcher.tsx app/components/admin-health-table.tsx app/components/app-shell.tsx app/page.tsx app/tests/page.tsx app/scenarios/page.tsx app/comparisons/page.tsx tests/team-routing.test.tsx tests/admin-rbac.test.tsx
git commit -m "feat: add team and device administration views"
```

## Task 8: Prove browser integration, outage behavior, and absence of demo data

**Files:**
- Create: `tests/e2e/web-analysis.spec.ts`
- Create: `tests/e2e/web-auth.spec.ts`
- Create: `tests/e2e/web-outage.spec.ts`
- Create: `tests/no-production-mocks.test.ts`
- Modify: `tests/rendered-html.test.mjs`
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `.github/workflows/platform-ci.yml`

- [ ] **Step 1: Add the failing no-mock guard**

Scan `app/` and `worker/` in a production build test. Fail when runtime modules contain imports from `tests/fixtures`, a hard-coded analysis payload, fake Pixel device labels, or the old static report export. Allow deterministic examples only in test files.

- [ ] **Step 2: Add browser contract tests**

Against a disposable API/PostgreSQL/Redis/S3 stack:

- log in through the same-origin proxy;
- select an authorized team;
- submit a small contract fixture as an APK upload;
- observe upload and queued states;
- inject a completed contract report and verify metrics, findings, evidence, provenance, and artifact action;
- return one failed and two successful children and verify partial completion;
- return `503 tenant_store_unavailable` and verify an explicit service banner with no demo fallback;
- refresh a running route and prove the state is recovered from the API;
- attempt a cross-team URL and verify no protected content renders.

- [ ] **Step 3: Run RED**

```bash
npm run test:unit -- tests/no-production-mocks.test.ts
npm run test:e2e -- tests/e2e/web-auth.spec.ts tests/e2e/web-analysis.spec.ts tests/e2e/web-outage.spec.ts
```

Expected: FAIL until the Playwright command, disposable stack fixture, and all real routes are connected.

- [ ] **Step 4: Add Playwright and CI**

Add `test:e2e` and a pinned Playwright dependency. Start the built Vinext Worker and disposable API stack on loopback-only ports. CI injects synthetic proxy, session, database, Redis, and S3 secrets; it never reuses production values. Upload only screenshots/traces from failed tests and redact cookie and authorization headers.

- [ ] **Step 5: Run GREEN and commit**

```bash
npm ci
npm run lint
npm test
npm run test:e2e
git add tests/e2e tests/no-production-mocks.test.ts tests/rendered-html.test.mjs package.json package-lock.json .github/workflows/platform-ci.yml
git commit -m "test: cover PerfPilot web integration"
```

## Task 9: Deploy the private backend and run the RKGallery real-device acceptance gate

**Files:**
- Create: `tests/e2e/rkgallery-acceptance.spec.ts`
- Create: `tests/e2e/rkgallery-acceptance.schema.json`
- Create: `tests/e2e/run-rkgallery-acceptance.sh`
- Create: `docs/acceptance/rkgallery/README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Deploy the exact pushed backend source to the private acceptance host**

Require `PERFPILOT_ACCEPTANCE_HOST`, `PERFPILOT_API_ORIGIN`, and SSH access to the dedicated deployment account. On the host, use a dedicated application directory, fetch `origin/main`, verify its SHA equals the local and GitHub SHA, and check out that SHA detached. Create the documented root-owned secret files outside Git for the proxy signature, sessions, snapshot signing, encrypted secret store, PostgreSQL, Redis, MinIO, and the development administrator bootstrap value. Never print or pass those values on a command line.

Render `infra/compose.acceptance.yaml`, run migrations before application readiness, build images from the detached exact source, record their digests, and start Caddy, data services, API/control workers, validator, and analysis Worker. Run `infra/scripts/verify_private_host.py`; it must prove TLS, signature enforcement, private internal ports, migrations, versioned storage, secret modes, clock, disk, and readiness. The temporary development administrator remains behind the signed proxy and private Sites gate and must be rotated before any public release.

- [ ] **Step 2: Validate prerequisites without changing the device**

Require explicit environment paths/IDs for the user-provided RKGallery APK, registered Agent, online device, team, fixture version, API origin, and test account secret. Query API/Agent health and `adb -s "$RKGALLERY_DEVICE_SERIAL" get-state`. Refuse to run if more than one device could match, the fixture hash differs from the checked-in fixture, the device is leased, or the API reports non-active tenant resources.

Do not commit the APK, credentials, registration code, token, cookies, Trace, screenshots, logs, reports, object URLs, or raw acceptance output.

- [ ] **Step 3: Write the acceptance assertions before the runner**

The test must fail unless one device-mode parent proves:

- the same device identity, APK hash, fixture hash, dataset fingerprint, `aot_speed` policy, and display mode across all children;
- `cold_start` has 5 valid process-cold samples in at most 10 attempts;
- `scroll` has 5 valid runs in at most 10 attempts and each measured segment is 30 seconds within the contract tolerance;
- `memory_cycle` has one unchanged PID and 10 completed enter/exit rounds;
- every valid startup and scroll sample passed the per-attempt thermal gate;
- every scenario has immutable artifact metadata and a scenario manifest;
- the report includes metrics, one of the four finding states, evidence, recommendations only where permitted, and complete provenance;
- raw Trace/startup and scroll artifacts are downloadable while retained;
- the Agent cleanup checkpoint proves APK, app data, temporary Trace, screenshots, and logs were removed.

Add a second controlled fixture run in which one scenario deterministically fails and assert that the other two results remain visible and the parent is `partially_completed`.

- [ ] **Step 4: Run the test and preserve only a redacted receipt**

```bash
bash tests/e2e/run-rkgallery-acceptance.sh
```

Expected on the first run: FAIL at the first unmet environment or product invariant. Fix the product or environment; never weaken sample counts, duration, thermal gates, process-cold semantics, PID stability, cleanup, or provenance requirements.

After a successful run, generate a small JSON receipt containing only schema version, analysis IDs, terminal states, counts, fixture/tool/rule versions, input hashes, device model fingerprint, timestamps, and API request IDs. Validate it against `rkgallery-acceptance.schema.json`. Keep the full evidence in protected platform storage.

- [ ] **Step 5: Run the complete Phase 1 gate**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv sync --locked --all-packages --all-extras --dev
uv run ruff check services tracekit agents
uv run pytest -q
npm ci
npm run lint
npm test
npm run test:e2e
bash tests/e2e/run-rkgallery-acceptance.sh
git diff --check
```

Expected: every command exits `0`; the acceptance receipt validates; `git status --short` contains only the intended receipt/documentation changes and no raw artifacts.

- [ ] **Step 6: Commit the acceptance harness and redacted receipt**

```bash
git add tests/e2e/rkgallery-acceptance.spec.ts tests/e2e/rkgallery-acceptance.schema.json tests/e2e/run-rkgallery-acceptance.sh docs/acceptance/rkgallery .gitignore
git commit -m "test: prove RKGallery real-device workflow"
```

## Task 10: Push the exact source and deploy the saved private Sites version

**Files:**
- Verify unchanged: `.openai/hosting.json`
- Modify only if build configuration requires it: `wrangler.jsonc`

- [ ] **Step 1: Verify the hosting boundary and source**

```bash
test "$(node -p "require('./.openai/hosting.json').project_id")" = "appgprj_6a61f0a6e350819199cf6fe9d64c21a4"
test "$(node -p "require('./.openai/hosting.json').d1")" = "null"
test "$(node -p "require('./.openai/hosting.json').r2")" = "null"
npm ci
npm run lint
npm test
git diff --check
git status --short
```

Expected: the project ID is unchanged, no Sites database/storage binding was added, all checks pass, and status is empty after the prior commit.

- [ ] **Step 2: Fast-forward push and verify GitHub**

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
git status --short
```

Expected: the push is fast-forward, the SHAs match, and the worktree is clean.

- [ ] **Step 3: Save exactly the pushed source as a Sites version**

Use the Sites connector for the existing project ID. Upload the exact Git tree identified by `local_sha`; configure production build command `npm run build`; provide `PERFPILOT_API_ORIGIN` as a runtime value and `PERFPILOT_PROXY_SECRET` as a runtime secret. Save a version with `commit_sha=local_sha`. Do not create a new site, derive another project ID, add D1/R2, or save a version from unpushed files.

- [ ] **Step 4: Deploy only the saved version**

Deploy that saved version with private access, then poll deployment status until it is terminal. Record the version ID, deployment ID, production URL, Git SHA, and terminal status in the implementation handoff. Every Sites deployment URL is production; do not create a probe deployment.

- [ ] **Step 5: Run production smoke checks**

Open the deployed login page, confirm the outer private-hosting gate and product login both work, confirm `/api/v1/health` traverses the signed proxy, submit no customer data, and verify an unauthorized team URL reveals no tenant content. Recheck:

```bash
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
git status --short
```

Expected: deployed version SHA equals both local and remote `main`, smoke checks pass, and status is empty.
