# SmartPerfetto Original and Chinese Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authorized, separately downloadable SmartPerfetto original AI document while keeping PerfPilot as a distinct Chinese, source-aware optimization report that maps strong Trace evidence to direct code changes and retest steps.

**Architecture:** Treat the existing `smartperfetto-report.json` as an immutable private artifact bound to team, analysis, size, SHA-256, and version; do not call a model to regenerate it. Keep AnalysisReport 1.2 compact, expose the original document through a tenant-authorized lazy endpoint, and validate Chinese only at the AI narrative boundary while excluding technical identifiers, paths, metrics, code, and Unified Diff.

**Product boundary:** The two reports are intentionally not normalized into the same wording or structure. The SmartPerfetto tab renders the original kernel document faithfully; the PerfPilot tabs consume validated Trace plus source context and prioritize concrete file/class/method fixes, strong-only Unified Diff, expected impact, and retest instructions. Without strong source evidence, PerfPilot emits recommendation-only content and never invents code locations.

**Tech Stack:** Python 3.12, FastAPI streaming responses, Pydantic/JSON Schema contracts, pytest, Next.js 16, React 19, TypeScript, Vitest, CSS print media, OpenAI-compatible synthesis provider.

---

### Task 1: Immutable SmartPerfetto original artifact binding

**Files:**
- Create: `services/api/src/perfpilot_api/reports/smartperfetto_original.py`
- Create: `services/api/tests/unit/test_smartperfetto_original.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/src/perfpilot_api/local_analysis_store.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Write artifact RED tests**

```python
def test_original_report_is_bound_to_team_analysis_size_hash_and_version(tmp_path: Path) -> None:
    artifact = persist_smartperfetto_original(
        team_id=TEAM_A,
        analysis_id=ANALYSIS_A,
        document={"summary": "原始结论", "findings": []},
        root=tmp_path,
    )
    assert artifact.mime == "application/json"
    assert artifact.size == len(artifact.path.read_bytes())
    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    with pytest.raises(SmartPerfettoOriginalNotFound):
        read_smartperfetto_original(root=tmp_path, team_id=TEAM_B, analysis_id=ANALYSIS_A)
```

Add tests for a changed byte, changed version, oversized file, symlink substitution, unknown JSON keys in the binding, and errors containing no storage path.

- [ ] **Step 2: Run the RED tests**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_smartperfetto_original.py -q`

Expected: collection fails with `ModuleNotFoundError: perfpilot_api.reports.smartperfetto_original`.

- [ ] **Step 3: Implement the private artifact boundary**

```python
@dataclass(frozen=True, slots=True)
class SmartPerfettoOriginalBinding:
    artifact_id: UUID
    team_id: UUID
    analysis_id: UUID
    version: int
    mime: Literal["application/json"]
    size: int
    sha256: str

def persist_smartperfetto_original(*, root: Path, team_id: UUID,
                                   analysis_id: UUID,
                                   document: object) -> SmartPerfettoOriginalBinding:
    payload = canonical_json_bytes(document)
    return _write_private_artifact(root, team_id, analysis_id, payload)

def read_smartperfetto_original(*, root: Path, binding: SmartPerfettoOriginalBinding,
                                team_id: UUID, analysis_id: UUID,
                                maximum_bytes: int = 2 * 1024 * 1024) -> bytes:
    if binding.team_id != team_id or binding.analysis_id != analysis_id:
        raise SmartPerfettoOriginalNotFound
    payload = _read_private_artifact_no_follow(root, binding, maximum_bytes)
    if len(payload) != binding.size or sha256(payload).hexdigest() != binding.sha256:
        raise SmartPerfettoOriginalInvalid
    return payload
```

Implement canonical UTF-8 JSON persistence inside the analysis's private team directory, `0600` file mode, no-follow reads, exact identity/version/size/SHA-256 checks, and a stable redacted exception. Save only the public availability/version/size/checksum metadata in analysis state; never embed the full original document in AnalysisReport.

- [ ] **Step 4: Add the authorized endpoint and RED/GREEN integration tests**

Add:

```python
@app.get("/v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original")
async def smartperfetto_original(team_id: UUID, analysis_id: UUID, request: Request) -> Response:
    authorize_team(request, team_id)
    analysis = await runtime.analysis(team_id, analysis_id)
    payload = await asyncio.to_thread(runtime.read_smartperfetto_original, analysis)
    return Response(payload, media_type="application/json", headers={
        "cache-control": "private, no-store",
        "content-disposition": f'attachment; filename="smartperfetto-{analysis_id}.json"',
        "x-content-type-options": "nosniff",
    })
```

