# PerfPilot Local Multi-Round Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist local Trace analyses, run three real PerfPilot AI synthesis rounds, and expose a clickable full report page styled like the existing PerfPilot/SmartPerfetto report experience.

**Architecture:** Keep SmartPerfetto behind its HTTP adapter and treat its sanitized report as the authoritative evidence source. Build a bounded AI projection, run three schema-constrained revisions through an injected provider, persist each checkpoint atomically, and render the final `AnalysisReport` on a dedicated web route. A local recovery endpoint imports the already completed SmartPerfetto session without rerunning the Trace.

**Tech Stack:** FastAPI, Pydantic, httpx, atomic JSON files, React, TypeScript, Vinext, Vitest, pytest, Playwright.

---

### Task 1: Persist local analysis checkpoints

**Files:**
- Create: `services/api/src/perfpilot_api/local_analysis_store.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/unit/test_local_analysis_store.py`
- Test: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Write the failing atomic-store tests**

```python
def test_store_round_trips_analysis_without_secrets(tmp_path: Path) -> None:
    store = LocalAnalysisStore(tmp_path)
    store.save_state(ANALYSIS_ID, {"schema_version": "1.0", "state": "analyzing"})
    assert store.load_states() == {
        ANALYSIS_ID: {"schema_version": "1.0", "state": "analyzing"}
    }


def test_store_rejects_symlinked_analysis_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "analyses").mkdir()
    (tmp_path / "analyses" / str(ANALYSIS_ID)).symlink_to(outside)
    with pytest.raises(LocalAnalysisStoreError):
        LocalAnalysisStore(tmp_path).save_state(ANALYSIS_ID, {"schema_version": "1.0"})
```

- [ ] **Step 2: Run the store tests and confirm RED**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_analysis_store.py -q`

Expected: collection fails because `perfpilot_api.local_analysis_store` does not exist.

- [ ] **Step 3: Implement focused atomic JSON persistence**

```python
class LocalAnalysisStore:
    def __init__(self, data_root: Path) -> None:
        self._root = data_root.resolve() / "analyses"
        self._root.mkdir(parents=True, exist_ok=True)

    def save_document(self, analysis_id: UUID, name: str, value: Mapping[str, object]) -> None:
        target = self._safe_analysis_dir(analysis_id) / name
        payload = canonical_json_bytes(dict(value))
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def save_state(self, analysis_id: UUID, value: Mapping[str, object]) -> None:
        self.save_document(analysis_id, "state.json", value)
```

The implementation must validate UUID directory names, reject symlink traversal, bound each JSON file, require exact UTF-8 JSON objects, and never serialize Provider tokens.

- [ ] **Step 4: Load persisted completed analyses in `_LocalRuntime`**

```python
async def start(self) -> None:
    restored = await asyncio.to_thread(self.store.load_states)
    async with self.lock:
        for analysis_id, state in restored.items():
            self.analyses[analysis_id] = self._restore_analysis(state)
```

Call `runtime.start()` from the FastAPI lifespan before yielding. Persist after create, upload reservation, finalization, SmartPerfetto submission, every AI round transition, completion, and failure.

- [ ] **Step 5: Run tests and confirm GREEN**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_analysis_store.py services/api/tests/integration/test_local_app.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the persistence slice**

```bash
git add services/api/src/perfpilot_api/local_analysis_store.py services/api/src/perfpilot_api/local_app.py services/api/tests/unit/test_local_analysis_store.py services/api/tests/integration/test_local_app.py
git commit -m "feat: persist local analysis checkpoints"
```

### Task 2: Run three real, bounded AI revisions

**Files:**
- Create: `services/api/src/perfpilot_api/ai/local_multiround.py`
- Create: `services/api/src/perfpilot_api/ai/prompts/perfpilot-local-extract-v1.txt`
- Create: `services/api/src/perfpilot_api/ai/prompts/perfpilot-local-review-v1.txt`
- Create: `services/api/src/perfpilot_api/ai/prompts/perfpilot-local-finalize-v1.txt`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/unit/test_local_multiround.py`

- [ ] **Step 1: Write failing sequential-round tests**

