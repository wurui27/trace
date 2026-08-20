# Finding Workbench UI and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render `AnalysisReport 1.3` as a blue-white desktop Finding workbench with trace evidence navigation, source-safe recommendations, retest plans, legacy compatibility, and real uploaded-Trace acceptance.

**Architecture:** Keep `AnalysisReportView` as the version dispatcher. Put all 1.3 behavior behind a new `FindingWorkbench` component and split its six user-facing regions into focused files. Keep 1.2 components unchanged, reuse the existing SmartPerfetto original HTML loader, and use strict TypeScript parsing before rendering.

**Tech Stack:** TypeScript, React, Vitest, Testing Library, CSS, existing `PerfPilotClient`, Python pytest acceptance, local SmartPerfetto gateway fixtures, Ruff, ESLint, production build.

---

## File responsibility map

- Modify `app/lib/perfpilot-api.ts`: 1.3 closed types and parser dispatch only.
- Create `app/components/finding-workbench.tsx`: six-region navigation and overall composition.
- Create `app/components/finding-overview.tsx`: green completed state, capability matrix, waterfall, and top three.
- Create `app/components/finding-list.tsx`: all findings, stable filters, sorting, and selection.
- Create `app/components/finding-detail.tsx`: problem/cause/source root/recommendation chain and confidence.
- Create `app/components/evidence-metrics-panel.tsx`: evidence locator, metrics, and controlled deep links.
- Create `app/components/trace-evidence-locator.tsx`: safe evidence locator page content derived from the validated report.
- Create `app/analyses/[id]/trace/page.tsx`: internal evidence deep-link destination.
- Create `app/components/finding-source-panel.tsx`: strong-only source refs and fixes; path-free manual actions otherwise.
- Create `app/components/retest-plan-panel.tsx`: retest environment, criteria, and Finding status.
- Modify `app/components/analysis-report.tsx`: dispatch 1.3 only.
- Modify `app/components/full-analysis-report.tsx`: print all 1.3 regions and preload original HTML.
- Modify `app/globals.css`: blue-white desktop workbench and print rules.
- Modify `tests/perfpilot-api.test.ts`: strict 1.3 parser and privacy tests.
- Modify `tests/analysis-report.test.tsx`: dispatcher and workbench behavior.
- Modify `tests/full-analysis-report.test.tsx`: print behavior.
- Create `services/api/tests/acceptance/test_finding_workbench_trace_upload.py`: real uploaded Trace end-to-end acceptance.

### Task 1: Add strict TypeScript types and parser dispatch

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: Add failing 1.3 parser tests**

```typescript
it("accepts a closed AnalysisReport 1.3 workbench", async () => {
  const response = analysisReportV13();
  mockFetch.mockResolvedValue(jsonResponse(response));

  const report = await client.report(TEAM_ID, ANALYSIS_ID);

  expect(report.schema_version).toBe("1.3");
  if (report.schema_version !== "1.3") throw new Error("expected 1.3");
  expect(report.workbench.primary_finding_ids).toHaveLength(3);
  expect(report.capabilities.source).toBe("matched");
});

it.each(["private_path", "repo_url", "remote", "argv"]) (
  "rejects private top-level field %s in AnalysisReport 1.3",
  async (field) => {
    const response = { ...analysisReportV13(), [field]: "private-marker" };
    mockFetch.mockResolvedValue(jsonResponse(response));

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toThrow(
      "invalid_api_response",
    );
  },
);

it("rejects weak source locations in every 1.3 narrative surface", async () => {
  const response = analysisReportV13();
  response.capabilities.source = "mismatch";
  response.workbench.findings[0].source_ref_ids = [SOURCE_REF_ID];

  mockFetch.mockResolvedValue(jsonResponse(response));

  await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toThrow(
    "invalid_api_response",
  );
});
```

- [ ] **Step 2: Run and verify RED**

```bash
npx vitest run tests/perfpilot-api.test.ts -t 'AnalysisReport 1.3|private top-level|weak source locations'
```

Expected: FAIL because 1.3 is not part of `AnalysisReport` and `analysisReportResponse` rejects it.

- [ ] **Step 3: Add exact 1.3 interfaces**

Add focused exported interfaces:

```typescript
export interface FindingWorkbenchReport extends AnalysisReportBase {
  readonly schema_version: "1.3";
  readonly state: "completed";
  readonly capabilities: ReportCapabilities;
  readonly workbench: FindingWorkbenchDocument;
  readonly synthesis: CompletedFindingSynthesis;
  readonly source_code: SourceCodeReport;
  readonly smartperfetto_original: SmartPerfettoOriginalBinding;
}

export type AnalysisReport =
  | LegacyAnalysisReport
  | SourceAwareAnalysisReport
  | FindingWorkbenchReport;

export interface FindingWorkbenchFinding {
  readonly finding_id: string;
  readonly title: string;
  readonly problem: string;
  readonly impact: string;
  readonly mechanism: string;
  readonly root_cause: string;
  readonly priority: "p0" | "p1" | "p2" | "p3";
  readonly priority_score: number;
  readonly evidence_ids: readonly string[];
  readonly metric_ids: readonly string[];
  readonly source_ref_ids: readonly string[];
  readonly status: "confirmed" | "hypothesis" | "resolved" | "improved" | "unchanged" | "regressed" | "new";
  readonly confidence: FindingConfidence;
}
```

- [ ] **Step 4: Add closed parser helpers**

Use exact key arrays and version dispatch:

```typescript
function analysisReportResponse(value: unknown): AnalysisReport {
  if (object(value) && value.schema_version === "1.3") {
    if (!validFindingWorkbenchReport(value)) throw invalidApiResponse();
    return value;
  }
  if (object(value) && value.schema_version === "1.2") {
    return sourceAwareAnalysisReportResponse(value);
  }
  return legacyAnalysisReportResponse(value);
}
```

`validFindingWorkbenchReport` must validate exact keys, canonical UUIDs, unique IDs, score range 0–100, primary IDs as an ordered subset, locator `end_ns >= start_ns`, and source capability coherence.

- [ ] **Step 5: Run complete client tests and commit**

```bash
npx vitest run tests/perfpilot-api.test.ts
npx eslint app/lib/perfpilot-api.ts tests/perfpilot-api.test.ts
git add app/lib/perfpilot-api.ts tests/perfpilot-api.test.ts
git commit -m "feat: parse finding workbench reports"
```

Expected: PASS and no lint errors.

### Task 2: Add the 1.3 dispatcher and six-region shell

**Files:**
- Create: `app/components/finding-workbench.tsx`
- Modify: `app/components/analysis-report.tsx`
- Modify: `tests/analysis-report.test.tsx`

- [ ] **Step 1: Add failing dispatcher and navigation tests**

```typescript
it("dispatches 1.3 to the six-region Finding workbench", async () => {
  render(<AnalysisReportView {...baseProps} report={analysisReportV13()} />);

  expect(screen.getByRole("navigation", { name: "Finding 工作台" })).toBeVisible();
  for (const label of [
    "概览",
    "问题清单",
    "证据与指标",
    "源码与优化",
    "SmartPerfetto 原始报告",
    "复测计划",
  ]) {
    expect(screen.getByRole("tab", { name: label })).toBeVisible();
  }
});

it("keeps 1.2 on the legacy three-tab source-aware report", () => {
  render(<AnalysisReportView {...baseProps} report={analysisReportV12()} />);

  expect(screen.getByRole("tab", { name: "结论" })).toBeVisible();
  expect(screen.queryByRole("tab", { name: "问题清单" })).toBeNull();
});
```

- [ ] **Step 2: Run and verify RED**

```bash
npx vitest run tests/analysis-report.test.tsx -t 'six-region|legacy three-tab'
```

Expected: FAIL because 1.3 is routed to the legacy branch.

- [ ] **Step 3: Implement the version dispatcher**

```tsx
export function AnalysisReportView(props: AnalysisReportViewProps) {
  if (props.report.schema_version === "1.3") {
    return (
      <FindingWorkbench
        client={props.client ?? defaultClient}
        report={props.report}
        teamId={props.teamId}
      />
    );
  }
  if (props.report.schema_version === "1.2") {
    return (
      <SourceAwareAnalysisReportView
        report={props.report}
        teamId={props.teamId}
        client={props.client ?? defaultClient}
      />
    );
  }
  return <LegacyAnalysisReportView {...props} report={props.report} />;
}
```

- [ ] **Step 4: Implement the workbench shell**