Test owner `200`, user B `404`, missing/corrupt artifact stable failure, correct download filename, no-store, nosniff, and unchanged PerfPilot report availability when the original artifact is missing.

- [ ] **Step 5: Run tests and commit**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_smartperfetto_original.py services/api/tests/integration/test_local_app.py -k 'smartperfetto_original or original_report' -q`

Expected: all selected tests pass.

```bash
git add services/api/src/perfpilot_api/reports/smartperfetto_original.py services/api/tests/unit/test_smartperfetto_original.py services/api/src/perfpilot_api/local_app.py services/api/src/perfpilot_api/local_analysis_store.py services/api/tests/integration/test_local_app.py
git commit -m "feat: serve SmartPerfetto original reports"
```

### Task 2: Fourth report tab, lazy view, and separate download

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/analysis-report.tsx`
- Create: `app/components/smartperfetto-original-report.tsx`
- Modify: `app/components/full-analysis-report.tsx`
- Modify: `app/lib/report-print.ts`
- Modify: `app/globals.css`
- Modify: `tests/perfpilot-api.test.ts`
- Modify: `tests/analysis-report.test.tsx`
- Modify: `tests/full-analysis-report.test.tsx`

- [ ] **Step 1: Write UI/client RED tests**

```tsx
it("loads the original report only after its fourth tab is selected", async () => {
  render(<AnalysisReport report={report12} client={client} teamId={TEAM} />);
  expect(client.smartPerfettoOriginal).not.toHaveBeenCalled();
  await user.click(screen.getByRole("tab", { name: "SmartPerfetto 原始报告" }));
  expect(await screen.findByText("原始结论")).toBeVisible();
  expect(client.smartPerfettoOriginal).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("link", { name: "下载原始报告" })).toHaveAttribute(
    "download", `smartperfetto-${ANALYSIS}.json`,
  );
});
```

Add tests for loading/error/retry, summary/findings/verification sections, full JSON disclosure, legacy 1.0/1.1 retaining three tabs, and print mode including the original summary but not the full JSON.

Add a regression asserting the SmartPerfetto tab renders its own original summary even when it differs from `report.executive_summary`, while the conclusion/source tabs continue to render PerfPilot's separate source-aware recommendation and strong-only Diff.

- [ ] **Step 2: Run UI/client RED**

Run: `npm test -- --run tests/perfpilot-api.test.ts tests/analysis-report.test.tsx tests/full-analysis-report.test.tsx`

Expected: tests fail because `PerfPilotClient.smartPerfettoOriginal` and the fourth tab do not exist.

- [ ] **Step 3: Add strict original-document client validation**

```ts
export type SmartPerfettoOriginal = Readonly<{
  summary: unknown;
  findings: readonly unknown[];
  claimVerificationResult?: unknown;
  analysisNotes?: unknown;
}>;

smartPerfettoOriginal(teamId: string, analysisId: string, signal?: AbortSignal):
  Promise<SmartPerfettoOriginal>;

smartPerfettoOriginalDownloadUrl(teamId: string, analysisId: string): string;
```

Enforce a 2 MiB response limit before JSON parse, require a top-level object, reject prototype-polluting keys recursively, preserve unknown SmartPerfetto domain fields for full JSON viewing, and never accept a URL or path from the response.

- [ ] **Step 4: Implement the fourth tab**

Add `SmartPerfettoOriginalReport` with a lazy `AbortController` fetch, structured rendering for `summary`, `findings`, and `claimVerificationResult`, a closed `<details>` for complete formatted JSON, and a same-origin download action. Keep the existing “结论 / 源码修复 / 技术附录” components unchanged except for tab registration. In print mode render only the original summary and finding titles, with `.smartperfetto-full-json { display: none !important; }`.

- [ ] **Step 5: Run frontend gates and commit**

Run: `npm test -- --run tests/perfpilot-api.test.ts tests/analysis-report.test.tsx tests/full-analysis-report.test.tsx`

Run: `npm run lint && npm run test:ssr`

Expected: all tests and checks pass.

```bash
git add app/lib/perfpilot-api.ts app/components/analysis-report.tsx app/components/smartperfetto-original-report.tsx app/components/full-analysis-report.tsx app/lib/report-print.ts app/globals.css tests/perfpilot-api.test.ts tests/analysis-report.test.tsx tests/full-analysis-report.test.tsx
git commit -m "feat: show SmartPerfetto original report"
```

### Task 3: Simplified-Chinese prompt and deterministic narrative validator