```python
@pytest.mark.asyncio
async def test_runner_executes_extract_review_finalize_in_order(projection: AIProjection) -> None:
    provider = FakeRoundProvider([EXTRACT_JSON, REVIEW_JSON, FINAL_JSON])
    runner = LocalMultiRoundSynthesizer(provider=provider)
    result = await runner.synthesize(projection, on_round=AsyncMock())
    assert provider.roles == ["extract", "review", "finalize"]
    assert result.output.document == FINAL_DOCUMENT
    assert result.rounds_completed == 3


@pytest.mark.asyncio
async def test_runner_never_executes_later_round_after_invalid_reference(
    projection: AIProjection,
) -> None:
    provider = FakeRoundProvider([INVALID_FINDING_JSON])
    with pytest.raises(LocalSynthesisError, match="ai_output_invalid"):
        await LocalMultiRoundSynthesizer(provider=provider).synthesize(projection)
    assert provider.roles == ["extract"]
```

- [ ] **Step 2: Run the multiround tests and confirm RED**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_multiround.py -q`

Expected: collection fails because `local_multiround` does not exist.

- [ ] **Step 3: Implement the provider-neutral three-round runner**

```python
RoundRole = Literal["extract", "review", "finalize"]
RoundState = Literal["running", "completed"]
RoundObserver = Callable[
    [int, RoundRole, RoundState, AISynthesisOutput | None],
    Awaitable[None],
]


class LocalSynthesisError(RuntimeError):
    pass


class LocalRoundProvider(Protocol):
    async def complete(
        self,
        *,
        role: RoundRole,
        projection: AIProjection,
        prior_outputs: Sequence[AISynthesisOutput],
    ) -> SynthesisCandidate:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LocalRoundUsage:
    round: int
    role: RoundRole
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LocalSynthesisResult:
    output: AISynthesisOutput
    rounds: tuple[LocalRoundUsage, LocalRoundUsage, LocalRoundUsage]


class LocalMultiRoundSynthesizer:
    async def synthesize(
        self,
        projection: AIProjection,
        *,
        on_round: RoundObserver | None = None,
    ) -> LocalSynthesisResult:
        outputs: list[AISynthesisOutput] = []
        usages: list[LocalRoundUsage] = []
        for number, role in enumerate(("extract", "review", "finalize"), start=1):
            if on_round is not None:
                await on_round(number, role, "running", None)
            candidate = await self._provider.complete(
                role=role,
                projection=projection,
                prior_outputs=tuple(outputs),
            )
            output = validate_synthesis_output(candidate.candidate_json, projection)
            outputs.append(output)
            usages.append(
                LocalRoundUsage(
                    round=number,
                    role=role,
                    prompt_tokens=candidate.prompt_tokens,
                    completion_tokens=candidate.completion_tokens,
                    latency_ms=candidate.latency_ms,
                )
            )
            if on_round is not None:
                await on_round(number, role, "completed", output)
        if len(usages) != 3:
            raise LocalSynthesisError("ai_round_incomplete")
        return LocalSynthesisResult(
            output=outputs[-1],
            rounds=(usages[0], usages[1], usages[2]),
        )
```

Each revision must satisfy the existing synthesis schema and semantic reference validator. The review and final prompts receive prior validated output, but the authoritative projection always wins.

- [ ] **Step 4: Add the OpenAI-compatible local provider factory**

Read only `PERFPILOT_LOCAL_AI_BASE_URL`, `PERFPILOT_LOCAL_AI_MODEL`, `PERFPILOT_LOCAL_AI_TOKEN`, and `PERFPILOT_LOCAL_AI_PROVIDER_NAME`. Build three `OpenAICompatibleSynthesisProvider` instances with immutable prompts and a shared redirect-disabled `httpx.AsyncClient`. Missing configuration returns `ai_not_configured`; it never falls back to deterministic content while claiming AI completion.

- [ ] **Step 5: Integrate the runner after SmartPerfetto normalization**

```python
projection = build_ai_projection(
    normalized,
    analysis_profile=analysis.profile,
    question=analysis.question,
)
result = await self.synthesizer.synthesize(
    projection,
    on_round=lambda number, role, state, output: self._record_round(
        analysis, number, role, state, output
    ),
)
report = _compose_local_report(
    analysis,
    canonical=canonical,
    normalized=normalized,
    synthesis=result.output.document,
    rounds=result.rounds,
)
```

- [ ] **Step 6: Run tests and confirm GREEN**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_multiround.py services/api/tests/integration/test_local_app.py -q`

