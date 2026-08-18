# Targeted Trace HTML Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make uploaded Trace analyses package-targeted and single-scenario, preserve SmartPerfetto's native HTML byte-for-byte, and render every evidence-grounded conclusion in a four-part, leadership-friendly report.

**Architecture:** Keep SmartPerfetto's structured JSON as private machine evidence, but fetch the native HTML from the validated `/api/reports/{reportId}/export` route and persist it as the only user-facing original. Add account ownership and single-current-analysis cleanup to the local runtime, simplify only the Trace-upload request/UI, and derive the complete conclusion list from normalized SmartPerfetto findings plus validated AI/source enrichment.

**Tech Stack:** FastAPI, Pydantic, asyncio/httpx, filesystem-backed local repositories, JSON Schema 2020-12, React/TypeScript, Vitest, Pytest.

---

**Verified external fact:** The pinned SmartPerfetto source implements native HTML generation in `src/routes/agentRoutes.ts` and serves it from `GET /api/reports/:reportId` plus `GET /api/reports/:reportId/export` with `Content-Type: text/html; charset=utf-8`. PerfPilot must preserve the exact HTTP response body returned by that native route.

## File map

- `services/api/src/perfpilot_api/engines/smartperfetto_transport.py`: bounded same-origin HTML download.
- `services/api/src/perfpilot_api/engines/contracts.py`: carry native HTML bytes beside structured engine evidence.
- `services/api/src/perfpilot_api/local_app.py`: targeted Trace request, account ownership, SmartPerfetto query, report publication and account-scoped replacement.
- `services/api/src/perfpilot_api/local_analysis_store.py`: descriptor-anchored deletion of one analysis directory.
- `services/api/src/perfpilot_api/reports/smartperfetto_original.py`: immutable `text/html` binding; remove JSON/collection behavior.
- `services/api/src/perfpilot_api/reports/conclusions.py`: pure assembly of all four-part conclusions from normalized findings and synthesis/source results.
- `services/api/src/perfpilot_api/ai/prompts/perfpilot-report-v3.txt`: require evidence-grounded Chinese causal explanation and recommendations.
- `contracts/v1/analyses/create-request.schema.json`: package-targeted single Trace request and conditional custom test fields.
- `contracts/v1/analyses/analysis-response.schema.json`: expose target/test metadata without auxiliary upload slots.
- `contracts/v1/reports/analysis-report.schema.json`: publish `conclusions` and HTML original metadata.
- `app/lib/perfpilot-api.ts`: strict request/response types and native HTML URL construction.
- `app/components/trace-upload-form.tsx`: simplified right-hand Trace form.
- `app/components/analysis-report.tsx`: remove appendix tab and render three report layers.
- `app/components/concise-report-summary.tsx`: top three expanded, remaining conclusions collapsed.
- `app/components/smartperfetto-original-report.tsx`: sandboxed native HTML iframe and HTML download.
- `app/components/technical-appendix.tsx`: delete.
- `tests/` and `services/api/tests/`: contract, security, integration and UI coverage.

### Task 1: Fetch SmartPerfetto native HTML without changing bytes

**Files:**
- Modify: `services/api/src/perfpilot_api/engines/smartperfetto_transport.py`
- Modify: `services/api/src/perfpilot_api/engines/contracts.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/unit/test_smartperfetto_transport.py`
- Test: `services/api/tests/unit/test_smartperfetto_local_gateway.py`

- [ ] **Step 1: Write the failing transport tests**

Add tests that return non-normalized HTML bytes and assert exact equality, `text/html`, no redirects, same configured origin, an explicit 16 MiB maximum, and stable rejection of JSON/oversize responses:

```python
HTML = b'<!doctype html>\n<html><body data-x="1">\xe4\xb8\xad</body></html>\n'

@pytest.mark.asyncio
async def test_request_html_preserves_smartperfetto_bytes() -> None:
    response = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=HTML)
    transport = _transport(response)
    assert await transport.request_html("/api/reports/report-1/export", workspace_id="default-workspace") == HTML

@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
async def test_request_html_rejects_non_html(content_type: str) -> None:
    transport = _transport(httpx.Response(200, headers={"content-type": content_type}, content=b"{}"))
    with pytest.raises(EngineAdapterError, match="engine_contract_invalid"):
        await transport.request_html("/api/reports/report-1/export", workspace_id="default-workspace")
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_smartperfetto_transport.py \
  services/api/tests/unit/test_smartperfetto_local_gateway.py \
  -k 'html or original_report'
```