**Files:**
- Modify: `services/api/src/perfpilot_api/ai/prompts/perfpilot-report-v3.txt`
- Create: `services/api/src/perfpilot_api/ai/chinese_narrative.py`
- Create: `services/api/tests/unit/test_chinese_narrative.py`
- Modify: `services/api/src/perfpilot_api/ai/local_report.py`
- Modify: `services/api/tests/unit/test_local_ai_report.py`

- [ ] **Step 1: Write validator RED tests**

```python
@pytest.mark.parametrize("text", [
    "主线程在启动阶段连续执行磁盘读取，直接拉长 TTID。",
    "建议把 MainActivity.onCreate 中的 SQLite 初始化移到后台线程。",
])
def test_chinese_narrative_accepts_chinese_with_technical_terms(text: str) -> None:
    validate_simplified_chinese_narrative(narrative_document(text))

def test_chinese_narrative_rejects_english_user_facing_paragraph() -> None:
    with pytest.raises(ChineseNarrativeError):
        validate_simplified_chinese_narrative(
            narrative_document("The main thread is blocked by synchronous disk reads.")
        )

def test_validator_ignores_code_paths_metrics_ids_and_unified_diff() -> None:
    validate_simplified_chinese_narrative(document_with_english_only_technical_fields())
```

Cover every narrative field in AnalysisReport 1.2: executive summary, pain point, user impact, recommendation title/action/expected impact/retest/limitations, and source fix explanation. Do not inspect IDs, paths, symbols, rule names, metrics, code, or diff text.

- [ ] **Step 2: Run validator RED**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_chinese_narrative.py -q`

Expected: collection fails with `ModuleNotFoundError: perfpilot_api.ai.chinese_narrative`.

- [ ] **Step 3: Implement a deterministic narrative-only rule**

```python
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"\b[A-Za-z]{3,}\b")

def _acceptable(text: str) -> bool:
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN_WORD.findall(text))
    return cjk >= 2 and (latin == 0 or cjk >= latin * 2)
```

Extract only the explicit narrative fields into `_acceptable`; allow standard technical tokens within Chinese sentences. Raise `ChineseNarrativeError("ai_narrative_language_invalid")` without echoing candidate content.

- [ ] **Step 4: Strengthen the prompt with an explicit output-language contract**

Append this exact policy to `perfpilot-report-v3.txt`:

```text
OUTPUT LANGUAGE (mandatory): Write every user-facing narrative field in Simplified Chinese.
Keep Android/Perfetto terms, metrics, identifiers, class and method names, file paths, code, and Unified Diff unchanged.
Do not emit duplicated bilingual paragraphs. Preserve every numeric value and unit exactly.
```

- [ ] **Step 5: Enforce one retry in the same logical report round**

After JSON Schema and semantic validation in `LocalReportSynthesizer`, call `validate_simplified_chinese_narrative`. On the first language failure, issue exactly one corrective retry with `上一候选的面向用户叙述不是简体中文；只修正叙述语言，保持证据、ID、数值、代码和 Diff 不变。`. Count both provider attempts inside generation 1; never create a second synthesis round.

- [ ] **Step 6: Run AI tests and commit**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_chinese_narrative.py services/api/tests/unit/test_local_ai_report.py -q`

Expected: Chinese candidate uses one provider call; English then Chinese uses two; English twice raises `ai_narrative_language_invalid`; code/Diff English does not trigger retry.

```bash
git add services/api/src/perfpilot_api/ai/prompts/perfpilot-report-v3.txt services/api/src/perfpilot_api/ai/chinese_narrative.py services/api/tests/unit/test_chinese_narrative.py services/api/src/perfpilot_api/ai/local_report.py services/api/tests/unit/test_local_ai_report.py
git commit -m "feat: require Chinese performance narratives"
```

### Task 4: Degraded report retains the SmartPerfetto original

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `app/components/concise-report-summary.tsx`
- Modify: `tests/analysis-report.test.tsx`

- [ ] **Step 1: Write the two-failure RED acceptance test**

```python
def test_two_english_candidates_publish_partial_report_and_original(client, english_provider) -> None:
    analysis = complete_trace_analysis(client, provider=english_provider)
    report = client.get(report_url(analysis)).json()
    assert english_provider.calls == 2
    assert report["state"] == "partially_completed"
    assert report["ai"]["failure_code"] == "ai_narrative_language_invalid"
    original = client.get(original_url(analysis))
    assert original.status_code == 200
    assert original.json()["summary"]
```