```tsx
const regions = [
  ["overview", "概览"],
  ["findings", "问题清单"],
  ["evidence", "证据与指标"],
  ["source", "源码与优化"],
  ["original", "SmartPerfetto 原始报告"],
  ["retest", "复测计划"],
] as const;

export function FindingWorkbench({ report, teamId, client }: Props) {
  const [region, setRegion] = useState<Region>("overview");
  const [selectedFindingId, setSelectedFindingId] = useState(
    report.workbench.primary_finding_ids[0] ?? report.workbench.findings[0]?.finding_id ?? null,
  );
  return (
    <article className="finding-workbench" aria-label="PerfPilot Finding 工作台">
      <nav className="finding-workbench-nav" aria-label="Finding 工作台" role="tablist">
        {regions.map(([id, label]) => (
          <button
            aria-controls={`finding-region-${id}`}
            aria-selected={region === id}
            key={id}
            onClick={() => setRegion(id)}
            role="tab"
            type="button"
          >
            {label}
          </button>
        ))}
      </nav>
      <FindingWorkbenchRegion
        client={client}
        region={region}
        report={report}
        selectedFindingId={selectedFindingId}
        setSelectedFindingId={setSelectedFindingId}
        teamId={teamId}
      />
    </article>
  );
}
```

Define `Props` with `openEvidence?: (target: TraceEvidenceTarget) => void`, defaulting to `defaultOpenEvidence`, and pass the selected Finding ID and opener through `FindingWorkbenchRegion`.

- [ ] **Step 5: Run and commit**

```bash
npx vitest run tests/analysis-report.test.tsx -t 'six-region|legacy three-tab'
npx eslint app/components/finding-workbench.tsx app/components/analysis-report.tsx tests/analysis-report.test.tsx
git add app/components/finding-workbench.tsx app/components/analysis-report.tsx tests/analysis-report.test.tsx
git commit -m "feat: route finding workbench reports"
```

Expected: PASS.

### Task 3: Build overview, top-three, list, and diagnostic chain

**Files:**
- Create: `app/components/finding-overview.tsx`
- Create: `app/components/finding-list.tsx`
- Create: `app/components/finding-detail.tsx`
- Modify: `app/components/finding-workbench.tsx`
- Modify: `tests/analysis-report.test.tsx`

- [ ] **Step 1: Add failing overview and expansion tests**

```typescript
it("shows green completion, capability truth, three primary findings, and all additional findings", async () => {
  render(<FindingWorkbench {...propsWithNineFindings()} />);

  expect(screen.getByText("分析完成")).toHaveClass("is-completed");
  expect(screen.getByText("源码匹配")).toBeVisible();
  expect(screen.getAllByTestId("primary-finding")).toHaveLength(3);
  expect(screen.getByText("展开其余 6 项")).toBeVisible();

  await userEvent.click(screen.getByText("展开其余 6 项"));
  expect(screen.getAllByTestId("additional-finding")).toHaveLength(6);
});

it("renders the fixed four-part diagnostic chain", async () => {
  render(<FindingWorkbench {...props()} />);

  expect(screen.getByText("1. 问题点")).toBeVisible();
  expect(screen.getByText("2. 为什么会有这个问题")).toBeVisible();
  expect(screen.getByText("3. 结合源码判断的根因是什么")).toBeVisible();
  expect(screen.getByText("4. 修改建议")).toBeVisible();
});

it("renders the server-owned critical path and filters all findings", async () => {
  render(<FindingWorkbench {...propsWithMixedPriorities()} />);

  expect(screen.getByText("Application 初始化")).toBeVisible();
  expect(screen.getByText("694 ms")).toBeVisible();
  await userEvent.click(screen.getByRole("tab", { name: "问题清单" }));
  await userEvent.selectOptions(screen.getByLabelText("优先级"), "p0");

  expect(screen.getAllByTestId("finding-list-item").map((node) => node.dataset.priority)).toEqual(["p0"]);
});
```

- [ ] **Step 2: Run and verify RED**

```bash
npx vitest run tests/analysis-report.test.tsx -t 'green completion|four-part diagnostic'
```

Expected: FAIL because the focused components do not exist.

- [ ] **Step 3: Implement deterministic top-three and additional findings**

```tsx
const findingById = new Map(
  report.workbench.findings.map((finding) => [finding.finding_id, finding]),
);
const primary = report.workbench.primary_finding_ids.flatMap((id) => {
  const finding = findingById.get(id);
  return finding ? [finding] : [];
});
const additional = report.workbench.findings.filter(
  (finding) => !report.workbench.primary_finding_ids.includes(finding.finding_id),
);
```

Render exactly three primary items at most. Render the real `additional.length` in a `<details>` summary and keep all additional items in stable server order.