Expected: all selected tests pass, including partial completion and retry cases.

- [ ] **Step 7: Commit the AI slice**

```bash
git add services/api/src/perfpilot_api/ai/local_multiround.py services/api/src/perfpilot_api/ai/prompts/perfpilot-local-*-v1.txt services/api/src/perfpilot_api/local_app.py services/api/tests/unit/test_local_multiround.py services/api/tests/integration/test_local_app.py
git commit -m "feat: run three-stage local AI synthesis"
```

### Task 3: Recover completed SmartPerfetto reports and expose round metadata

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `app/lib/perfpilot-api.ts`
- Test: `services/api/tests/integration/test_local_app.py`
- Test: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: Write failing recovery and API-contract tests**

```python
def test_recovery_imports_completed_session_once(client, gateway) -> None:
    payload = {
        "schema_version": "1.0",
        "session_id": "agent-completed-1",
        "run_id": "run-completed-1",
        "profile": "auto",
        "trace_size": 10_000_000,
        "trace_sha256_b64": CHECKSUM,
    }
    headers = {"x-csrf-token": csrf(client)}
    first = client.post(
        f"/v1/teams/{LOCAL_TEAM_ID}/local-recoveries",
        headers=headers,
        json=payload,
    )
    second = client.post(
        f"/v1/teams/{LOCAL_TEAM_ID}/local-recoveries",
        headers=headers,
        json=payload,
    )
    assert first.status_code == 201
    assert second.json()["analysis_id"] == first.json()["analysis_id"]
```

```ts
expect(await client.analysis(TEAM_ID, ANALYSIS_ID)).toMatchObject({
  ai_rounds: [
    { round: 1, role: "extract", state: "completed" },
    { round: 2, role: "review", state: "completed" },
    { round: 3, role: "finalize", state: "completed" },
  ],
  source_analysis: { engine: "smartperfetto", rounds: 53 },
});
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -q && npx vitest run tests/perfpilot-api.test.ts`

Expected: recovery endpoint and `ai_rounds` contract are absent.

- [ ] **Step 3: Implement idempotent local recovery**

The CSRF-protected endpoint verifies SmartPerfetto status, fetches the sanitized report, extracts its reported round count, creates a stable UUIDv5 from workspace plus session ID, persists the source report, and starts missing PerfPilot rounds. It never accepts a report URL or local path from the browser.

- [ ] **Step 4: Extend the browser API validator**

```ts
export interface AnalysisAiRound {
  readonly round: 1 | 2 | 3;
  readonly role: "extract" | "review" | "finalize";
  readonly state: "pending" | "running" | "completed" | "failed";
}

export interface AnalysisSource {
  readonly engine: "smartperfetto";
  readonly rounds: number | null;
  readonly verification: "passed" | "failed" | "unknown";
}
```

Require exact ordering and exact keys. Production responses may omit these local metadata fields; local responses must provide both.

- [ ] **Step 5: Run tests and confirm GREEN**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -q && npx vitest run tests/perfpilot-api.test.ts`

Expected: recovery is idempotent and TypeScript validation accepts only the exact round sequence.

- [ ] **Step 6: Commit the recovery slice**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py app/lib/perfpilot-api.ts tests/perfpilot-api.test.ts
git commit -m "feat: recover completed SmartPerfetto reports"
```

### Task 4: Add a clickable full final-report page

**Files:**
- Create: `app/components/full-analysis-report.tsx`
- Create: `app/analyses/[id]/report/page.tsx`
- Modify: `app/components/analysis-progress.tsx`
- Modify: `app/components/analysis-report.tsx`
- Modify: `app/globals.css`
- Test: `tests/analysis-progress.test.tsx`
- Test: `tests/full-analysis-report.test.tsx`
- Test: `tests/rendered-html.test.mjs`

- [ ] **Step 1: Write failing navigation and report-page tests**

```tsx
render(<AnalysisProgressView analysis={completedAnalysis} report={report} />);
expect(screen.getByRole("link", { name: "打开完整报告" })).toHaveAttribute(
  "href",
  "/analyses/analysis-live-1/report",
);
```