Expected: fail because `SmartPerfettoTransport.request_html` and the HTML result field do not exist.

- [ ] **Step 3: Add a bounded HTML transport and result carrier**

Implement a method that reuses `_headers`, `_validate_path`, `_check_status`, `follow_redirects=False`, and never decodes the body:

```python
async def request_html(self, path: str, *, workspace_id: str) -> bytes:
    safe_path = _validate_path(path)
    headers = await self._headers(accept="text/html", workspace_id=workspace_id)
    response = await self._client.send(
        self._client.build_request("GET", f"{self._base_url}{safe_path}", headers=headers),
        stream=True,
        follow_redirects=False,
    )
    try:
        self._check_status(response)
        if not 200 <= response.status_code <= 299:
            raise _error("engine_contract_invalid", retryable=False)
        if response.headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "text/html":
            raise _error("engine_contract_invalid", retryable=False)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 16 * 1024 * 1024:
                raise _error("engine_contract_invalid", retryable=False)
            chunks.append(chunk)
        payload = b"".join(chunks)
        if not payload:
            raise _error("engine_contract_invalid", retryable=False)
        return payload
    finally:
        await response.aclose()
```

Add `original_report_html_bytes: bytes | None = None` to the engine result carrier. In `SmartPerfettoLocalGateway.fetch_result`, construct the reviewed path from `parsed.report_id`, never from an arbitrary URL:

```python
html = await self._transport.request_html(
    f"/api/reports/{parsed.report_id}/export",
    workspace_id=self._workspace_id,
)
```

Keep sanitized structured JSON in `payload`; stop assigning JSON bytes to the user-facing original field.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/engines/smartperfetto_transport.py \
  services/api/src/perfpilot_api/engines/contracts.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_smartperfetto_transport.py \
  services/api/tests/unit/test_smartperfetto_local_gateway.py
git commit -m "feat: fetch native SmartPerfetto HTML"
```

### Task 2: Replace JSON originals with one immutable HTML artifact

**Files:**
- Modify: `services/api/src/perfpilot_api/reports/smartperfetto_original.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/src/perfpilot_api/local_analysis_store.py`
- Modify: `contracts/v1/reports/analysis-report.schema.json`
- Test: `services/api/tests/unit/test_smartperfetto_original.py`
- Test: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Write failing artifact and endpoint tests**

Cover exact non-normalized HTML bytes, MIME, checksum, owner/team/analysis binding, symlink/FIFO/mode rejection, inline read, attachment download, no scenario collection and no JSON document:

```python
def test_native_html_is_byte_faithful(tmp_path: Path) -> None:
    payload = b'<!doctype html>\n<html><body class="x">\xe4\xb8\xad</body></html>\n'
    binding = persist_smartperfetto_original(
        root=tmp_path, team_id=TEAM, analysis_id=ANALYSIS, payload=payload
    )
    assert binding.mime == "text/html"
    assert read_smartperfetto_original(
        root=tmp_path, binding=binding, team_id=TEAM, analysis_id=ANALYSIS
    ) == payload
```

Integration assertions:

```python
inline = client.get(f"/v1/teams/{TEAM}/analyses/{analysis_id}/smartperfetto-original")
assert inline.headers["content-type"].startswith("text/html")
assert inline.content == HTML
download = client.get(inline.request.url.copy_add_param("download", "true"))
assert download.content == HTML
assert download.headers["content-disposition"].endswith('.html"')
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_smartperfetto_original.py \
  services/api/tests/integration/test_local_app.py \
  -k 'native_html or smartperfetto_original'