Render the waterfall directly from `report.workbench.critical_path`; do not recalculate duration or ordering in React. `FindingList` keeps controlled `priority`, `evidenceGrade`, and `status` filters and preserves the server order:

```tsx
const visibleFindings = findings.filter((finding) =>
  (priority === "all" || finding.priority === priority) &&
  (evidenceGrade === "all" || finding.confidence.evidence_grade === evidenceGrade) &&
  (status === "all" || finding.status === status),
);
```

- [ ] **Step 4: Implement fixed diagnostic labels and confidence cards**

```tsx
<dl className="finding-diagnostic-chain">
  <div><dt>1. 问题点</dt><dd>{finding.problem}</dd></div>
  <div><dt>2. 为什么会有这个问题</dt><dd>{finding.mechanism}</dd></div>
  <div><dt>3. 结合源码判断的根因是什么</dt><dd>{sourceRootCause}</dd></div>
  <div><dt>4. 修改建议</dt><dd>{recommendation.summary}</dd></div>
</dl>
<ul aria-label="结论可信度" className="finding-confidence">
  <li>数据完整性：{confidence.data_completeness}</li>
  <li>证据等级：{confidence.evidence_grade}</li>
  <li>归因可信度：{confidence.attribution}</li>
  <li>统计可信度：{confidence.statistical}</li>
</ul>
```

If source capability is not `matched`, `sourceRootCause` must be the contract-provided source-unavailable sentence and must not read a source ref.

- [ ] **Step 5: Run and commit**

```bash
npx vitest run tests/analysis-report.test.tsx -t 'green completion|four-part diagnostic|additional findings'
git add app/components/finding-overview.tsx app/components/finding-list.tsx \
  app/components/finding-detail.tsx app/components/finding-workbench.tsx \
  tests/analysis-report.test.tsx
git commit -m "feat: show prioritized finding diagnostics"
```

Expected: PASS.

### Task 4: Add Evidence navigation, source-safe actions, and retest plans

**Files:**
- Create: `app/components/evidence-metrics-panel.tsx`
- Create: `app/components/trace-evidence-locator.tsx`
- Create: `app/analyses/[id]/trace/page.tsx`
- Create: `app/components/finding-source-panel.tsx`
- Create: `app/components/retest-plan-panel.tsx`
- Modify: `app/components/finding-workbench.tsx`
- Modify: `tests/analysis-report.test.tsx`

- [ ] **Step 1: Add failing Evidence and source privacy tests**

```typescript
it("opens a controlled Trace evidence deep link", async () => {
  const opener = vi.fn();
  render(<FindingWorkbench {...props()} openEvidence={opener} />);

  await userEvent.click(screen.getByRole("button", { name: "在 Trace 中打开证据" }));

  expect(opener).toHaveBeenCalledWith({ analysisId: ANALYSIS_ID, evidenceId: EVIDENCE_ID });
});

it.each(["mismatch", "unavailable", "not_requested"] as const)(
  "never renders source locations or Diff when source is %s",
  async (source) => {
    render(<FindingWorkbench {...props({ source })} />);

    await userEvent.click(screen.getByRole("tab", { name: "源码与优化" }));
    expect(screen.queryByText("app/src/main/java/demo/Startup.kt")).toBeNull();
    expect(screen.queryByLabelText("建议代码 Diff")).toBeNull();
    expect(screen.getByText("修改仅供参考")).toBeVisible();
  },
);

it("renders every distinct strong source fix and the reference notice", async () => {
  render(<FindingWorkbench {...propsWithThreeStrongFixes()} />);
  await userEvent.click(screen.getByRole("tab", { name: "源码与优化" }));

  expect(screen.getAllByLabelText("建议代码 Diff")).toHaveLength(3);
  expect(screen.getByText("修改仅供参考")).toBeVisible();
});

it("marks a changed retest environment as not directly comparable", async () => {
  render(<FindingWorkbench {...propsWithChangedRetestEnvironment()} />);
  await userEvent.click(screen.getByRole("tab", { name: "复测计划" }));

  expect(screen.getByText("环境发生变化，结果不能直接比较")).toBeVisible();
  expect(screen.queryByText("已解决")).toBeNull();
});
```

- [ ] **Step 2: Run and verify RED**

```bash
npx vitest run tests/analysis-report.test.tsx -t 'controlled Trace evidence|never renders source'
```

Expected: FAIL because Evidence/source/retest components are not connected.

- [ ] **Step 3: Implement the Evidence opener boundary**