```tsx
render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} />);
expect(await screen.findByRole("heading", { name: "最终性能报告" })).toBeVisible();
expect(screen.getByText("53 轮 SmartPerfetto 分析")).toBeVisible();
expect(screen.getByText("3 轮 PerfPilot AI 已完成")).toBeVisible();
expect(screen.getByRole("heading", { name: "优化建议" })).toBeVisible();
```

- [ ] **Step 2: Run the UI tests and confirm RED**

Run: `npx vitest run tests/analysis-progress.test.tsx tests/full-analysis-report.test.tsx`

Expected: the full report component and link do not exist.

- [ ] **Step 3: Add the report entry point**

```tsx
{report ? (
  <Link className="analysis-open-report" href={`/analyses/${analysis.analysis_id}/report`}>
    打开完整报告
    <ArrowUpRight aria-hidden="true" />
  </Link>
) : null}
```

- [ ] **Step 4: Build the dedicated report route**

```tsx
interface PageProps {
  readonly params: Promise<{ readonly id: string }>;
}

export default async function FinalReportPage({ params }: PageProps) {
  const { id } = await params;
  return <FullAnalysisReport analysisId={id} />;
}
```

The client component reuses the authenticated analysis loader and `AnalysisReportView`. It adds a report masthead, engine/AI process summary, back navigation, generation metadata, and a clear source boundary. It renders real API data only.

- [ ] **Step 5: Style the report like a focused SmartPerfetto document**

Use a centered reading column, sticky compact masthead, high-contrast section titles, restrained cards, evidence anchors, print styles, and responsive single-column behavior. Keep existing fonts and color tokens; do not clone SmartPerfetto branding or embed its HTML.

- [ ] **Step 6: Run UI and SSR tests and confirm GREEN**

Run: `npx vitest run tests/analysis-progress.test.tsx tests/full-analysis-report.test.tsx && npm run test:ssr`

Expected: all tests pass and `/analyses/:id/report` server-renders without demo content.

- [ ] **Step 7: Commit the web slice**

```bash
git add app/components/full-analysis-report.tsx app/analyses/[id]/report/page.tsx app/components/analysis-progress.tsx app/components/analysis-report.tsx app/globals.css tests/analysis-progress.test.tsx tests/full-analysis-report.test.tsx tests/rendered-html.test.mjs
git commit -m "feat: open full AI analysis reports"
```

### Task 5: Recover the current report and verify the real browser flow

**Files:**
- Modify: `.dev.vars.example`
- Modify: `README.md`

- [ ] **Step 1: Start the local API with AI configuration**

Map the existing local DeepSeek credential into the four `PERFPILOT_LOCAL_AI_*` environment variables without printing or persisting the token. Restart only the local API; keep SmartPerfetto and the web server running.

- [ ] **Step 2: Import the completed source session**

POST the recovery request for:

```text
session_id=agent-1785835764755-xvfs1ut4
run_id=run-agent-1785835764755-xvfs1ut4-1-1785835764757-hv80g5
profile=auto
```

Poll until all three PerfPilot rounds and the report stage reach a terminal state. Record the returned PerfPilot analysis ID.

- [ ] **Step 3: Verify persistence across restart**

Restart the local API. GET the same analysis and report URLs. Confirm the analysis ID, SmartPerfetto round count, three AI rounds, report version, and final content remain unchanged.

- [ ] **Step 4: Verify the browser with Playwright**

Open `/analyses/<analysis-id>`, click “打开完整报告,” and snapshot the dedicated page. Confirm it shows the actual package, SmartPerfetto round count, three completed PerfPilot rounds, five or fewer evidence-backed findings, recommendations, retest plan, and limitations. Confirm the console has no application errors.

- [ ] **Step 5: Run the full verification gate**

```bash
.venv/bin/pytest -p no:cacheprovider -q
npm run test:unit
npm run lint
.venv/bin/ruff check .
npm run test:ssr
git diff --check
```

Expected: every command exits 0. The real local browser route remains readable after an API restart.

- [ ] **Step 6: Commit local documentation**

```bash
git add .dev.vars.example README.md docs/superpowers/plans/2026-08-04-perfpilot-local-multiround-report.md
git commit -m "docs: explain local AI report runtime"
```