```

Expected: current service requires JSON and the endpoint returns JSON documents.

- [ ] **Step 3: Collapse the artifact service to HTML-only**

Use:

```python
MAX_SMARTPERFETTO_ORIGINAL_BYTES = 16 * 1024 * 1024
_MIME = "text/html"
```

Delete scenario binding/collection types and JSON parsing. Validate bytes are non-empty and contain an HTML document marker within the first bounded prefix, but return the exact original payload. Keep deterministic artifact identity, `0600`, `O_NOFOLLOW`, regular-file/link/size/hash validation and atomic fsync publication.

Make the local endpoint return `Response(payload, media_type="text/html")`; add attachment headers only when `download=true`. Remove scenario query handling.

- [ ] **Step 4: Persist HTML during report preparation**

Replace every use of `result.original_report_bytes` with `result.original_report_html_bytes`. A missing native HTML field must produce stable partial state `smartperfetto_html_unavailable`; it must never fall back to canonical JSON.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/perfpilot_api/reports/smartperfetto_original.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/src/perfpilot_api/local_analysis_store.py \
  contracts/v1/reports/analysis-report.schema.json \
  services/api/tests/unit/test_smartperfetto_original.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: persist native SmartPerfetto HTML"
```

### Task 3: Make Trace upload single-scenario and package-targeted

**Files:**
- Modify: `contracts/v1/analyses/create-request.schema.json`
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/trace-upload-form.tsx`
- Test: `services/api/tests/unit/test_analysis_contracts.py`
- Test: `services/api/tests/integration/test_local_app.py`
- Test: `tests/perfpilot-api.test.ts`
- Test: `tests/trace-upload-form.test.tsx`

- [ ] **Step 1: Write failing request/UI tests**

Add strict cases for `cold_start|hot_start|scroll|other`, mandatory package name, conditional custom fields, trace-only inputs, and no change to the device form:

```ts
expect(screen.getByLabelText("应用包名")).toBeRequired();
expect(screen.queryByLabelText("APK 文件（可选）")).not.toBeInTheDocument();
expect(screen.queryByLabelText("内存证据（可选）")).not.toBeInTheDocument();
await user.selectOptions(screen.getByLabelText("测试类型"), "other");
expect(screen.getByLabelText("测试名称")).toBeRequired();
expect(screen.getByLabelText("测试说明")).toBeRequired();
```

Backend model cases must reject an empty package, `../app`, shell text, omitted custom description, custom fields on non-custom types, and any non-Trace input.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/integration/test_local_app.py \
  -k 'target_package or custom_trace or trace_inputs'
npx vitest run tests/perfpilot-api.test.ts tests/trace-upload-form.test.tsx
```

Expected: package/custom fields are absent and auxiliary files are still accepted/rendered.

- [ ] **Step 3: Define the strict request**

Use the same shape in JSON Schema, Pydantic and TypeScript:

```ts
type UploadedTraceTestType = "cold_start" | "hot_start" | "scroll" | "other";

interface SubmitTraceInput {
  readonly testType: UploadedTraceTestType;
  readonly packageName: string;
  readonly customTestName?: string;
  readonly customTestDescription?: string;
  readonly question?: string;
  readonly trace: File;
  readonly sourceBinding?: SourceBinding;
}
```

The Pydantic request must require exactly one descriptor whose kind is `trace`. Use an anchored Android application ID validator and conditional model validation:

```python
_PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")

if self.test_type == "other":
    if not self.custom_test_name or not self.custom_test_description:
        raise ValueError("custom test details are required")
elif self.custom_test_name is not None or self.custom_test_description is not None:
    raise ValueError("custom test details are forbidden")
```

- [ ] **Step 4: Persist and use the target**

Add `package_name`, `test_type`, `custom_test_name`, and `custom_test_description` to `_LocalAnalysis` and its closed persisted state. Update `SmartPerfettoLocalGateway.submit` to accept those fields and build a target-specific query:

```python
query = (
    f"Only analyze Android package {package_name}. "
    f"The captured scenario is {scenario_instruction}. "
    "Ignore unrelated processes and state when target evidence is insufficient."
)
```

After normalization, compare every parsed target package with the requested package. A mismatch must create an evidence limitation and must not produce a confirmed finding for another package.

- [ ] **Step 5: Implement the simplified form**