```typescript
export interface TraceEvidenceTarget {
  readonly analysisId: string;
  readonly evidenceId: string;
}

function defaultOpenEvidence(target: TraceEvidenceTarget): void {
  const query = new URLSearchParams({ evidence: target.evidenceId });
  window.location.assign(`/analyses/${target.analysisId}/trace?${query.toString()}`);
}
```

Never accept a URL, time range, or path from the click target. Construct the relative URL only from validated analysis and Evidence IDs. `app/analyses/[id]/trace/page.tsx` passes the route IDs to `TraceEvidenceLocator`; that component loads the validated report through `PerfPilotClient`, finds the Evidence by ID, and renders its server-validated start/end time, process, thread, track, slice, and query ID with a “复制定位参数” action. Unknown or cross-analysis Evidence IDs render a stable “证据不存在” state. This first version is the controlled deep-link destination until an embedded Trace viewer is added.

- [ ] **Step 4: Implement source and retest panels**

The source panel must branch before reading refs:

```tsx
if (report.capabilities.source !== "matched") {
  return (
    <section aria-label="源码与优化">
      <h2>源码未匹配</h2>
      <p>本次保留基于 SmartPerfetto 证据的修改方案，不展示文件位置或 Diff。</p>
      <p>修改仅供参考</p>
      <ManualRecommendations report={report} />
    </section>
  );
}
```

The retest panel must render `scenario_type`, `package_name`, `duration_seconds`, environment fingerprint, metric IDs, and each pass criterion. It must label non-comparable environments before showing improvement status.

- [ ] **Step 5: Run and commit**

```bash
npx vitest run tests/analysis-report.test.tsx -t 'Trace evidence|source|retest'
npx eslint app/components tests/analysis-report.test.tsx
git add app/components/evidence-metrics-panel.tsx \
  app/components/trace-evidence-locator.tsx app/analyses/[id]/trace/page.tsx \
  app/components/finding-source-panel.tsx \
  app/components/retest-plan-panel.tsx \
  app/components/finding-workbench.tsx tests/analysis-report.test.tsx
git commit -m "feat: navigate evidence and retest findings"
```

Expected: PASS.

### Task 5: Add blue-white desktop and print behavior

**Files:**
- Modify: `app/globals.css`
- Modify: `app/components/full-analysis-report.tsx`
- Modify: `tests/full-analysis-report.test.tsx`

- [ ] **Step 1: Add failing visual-structure and print tests**

```typescript
it("prints all workbench regions and preloads original HTML", async () => {
  const client = fakeClientWithOriginalHtml();
  const printer = vi.fn();
  render(<FullAnalysisReport report={analysisReportV13()} client={client} printer={printer} />);

  await userEvent.click(screen.getByRole("button", { name: "下载 PDF" }));

  expect(client.smartPerfettoOriginal).toHaveBeenCalledTimes(1);
  expect(printer).toHaveBeenCalledTimes(1);
  expect(document.body).toHaveAttribute("data-report-printing", "true");
  for (const layer of ["overview", "findings", "evidence", "source", "original", "retest"]) {
    expect(document.querySelector(`[data-report-layer="${layer}"]`)).not.toBeNull();
  }
});
```

- [ ] **Step 2: Run and verify RED**

```bash
npx vitest run tests/full-analysis-report.test.tsx -t 'workbench regions'
```

Expected: FAIL because print logic knows only the 1.2 three-layer report.

- [ ] **Step 3: Add workbench CSS and print expansion**

Add blue-white desktop styles using existing variables and these stable selectors:

```css
.finding-workbench {
  display: grid;
  grid-template-columns: 14rem minmax(0, 1fr);
  min-height: 42rem;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: 1rem;
  background: #fff;
}

.finding-workbench-nav {
  padding: 1rem;
  border-right: 1px solid #dbe6f3;
  background: #f8fbff;
}

.finding-status.is-completed { color: #16845b; }

@media print {
  body[data-report-printing="true"] .finding-workbench {
    display: block;
  }
  body[data-report-printing="true"] [data-report-layer] {
    display: block !important;
  }
  body[data-report-printing="true"] .smartperfetto-original-json {
    display: none !important;
  }
}
```

- [ ] **Step 4: Preload original HTML before printing**

Reuse the existing bounded preload hook. Await it before `printer()` and render a stable Chinese error summary if preload fails; do not print a loading placeholder.

- [ ] **Step 5: Run and commit**