- [ ] **Step 2: Run RED and implement the bounded degradation**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -k 'english_candidates' -q`

Expected: the current runtime either publishes English as success or loses the original document on AI failure.

In `_publish_prepared`, persist the SmartPerfetto original binding before calling the PerfPilot provider. Catch `ChineseNarrativeError`, publish the existing deterministic core as AnalysisReport 1.2 with `partially_completed`, retain `smartperfetto_original.available=true`, and expose the stable failure code. Do not perform a third provider call.

- [ ] **Step 3: Make the UI state explicit and test it**

Render `PerfPilot AI 中文总结生成失败；SmartPerfetto 原始报告和核心 Trace 结论仍可查看。` in the conclusion tab while leaving the fourth tab enabled.

Run: `npm test -- --run tests/analysis-report.test.tsx`

Expected: the degraded state and accessible fourth tab pass.

- [ ] **Step 4: Commit the degradation path**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py app/components/concise-report-summary.tsx tests/analysis-report.test.tsx
git commit -m "fix: preserve original report on AI language failure"
```

### Task 5: End-to-end verification, push, and Ubuntu activation

**Files:**
- Modify only files implicated by a failing gate.

- [ ] **Step 1: Run the complete focused backend and Agent gate**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_control_store.py services/api/tests/unit/test_local_agent_store.py services/api/tests/unit/test_local_analysis_store.py services/api/tests/unit/test_smartperfetto_original.py services/api/tests/unit/test_chinese_narrative.py services/api/tests/integration/test_local_app.py agents/device-agent/tests -q`

Expected: all tests pass.

- [ ] **Step 2: Run frontend and deployment gates**

Run: `npm test -- --run tests/perfpilot-api.test.ts tests/local-login.test.tsx tests/perfpilot-session-provider.test.tsx tests/dashboard.test.tsx tests/source-workspace-field.test.tsx tests/agent-management.test.tsx tests/analysis-report.test.tsx tests/full-analysis-report.test.tsx tests/ubuntu-user-deployment.test.ts`

Run: `npm run lint && npm run test:ssr && npm run build && git diff --check`

Expected: all tests, lint, SSR, production build, and diff checks pass.

- [ ] **Step 3: Apply verification-before-completion and commit any necessary scoped fix**

Re-run only the failed command after a fix, then re-run the complete gate that exposed it. If files changed:

```bash
git add services/api/src/perfpilot_api app tests services/api/tests infra/ubuntu-user scripts
git commit -m "fix: close multi-user report acceptance gaps"
```

- [ ] **Step 4: Push `main`**

Run: `git status --short && git log -1 --oneline && git push origin main`

Expected: worktree contains only the preserved untracked `.superpowers/`, and GitHub advances `main` to the verified commit.

- [ ] **Step 5: Deploy without printing secrets**

Run from the Mac:

```bash
ssh -i ~/.ssh/perfpilot_ubuntu_ed25519 rivotek@10.166.0.125 \
  'cd /home/rivotek/perfpilot/platform && git pull --ff-only origin main && bash scripts/bootstrap-ubuntu-user.sh'
```

Expected: bootstrap upgrades the code, creates/preserves `/home/rivotek/perfpilot/state`, idempotently creates `ray_wu` plus `user01`–`user05`, permanently clears `/home/rivotek/perfpilot/data/local-runtime`, and reports all three services healthy. Temporary passwords are available only in `/home/rivotek/perfpilot/state/bootstrap-users.txt` mode `0600`; do not print that file through Codex tool output.

- [ ] **Step 6: Run remote acceptance**

Verify over authenticated HTTP/browser sessions:

1. `ray_wu` remains admin; five ordinary accounts exist and require initial password change.
2. User01 creates an analysis; user02 receives `404` for its analysis, report, original report, cancel, and download URLs.
3. `systemctl --user restart perfpilot.target` removes all analysis files and leaves `state/control.json` and `state/agents.json` byte-identical.
4. The dashboard is empty after restart; all six accounts can still authenticate.
5. A user-generated Agent registration code can register the Mac Agent; `source add` uses a path selected by that user; only that user's selector shows the workspace.
6. A completed trace shows four report tabs, PerfPilot narrative is Chinese, and the SmartPerfetto original JSON downloads as `smartperfetto-{analysis_id}.json`.
7. Deliberately different SmartPerfetto and PerfPilot fixture conclusions remain different in the UI; the PerfPilot view contains a concrete source file/class/method action, expected impact, and retest step, while the original tab remains byte-faithful to SmartPerfetto.

- [ ] **Step 7: Record deployment evidence**

Record only commit IDs, service states, test counts, HTTP status codes, and checksums in the handoff. Do not record passwords, session cookies, registration codes, tokens, absolute source paths, Trace contents, or report contents.