Render the four test options and conditional custom fields. Remove the entire `trace-optional-files` block and submit only the selected Trace. Keep source workspace and optional question.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 commands. Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add contracts/v1/analyses/create-request.schema.json \
  contracts/v1/analyses/analysis-response.schema.json \
  services/api/src/perfpilot_api/local_app.py \
  app/lib/perfpilot-api.ts app/components/trace-upload-form.tsx \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/integration/test_local_app.py \
  tests/perfpilot-api.test.ts tests/trace-upload-form.test.tsx
git commit -m "feat: target uploaded traces by package"
```

### Task 4: Keep exactly one successful analysis per account

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/src/perfpilot_api/local_analysis_store.py`
- Test: `services/api/tests/unit/test_local_analysis_store.py`
- Test: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Write failing isolation and failure-atomicity tests**

Create accounts A and B. Give A an existing completed analysis, attempt an invalid/new failed submission and assert the old report survives. Then successfully submit A's new analysis and assert all A-old paths/API records are gone while B's analysis/report/HTML/source data remain byte-identical.

Also cover symlink/FIFO/unknown-entry refusal and a post-rename durability fault.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_local_analysis_store.py \
  services/api/tests/integration/test_local_app.py \
  -k 'account_replacement or delete_analysis'
```

Expected: analysis documents have no owner and the store has no safe single-analysis deletion operation.

- [ ] **Step 3: Bind owner authority**

Add `owner_user_id: UUID` to `_LocalAnalysis`, persisted state and restore validation. In the route, retain the principal returned by `authorize_team` and pass `principal.user_id` into `runtime.create`; never accept owner ID from the request body.

- [ ] **Step 4: Add descriptor-anchored removal**

Implement `LocalAnalysisStore.remove_analysis(team_id, analysis_id)` by opening the trusted team/analyses/analysis hierarchy with `O_DIRECTORY|O_NOFOLLOW`, verifying exact UUID directory names and modes, atomically renaming the verified analysis entry to a private tombstone under the same trusted parent, fsyncing the parent, then recursively deleting only through directory descriptors. Reject symlinks, non-regular unknown objects and path substitution; do not call pathname-based `shutil.rmtree`.

- [ ] **Step 5: Commit replacement only after new submission succeeds**

After the new Trace upload has finalized and its background task has been durably published, collect every other in-memory analysis owned by the same user, remove its runtime/upload grants, remove its trusted store directory and leave other owners unchanged. If new submission fails, do not invoke removal.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add services/api/src/perfpilot_api/local_app.py \
  services/api/src/perfpilot_api/local_analysis_store.py \
  services/api/tests/unit/test_local_analysis_store.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: replace prior account analyses"
```

### Task 5: Assemble every evidence-grounded four-part conclusion

**Files:**
- Create: `services/api/src/perfpilot_api/reports/conclusions.py`
- Modify: `services/api/src/perfpilot_api/reports/writer.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/src/perfpilot_api/ai/prompts/perfpilot-report-v3.txt`
- Modify: `contracts/v1/reports/analysis-report.schema.json`
- Modify: `app/lib/perfpilot-api.ts`
- Test: `services/api/tests/unit/test_report_writer.py`
- Test: `services/api/tests/unit/test_local_report.py`
- Test: `services/api/tests/contract/test_ai_report_contracts.py`

- [ ] **Step 1: Write failing conclusion assembly tests**

Build five normalized SmartPerfetto findings. Put three IDs in AI `top_findings`, recommendations against four findings, and one strong source fix. Assert five conclusions survive in deterministic order and each has exactly:

```json
{
  "finding_id": "...",
  "evidence_ids": ["..."],
  "priority": "primary",
  "problem": "问题点",
  "reason": "为什么会有这个问题",
  "source_root_cause": "结合源码判断的根因是什么",
  "recommendation": "修改建议",
  "source_fix_id": "... or null"
}
```

Assert the first three follow AI priority, the remaining findings follow severity/confidence/stable ID, no finding/evidence/source ID can be invented, and weak/no-source output contains no path/line/Diff.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_report_writer.py \
  services/api/tests/unit/test_local_report.py \
  services/api/tests/contract/test_ai_report_contracts.py \
  -k 'four_part or all_conclusions'