```bash
npx vitest run tests/full-analysis-report.test.tsx tests/analysis-report.test.tsx
npm run lint
npm run build
git add app/globals.css app/components/full-analysis-report.tsx tests/full-analysis-report.test.tsx
git commit -m "feat: style and print finding workbench"
```

Expected: tests, lint, and production build pass.

### Task 6: Add real uploaded-Trace acceptance and final gates

**Files:**
- Create: `services/api/tests/acceptance/test_finding_workbench_trace_upload.py`
- Modify: `services/api/tests/acceptance/test_analysis_reliability.py`

- [ ] **Step 1: Add the full acceptance test**

```python
def test_uploaded_trace_publishes_stable_finding_workbench(
    local_app_factory: LocalAppFactory,
    trace_fixture: Path,
) -> None:
    first = local_app_factory.run_trace_upload(
        trace_path=trace_fixture,
        package_name="com.rivotek.mediacenter",
        scenario_type="cold_start",
        source_binding=local_app_factory.matching_source_binding(),
    )

    assert first.report["schema_version"] == "1.3"
    assert first.report["state"] == "completed"
    assert first.public_state == "completed"
    assert len(first.report["workbench"]["primary_finding_ids"]) <= 3
    assert first.report["workbench"]["findings"]
    assert all(
        finding["evidence_ids"]
        for finding in first.report["workbench"]["findings"]
        if finding["status"] == "confirmed"
    )
    assert first.smartperfetto_original_html.startswith(b"<!doctype html")
    assert first.provider_calls == 1

    second = local_app_factory.rebuild_report(first.analysis_id)
    assert second.provider_calls == 0
    assert second.report["workbench"]["metrics"] == first.report["workbench"]["metrics"]
    assert [item["finding_id"] for item in second.report["workbench"]["findings"]] == [
        item["finding_id"] for item in first.report["workbench"]["findings"]
    ]
```

Add separate acceptance cases for source mismatch, two invalid AI candidates, cancel before publication, and restart after persisted projection.

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/acceptance/test_finding_workbench_trace_upload.py
```

Expected: FAIL until Plans 1 and 2 and Tasks 1–5 of this plan are complete.

- [ ] **Step 3: Complete the acceptance fixture without production shortcuts**

The fixture must use the real local Trace upload API, real report writer, real contract validation, real SmartPerfetto result polling adapter, and real synthesis orchestrator. Only external SmartPerfetto transport and AI provider may be deterministic fakes. Do not call private composer functions directly.

- [ ] **Step 4: Run backend, frontend, privacy, and build gates**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/contract/test_ai_report_contracts.py \
  services/api/tests/unit/test_finding_workbench.py \
  services/api/tests/unit/test_finding_fallback.py \
  services/api/tests/unit/test_finding_report.py \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  services/api/tests/unit/test_analysis_report_writer.py \
  services/api/tests/acceptance/test_analysis_reliability.py \
  services/api/tests/acceptance/test_finding_workbench_trace_upload.py
npx vitest run tests/perfpilot-api.test.ts tests/analysis-report.test.tsx tests/full-analysis-report.test.tsx
.venv/bin/ruff check services/api/src/perfpilot_api services/api/tests
npm run lint
npm run build
git diff --check
```

Expected: all tests pass, Ruff/ESLint/build are clean.

- [ ] **Step 5: Perform one manual real-Trace verification**

Require an explicit real Trace fixture and start the local stack:

```bash
test -n "$PERFPILOT_ACCEPTANCE_TRACE"
test -f "$PERFPILOT_ACCEPTANCE_TRACE"
npm run dev:restart
```

Upload `$PERFPILOT_ACCEPTANCE_TRACE` through the browser and verify:

1. green “分析完成” appears only after report publication;
2. exactly three primary findings appear when at least three exist;
3. additional Finding count is correct;
4. Evidence opens the expected Trace time range;
5. source matched shows relative paths and multiple Diff cards;
6. source mismatch shows no path, symbol, line, or Diff;
7. SmartPerfetto original HTML loads independently;
8. print contains all workbench regions and no technical appendix.

Save a screenshot under ignored `output/playwright/finding-workbench-real-trace.png` for handoff evidence.

- [ ] **Step 6: Commit acceptance**

```bash
git add services/api/tests/acceptance/test_finding_workbench_trace_upload.py \
  services/api/tests/acceptance/test_analysis_reliability.py
git commit -m "test: verify finding workbench trace workflow"
```

## Plan 3 completion gate

Run the commands from Task 6 Step 4 again from a clean working tree. Confirm `git status --short` contains no task files before claiming completion.