```

Expected: reports expose separate capped arrays and no `conclusions` document.

- [ ] **Step 3: Add the pure assembler**

Define `compose_conclusions(core_document, synthesis_output, source_code_document)`. For each confirmed/suspected SmartPerfetto finding:

- `problem`: normalized `title`.
- `reason`: AI causal narrative when present, otherwise normalized SmartPerfetto `summary`.
- `source_root_cause`: matching strong source-fix `diagnosis`; otherwise the stable Chinese sentence `SmartPerfetto 已确认现象，但本次未能从可信源码上下文确认具体代码根因。`
- `recommendation`: first evidence-closed AI recommendation referencing the finding; otherwise SmartPerfetto's normalized recommendation; if neither exists, state that more target evidence is required instead of inventing an action.

Every conclusion retains finding/evidence IDs. Mark the AI-selected first three `primary`; mark all others `additional`.

- [ ] **Step 4: Extend the report contract and writer**

Add a required `conclusions` array with `minItems: 0`, `maxItems: 20`, unique finding IDs and the exact fields above. Enforce at most three `primary`, all primary entries before additional entries, and source-fix closure. Keep legacy synthesis fields internally for provenance/source generation, but make `conclusions` the only conclusion-view payload.

Update the prompt to require clear Simplified Chinese causal explanation grounded only in referenced SmartPerfetto evidence; do not raise the source-fix cap or invent source locations.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/perfpilot_api/reports/conclusions.py \
  services/api/src/perfpilot_api/reports/writer.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/src/perfpilot_api/ai/prompts/perfpilot-report-v3.txt \
  contracts/v1/reports/analysis-report.schema.json \
  app/lib/perfpilot-api.ts \
  services/api/tests/unit/test_report_writer.py \
  services/api/tests/unit/test_local_report.py \
  services/api/tests/contract/test_ai_report_contracts.py
git commit -m "feat: publish complete evidence-backed conclusions"
```

### Task 6: Simplify the report UI and embed native HTML

**Files:**
- Modify: `app/components/analysis-report.tsx`
- Modify: `app/components/concise-report-summary.tsx`
- Modify: `app/components/smartperfetto-original-report.tsx`
- Modify: `app/components/full-analysis-report.tsx`
- Delete: `app/components/technical-appendix.tsx`
- Modify: `app/globals.css`
- Modify: `app/lib/perfpilot-api.ts`
- Test: `tests/analysis-report.test.tsx`
- Test: `tests/full-analysis-report.test.tsx`
- Test: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: Write failing UI tests**

Assert exactly three tabs, no appendix text in screen/print, native HTML iframe URL, `.html` download, three expanded conclusion cards and an expandable remainder:

```tsx
expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
  "结论", "源码优化", "SmartPerfetto 原始报告",
]);
expect(screen.queryByText("技术附录")).not.toBeInTheDocument();
expect(screen.getAllByTestId("primary-conclusion")).toHaveLength(3);
await user.click(screen.getByText("展开其余结论（2 条）"));
expect(screen.getAllByTestId("additional-conclusion")).toHaveLength(2);
```

Each card must have headings `问题点`, `为什么会有这个问题`, `结合源码判断的根因是什么`, and `修改建议`.

- [ ] **Step 2: Run tests and verify RED**

```bash
npx vitest run tests/analysis-report.test.tsx \
  tests/full-analysis-report.test.tsx tests/perfpilot-api.test.ts
```

Expected: appendix exists, conclusions are capped, and the original component parses JSON.

- [ ] **Step 3: Render the conclusion disclosure**

Replace the current split findings/recommendations sections with reusable conclusion cards. Render `conclusions.filter(priority === "primary").slice(0, 3)` directly and all additional entries inside one `<details>` whose summary includes the exact count. Never use `dangerouslySetInnerHTML` for PerfPilot narrative.

- [ ] **Step 4: Remove appendix and embed native HTML**

Delete the import/component/tab/panel and print CSS. Replace JSON fetching with a sandboxed same-origin iframe:

```tsx
<iframe
  title="SmartPerfetto 原始报告"
  src={client.smartPerfettoOriginalUrl(teamId, analysisId)}
  sandbox="allow-scripts"
  referrerPolicy="no-referrer"
/>
```

The download link uses the same endpoint with `download=true`. Do not use `srcDoc`, do not parse the HTML and do not expose upstream `reportUrl`.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/components/analysis-report.tsx \
  app/components/concise-report-summary.tsx \
  app/components/smartperfetto-original-report.tsx \
  app/components/full-analysis-report.tsx app/globals.css \
  app/lib/perfpilot-api.ts tests/analysis-report.test.tsx \
  tests/full-analysis-report.test.tsx tests/perfpilot-api.test.ts
git rm app/components/technical-appendix.tsx
git commit -m "feat: simplify source-aware report presentation"
```

### Task 7: End-to-end acceptance, reset and deployment readiness

**Files:**
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`
- Modify: `services/api/tests/acceptance/test_remote_agent_capture.py`
- Modify: `tests/ubuntu-user-deployment.test.ts`
- Modify: `README.md`

- [ ] **Step 1: Add full acceptance coverage**

Create one uploaded-Trace acceptance that uses package `com.rivotek.mediacenter`, one test type, native HTML with intentionally unusual bytes, five SmartPerfetto findings, strong source context and two users. Assert:

- SmartPerfetto receives the exact package/test target.
- HTML download equals upstream bytes and JSON is never exposed as the original.
- all five conclusions survive; three primary and two additional.
- every conclusion references SmartPerfetto evidence and has four Chinese sections.
- no appendix or auxiliary upload slot is public.
- successful second analysis deletes only the same user's first analysis.
- a failed second submission retains the previous report.
- the second user can still read only their own analysis.

- [ ] **Step 2: Run the focused acceptance suite**

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest -q \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  services/api/tests/acceptance/test_remote_agent_capture.py
```

Expected: pass.

- [ ] **Step 3: Run backend regression gates**

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest -q \
  services/api/tests/unit services/api/tests/contract services/api/tests/integration
.venv/bin/ruff check services/api/src/perfpilot_api services/api/tests
```

Expected: all runnable tests pass; environment-gated PostgreSQL tests may skip with their existing reason.

- [ ] **Step 4: Run frontend gates**

```bash
npm test -- --run
npm run lint
npm run build
```

Expected: all tests, lint and production build pass.

- [ ] **Step 5: Verify reset behavior**

Run the deployment test and assert restart removes all analysis directories without creating a backup while leaving control users and Agent registrations intact:

```bash
npx vitest run tests/ubuntu-user-deployment.test.ts
bash -n scripts/reset-ubuntu-analysis-data.sh scripts/restart-local.sh
```

- [ ] **Step 6: Update operator documentation**

Document the simplified Trace form, mandatory package, custom test fields, native HTML report, per-account replacement and the existing reset-on-restart behavior in `README.md`.

- [ ] **Step 7: Final diff and commit**

```bash
git diff --check
git status --short
git add services/api/tests/acceptance/test_source_aware_report_flow.py \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  tests/ubuntu-user-deployment.test.ts README.md
git commit -m "test: verify targeted HTML report workflow"
```

### Task 8: Push, clear old analyses and restart the Ubuntu stack

**Files:**
- No source files; operational execution only after all verification passes.

- [ ] **Step 1: Verify the exact branch and commits**

```bash
git status --short
git log --oneline origin/main..main
```

Expected: only the intentionally untracked `.superpowers/` remains and all feature commits are listed.

- [ ] **Step 2: Push main**

```bash
git push origin main
```

Expected: remote `main` advances to the verified local HEAD.

- [ ] **Step 3: Deploy and reset without backup**

Use the repository's reviewed Ubuntu user-service deployment path. Run the reset/restart target that deletes only analysis data, creates no backup, and retains users/teams/Agent registrations. Do not manually delete broad directories.

- [ ] **Step 4: Smoke test**

Verify login, Agent online state, one package-targeted Trace submission, final four-part conclusions, native HTML display/download, and absence of the technical appendix and auxiliary upload controls.

- [ ] **Step 5: Record deployed revision**

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Expected: local, remote and deployed revisions match.
