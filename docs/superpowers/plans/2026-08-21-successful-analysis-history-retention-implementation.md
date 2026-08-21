# Successful Analysis History Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 精简报告与主导航，并为每个团队提供只保留最近 10 份可查看成功报告的测试历史。

**Architecture:** 服务端用统一的“终态且报告可读”判定驱动历史查询和安全裁剪，在报告原子发布后及运行时启动恢复后清理超额成功记录。前端复用现有团队级分析列表接口，用独立客户端组件展示 10 份历史；报告工作台只移除 Evidence 的可见页签，不改变报告合同和证据生成链。

**Tech Stack:** Python 3.12、FastAPI、pytest、TypeScript、React 19、Next/vinext、Vitest、Testing Library、CSS

---

## 文件结构

- 修改 `services/api/src/perfpilot_api/local_app.py`：定义成功历史判定、执行团队级最近 10 份裁剪，并在发布与启动恢复后调用。
- 修改 `services/api/tests/integration/test_local_app.py`：覆盖发布后保留、超额裁剪、非成功任务排除、团队隔离、清理失败和重启收敛。
- 修改 `app/components/finding-workbench.tsx`：把 AnalysisReport 1.3 工作台从六个区域改为五个区域。
- 修改 `app/components/analysis-report.tsx`：移除只服务于 Evidence 页签的回调类型与属性。
- 删除 `app/components/evidence-metrics-panel.tsx`：移除不再使用的可见 Evidence 面板。
- 修改 `tests/analysis-report.test.tsx`：锁定五页签结构，并确认底层报告仍保留 Evidence 数据。
- 修改 `app/components/app-shell.tsx`：从主导航移除“问题”和“对比”。
- 修改 `tests/app-shell-device.test.tsx`：锁定精简后的导航入口。
- 新建 `app/components/analysis-history.tsx`：读取和展示当前团队最近 10 份成功报告。
- 修改 `app/tests/page.tsx`：用真实历史组件替换占位页。
- 修改 `app/globals.css`：增加蓝白风格的历史列表、状态和空态样式。
- 新建 `tests/analysis-history.test.tsx`：覆盖加载、成功列表、字段降级、空态与错误态。
- 修改 `tests/rendered-html.test.mjs`：让匿名 SSR 合同与登录门禁一致；历史正文由组件测试和登录后的 API 验收覆盖。

### Task 1: 建立成功报告判定并停止低于上限时误删

**Files:**
- Modify: `services/api/tests/integration/test_local_app.py:3658-3760`
- Modify: `services/api/src/perfpilot_api/local_app.py:1636-1668`
- Modify: `services/api/src/perfpilot_api/local_app.py:3022-3042`
- Modify: `services/api/src/perfpilot_api/local_app.py:4370-4378`

- [ ] **Step 1: 把现有删除测试改成“两份成功报告都保留”的失败测试**

将 `test_successful_trace_submission_removes_only_current_team_previous_analysis` 改名为 `test_successful_trace_submission_preserves_previous_report_below_retention_limit`，保留两次真实上传和失败 finalize 的安排，将结尾断言改为：

```python
        assert client.get(
            f"/v1/teams/{team_id}/analyses/{first_id}/report"
        ).status_code == 200
        history = client.get(
            f"/v1/teams/{team_id}/analyses?report_available=true&limit=10"
        )
        assert history.status_code == 200
        assert [item["analysis_id"] for item in history.json()["analyses"]] == [
            second_id,
            first_id,
        ]

    reopened = LocalAnalysisStore(tmp_path)
    assert (UUID(team_id), UUID(first_id)) in reopened.load_states()
    assert reopened.load_document(
        other_team_id, other_analysis_id, "state.json"
    ) == {
        "team_id": str(other_team_id),
        "analysis_id": str(other_analysis_id),
        "state": "completed",
    }
    reopened.close()
```

- [ ] **Step 2: 运行测试并确认旧逻辑导致失败**

Run:

```bash
.venv/bin/python -m pytest services/api/tests/integration/test_local_app.py::test_successful_trace_submission_preserves_previous_report_below_retention_limit -q
```

Expected: FAIL；第一份报告返回 `404`，证明现有 `_remove_previous_team_analyses` 仍在删除全部旧终态分析。

- [ ] **Step 3: 增加统一成功判定和保留上限**

在 `_LocalRuntime` 中增加：

```python
    SUCCESSFUL_ANALYSIS_RETENTION_LIMIT = 10

    @staticmethod
    def _is_successful_analysis(analysis: _LocalAnalysis) -> bool:
        return (
            analysis.report is not None
            and analysis.state in {"completed", "partially_completed"}
        )
```

将 `report_analyses` 的筛选改为：

```python
            available = tuple(
                analysis
                for (current_team_id, _), analysis in self.analyses.items()
                if current_team_id == team_id
                and self._is_successful_analysis(analysis)
            )
```

- [ ] **Step 4: 删除“任意新报告发布即清空旧终态分析”的调用**

删除 `_remove_previous_team_analyses` 方法，以及 `_publish_prepared` 结尾的以下调用：

```python
        if (
            analysis.analysis_mode == "trace_upload"
            and analysis.trace_test_type is not None
        ):
            await self._remove_previous_team_analyses(analysis)
```

本任务只修复“低于 10 份也会误删”的行为。最近 10 份的裁剪由 Task 2 的第 11 份失败测试驱动。

- [ ] **Step 5: 运行定向测试并确认通过**

Run:

```bash
.venv/bin/python -m pytest services/api/tests/integration/test_local_app.py::test_successful_trace_submission_preserves_previous_report_below_retention_limit -q
```

Expected: `1 passed`。

- [ ] **Step 6: 提交基础保留语义**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py
git commit -m "fix: preserve successful analysis history"
```

### Task 2: 实现第 11 份裁剪、非成功任务保护和启动恢复

**Files:**
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/src/perfpilot_api/local_app.py:2294-2525`

- [ ] **Step 1: 增加轮询成功报告的测试辅助函数**

在 `_upload_and_finalize_trace` 后增加：

```python
def _wait_for_report(
    client: TestClient,
    *,
    team_id: str,
    analysis_id: str,
) -> dict[str, object]:
    response = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}/report")
    for _ in range(200):
        if response.status_code == 200:
            return response.json()
        time.sleep(0.01)
        response = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )
    pytest.fail(f"analysis report did not become available: {analysis_id}")
```

- [ ] **Step 2: 写第 11 份成功报告的失败测试**

增加 `test_successful_analysis_retention_keeps_latest_ten_without_deleting_non_successes`。测试先创建一个未上传的活动任务和一个已取消任务，再连续完成 11 份 Trace 分析：

```python
    successful_ids: list[str] = []
    for index in range(11):
        trace = f"retained-trace-{index:02d}".encode()
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
            trace=trace,
            package_name="com.rivotek.mediacenter",
            schema_version="1.3",
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
            trace=trace,
        )
        _wait_for_report(client, team_id=team_id, analysis_id=analysis_id)
        successful_ids.append(analysis_id)

    history = client.get(
        f"/v1/teams/{team_id}/analyses?report_available=true&limit=20"
    ).json()["analyses"]
    assert [item["analysis_id"] for item in history] == list(
        reversed(successful_ids[1:])
    )
    assert client.get(
        f"/v1/teams/{team_id}/analyses/{successful_ids[0]}"
    ).status_code == 404
    assert client.get(
        f"/v1/teams/{team_id}/analyses/{active_id}"
    ).status_code == 200
    assert client.get(
        f"/v1/teams/{team_id}/analyses/{canceled_id}"
    ).status_code == 200
```

同一测试在另一个团队目录保存哨兵状态，并在裁剪后断言它仍存在。磁盘断言必须确认第一个成功分析目录不存在，而活动、取消和另一个团队目录存在。

- [ ] **Step 3: 运行测试并确认尚未执行上限裁剪**

Run:

```bash
.venv/bin/python -m pytest services/api/tests/integration/test_local_app.py::test_successful_analysis_retention_keeps_latest_ten_without_deleting_non_successes -q
```

Expected: FAIL；历史接口仍返回 11 份成功报告，最旧成功目录仍存在。

- [ ] **Step 4: 写启动恢复超额历史的失败测试**

增加 `test_local_runtime_prunes_excess_successful_history_on_restart`：先完成 10 份报告并关闭应用，再复制一份有效的旧成功状态和 `report.json` 为更早的第 11 份记录，随后重建应用。新应用启动后断言列表为最新 10 份、旧目录被删除。

关键持久化字段必须同时改写：

```python
    old_state["analysis_id"] = str(old_id)
    old_state["created_at"] = "2000-01-01T00:00:00+00:00"
    old_report["analysis_id"] = str(old_id)
    store.save_state(parsed_team_id, old_id, old_state)
    store.save_document(parsed_team_id, old_id, "report.json", old_report)
```

- [ ] **Step 5: 运行重启测试并确认尚未启动裁剪**

Run:

```bash
.venv/bin/python -m pytest services/api/tests/integration/test_local_app.py::test_local_runtime_prunes_excess_successful_history_on_restart -q
```

Expected: FAIL；重启后仍能读取第 11 份旧成功记录。

- [ ] **Step 6: 实现安全裁剪，并接入报告发布和启动恢复**

在 `_LocalRuntime` 中增加：

```python
    async def _prune_successful_team_analyses(self, team_id: UUID) -> None:
        async with self.lock:
            successful = tuple(
                current
                for (current_team_id, _), current in self.analyses.items()
                if current_team_id == team_id
                and self._is_successful_analysis(current)
            )
            expired = tuple(
                sorted(
                    successful,
                    key=lambda current: (
                        current.created_at,
                        str(current.analysis_id),
                    ),
                    reverse=True,
                )[self.SUCCESSFUL_ANALYSIS_RETENTION_LIMIT :]
            )

        for current in expired:
            key = (current.team_id, current.analysis_id)
            try:
                await asyncio.to_thread(
                    self.store.remove_analysis,
                    current.team_id,
                    current.analysis_id,
                )
            except LocalAnalysisStoreError as error:
                _LOGGER.warning(
                    "Successful analysis retention cleanup deferred "
                    "team_id=%s analysis_id=%s type=%s",
                    current.team_id,
                    current.analysis_id,
                    type(error).__name__,
                )
                continue
            async with self.lock:
                if self.analyses.get(key) is not current:
                    continue
                del self.analyses[key]
                self.remote_capture.discard(self._remote_capture_context(current))
                self.terminal_commit_locks.pop(key, None)
                for upload in tuple(self.uploads.values()):
                    if (
                        upload.team_id == current.team_id
                        and upload.analysis_id == current.analysis_id
                    ):
                        self._unregister_upload(upload)
```

报告终态状态成功持久化后，无条件执行：

```python
        await self._prune_successful_team_analyses(analysis.team_id)
```

在 `start()` 读取循环前建立团队集合，在恢复每条分析时登记团队，并在所有持久化状态恢复完成后、远端发布恢复前执行：

```python
        restored_team_ids: set[UUID] = set()
        # 恢复循环中：
        restored_team_ids.add(team_id)

        # 恢复循环结束后：
        for restored_team_id in sorted(restored_team_ids, key=str):
            await self._prune_successful_team_analyses(restored_team_id)
```

`_is_successful_analysis` 只接受 `completed` 和 `partially_completed`，因此正在恢复的活动任务不会被清理。

- [ ] **Step 7: 增加清理失败不回滚报告的测试**

增加 `test_successful_analysis_cleanup_failure_keeps_new_report_available`。准备超过上限的成功记录后，用 `monkeypatch` 让 `runtime.store.remove_analysis` 抛出 `LocalAnalysisStoreError`，调用裁剪并断言：

```python
    assert newest.report is not None
    assert newest.state in {"completed", "partially_completed"}
    assert len(await runtime.report_analyses(parsed_team_id, limit=20)) == 11
```

恢复原方法后再次裁剪，断言数量收敛到 10。

- [ ] **Step 8: 运行全部保留策略测试**

Run:

```bash
.venv/bin/python -m pytest services/api/tests/integration/test_local_app.py -k "retention or preserves_previous_report" -q
```

Expected: 所有选中测试 PASS。

- [ ] **Step 9: 提交完整裁剪与启动恢复**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py
git commit -m "feat: retain latest ten successful analyses"
```

### Task 3: 从 AnalysisReport 1.3 移除“证据与指标”可见区域

**Files:**
- Modify: `tests/analysis-report.test.tsx:396-482`
- Modify: `app/components/finding-workbench.tsx`
- Modify: `app/components/analysis-report.tsx:1-32,160-175`
- Delete: `app/components/evidence-metrics-panel.tsx`

- [ ] **Step 1: 把六页签测试改成五页签失败测试**

将测试名改为 `dispatches AnalysisReport 1.3 to the five-region Finding workbench`，期望页签改为：

```tsx
    for (const label of [
      "概览",
      "问题清单",
      "源码与优化",
      "SmartPerfetto 原始报告",
      "复测计划",
    ]) {
      expect(screen.getByRole("tab", { name: label })).toBeVisible();
    }
    expect(screen.queryByRole("tab", { name: "证据与指标" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "在 Trace 中打开证据" }),
    ).not.toBeInTheDocument();
    expect(findingWorkbenchReport().workbench.evidence).not.toHaveLength(0);
```

删除 `opens only a validated Trace evidence identifier` 这项只验证已移除按钮的测试。

- [ ] **Step 2: 运行测试并确认仍出现第六页签**

Run:

```bash
npm run test:unit -- tests/analysis-report.test.tsx
```

Expected: FAIL；页面仍包含“证据与指标”。

- [ ] **Step 3: 删除 Evidence 面板接线**

在 `finding-workbench.tsx`：

- 删除 `EvidenceMetricsPanel` 和 `TraceEvidenceTarget` 导入；
- 从 `regions` 删除 `["evidence", "证据与指标"]`；
- 删除 `defaultOpenEvidence`；
- 删除 `openEvidence` 属性；
- 删除 `finding-region-evidence` 面板；
- 删除文件末尾的 `TraceEvidenceTarget` 导出。

在 `analysis-report.tsx`：

- 把导入改为 `import { FindingWorkbench } from "./finding-workbench";`；
- 从 `AnalysisReportViewProps` 删除 `openEvidence`；
- 调用 `FindingWorkbench` 时不再传 `openEvidence`。

删除 `app/components/evidence-metrics-panel.tsx`。保留 `app/analyses/[id]/trace/page.tsx` 和 `trace-evidence-locator.tsx`，因为本次只移除报告中的可见入口，不改变底层证据合同或内部定位能力。

- [ ] **Step 4: 运行报告测试和合同测试**

Run:

```bash
npm run test:unit -- tests/analysis-report.test.tsx tests/perfpilot-analysis-contract.test.ts
```

Expected: PASS；五页签成立，AnalysisReport 1.3 的 Evidence 数据合同仍通过。

- [ ] **Step 5: 提交报告精简**

```bash
git add app/components/finding-workbench.tsx app/components/analysis-report.tsx app/components/evidence-metrics-panel.tsx tests/analysis-report.test.tsx
git commit -m "refactor: simplify finding report navigation"
```

### Task 4: 精简侧边栏但保留“测试”和“场景”

**Files:**
- Modify: `tests/app-shell-device.test.tsx`
- Modify: `app/components/app-shell.tsx:1-70`

- [ ] **Step 1: 写导航失败断言**

在现有 `AppShell` 渲染完成后增加：

```tsx
  const navigation = screen.getByRole("navigation", { name: "主导航" });
  expect(within(navigation).getByRole("link", { name: "测试" })).toBeVisible();
  expect(within(navigation).getByRole("link", { name: "场景" })).toBeVisible();
  expect(within(navigation).queryByRole("link", { name: "问题" })).not.toBeInTheDocument();
  expect(within(navigation).queryByRole("link", { name: "对比" })).not.toBeInTheDocument();
```

同时从 Testing Library 导入 `within`。

- [ ] **Step 2: 运行测试并确认旧入口导致失败**

Run:

```bash
npm run test:unit -- tests/app-shell-device.test.tsx
```

Expected: FAIL；“问题”和“对比”仍在主导航。

- [ ] **Step 3: 删除两个导航项及其图标类型**

在 `app-shell.tsx`：

- 删除 `CircleAlert` 和 `GitCompare` 图标导入；
- 从 `ActiveItem` 删除 `"problems" | "comparisons"`；
- 从 `navigationItems` 删除 `/problems` 和 `/comparisons` 两项；
- 保留路由文件，不把现有直达地址改成新的功能。

- [ ] **Step 4: 运行导航测试**

Run:

```bash
npm run test:unit -- tests/app-shell-device.test.tsx
```

Expected: PASS。

- [ ] **Step 5: 提交导航精简**

```bash
git add app/components/app-shell.tsx tests/app-shell-device.test.tsx
git commit -m "refactor: remove unfinished sidebar entries"
```

### Task 5: 实现真实的最近 10 份测试历史页

**Files:**
- Create: `app/components/analysis-history.tsx`
- Modify: `app/tests/page.tsx`
- Modify: `app/globals.css`
- Create: `tests/analysis-history.test.tsx`
- Modify: `tests/rendered-html.test.mjs`

- [ ] **Step 1: 写成功历史和固定提示的失败测试**

新建 `tests/analysis-history.test.tsx`，使用 `AnalysisListItem` 合同构造两条记录，并注入只实现 `analyses` 的客户端：

```tsx
// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AnalysisHistory } from "../app/components/analysis-history";
import type {
  AnalysisListItem,
  PerfPilotClient,
} from "../app/lib/perfpilot-api";

const TEAM_ID = "fe56f98a-84ef-4a7e-b6e7-83082505d5df";
const BASE_ITEM: AnalysisListItem = {
  schema_version: "1.3",
  analysis_id: "8e759ddc-4ca9-4677-831f-f8e3d8f7808a",
  team_id: TEAM_ID,
  analysis_mode: "trace_upload",
  analysis_profile: "startup",
  test_type: "cold_start",
  package_name: "com.rivotek.mediacenter",
  custom_test_name: null,
  custom_test_description: null,
  question: null,
  state: "completed",
  version: 4,
  created_at: "2026-08-21T08:00:00+00:00",
  completed_at: "2026-08-21T08:01:00+00:00",
  report_available: true,
  failure: null,
  stages: [],
  input_uploads: [],
};

function historyItem(
  overrides: Partial<AnalysisListItem>,
): AnalysisListItem {
  return { ...BASE_ITEM, ...overrides };
}

afterEach(() => cleanup());

it("shows the latest ten report-bearing analyses newest first", async () => {
  const client = {
    analyses: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      analyses: [
        historyItem({
          analysis_id: "8e759ddc-4ca9-4677-831f-f8e3d8f7808a",
          state: "partially_completed",
          test_type: "cold_start",
          package_name: "com.rivotek.mediacenter",
        }),
        historyItem({
          analysis_id: "8e759ddc-4ca9-4677-831f-f8e3d8f7808b",
          state: "completed",
          test_type: "other",
          custom_test_name: "首页连续切换",
        }),
      ],
    }),
  } as unknown as PerfPilotClient;

  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);

  expect(await screen.findAllByText("分析完成")).toHaveLength(2);
  expect(screen.getByText("冷启动")).toBeVisible();
  expect(screen.getByText("com.rivotek.mediacenter")).toBeVisible();
  expect(screen.getByText("首页连续切换")).toBeVisible();
  expect(screen.getByText(
    "历史数据仅保留最近 10 份，超过后最旧数据将自动丢弃。",
  )).toBeVisible();
  expect(client.analyses).toHaveBeenCalledWith(
    TEAM_ID,
    10,
    expect.any(AbortSignal),
  );
  expect(screen.getAllByRole("link", { name: "查看报告" })[0]).toHaveAttribute(
    "href",
    "/analyses/8e759ddc-4ca9-4677-831f-f8e3d8f7808a/report",
  );
  expect(screen.getAllByRole("link", { name: "查看报告" })[1]).toHaveAttribute(
    "href",
    "/analyses/8e759ddc-4ca9-4677-831f-f8e3d8f7808b/report",
  );
});
```

- [ ] **Step 2: 写空态、错误态和旧字段降级测试**

```tsx
it("shows an honest empty state when no successful reports exist", async () => {
  const client = {
    analyses: vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] }),
  } as unknown as PerfPilotClient;
  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);
  expect(await screen.findByText("还没有成功的测试记录")).toBeVisible();
});

it("keeps stored reports honest when optional history fields are absent", async () => {
  const client = {
    analyses: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      analyses: [historyItem({
        test_type: undefined,
        package_name: undefined,
        application_metadata: null,
        completed_at: null,
      })],
    }),
  } as unknown as PerfPilotClient;
  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);
  expect(await screen.findByText("未记录测试类型")).toBeVisible();
  expect(screen.getByText("未记录包名")).toBeVisible();
  expect(screen.getByText("未记录完成时间")).toBeVisible();
});

it("shows a retryable read error without demo history", async () => {
  const client = {
    analyses: vi.fn().mockRejectedValue(new Error("offline")),
  } as unknown as PerfPilotClient;
  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);
  expect(await screen.findByText("暂时无法读取测试历史")).toBeVisible();
  expect(screen.queryByText("com.example")).not.toBeInTheDocument();
});
```

- [ ] **Step 3: 运行新测试并确认组件不存在**

Run:

```bash
npm run test:unit -- tests/analysis-history.test.tsx
```

Expected: FAIL；无法导入 `app/components/analysis-history.tsx`。

- [ ] **Step 4: 实现历史组件的状态机和字段映射**

新组件必须使用以下固定规则：

```tsx
"use client";

const HISTORY_LIMIT = 10;
const testTypeLabels = {
  cold_start: "冷启动",
  hot_start: "热启动",
  scroll: "滑动",
  other: "其他",
} as const;

function historyTypeLabel(analysis: AnalysisListItem): string {
  if (analysis.test_type === "other" && analysis.custom_test_name?.trim()) {
    return analysis.custom_test_name.trim();
  }
  return analysis.test_type
    ? testTypeLabels[analysis.test_type]
    : "未记录测试类型";
}

function historyPackageName(analysis: AnalysisListItem): string {
  return (
    analysis.package_name?.trim()
    || analysis.application_metadata?.package_name?.trim()
    || "未记录包名"
  );
}
```

`AnalysisHistory` 接受可选的 `client` 和 `teamId` 以便测试；生产环境优先使用 `useOptionalPerfPilotSession()` 提供的团队和客户端。`useEffect` 在团队就绪后调用 `client.analyses(teamId, HISTORY_LIMIT, controller.signal)`，卸载时中止请求。

视图状态限定为：

```tsx
type HistoryView =
  | { readonly state: "loading" }
  | { readonly state: "empty" }
  | { readonly state: "error" }
  | { readonly state: "ready"; readonly analyses: readonly AnalysisListItem[] };
```

成功项统一渲染绿色“分析完成”，不向用户显示内部 `partially_completed`。时间使用 `<time dateTime={原始值}>`，无值或无效值使用明确缺省文案。

- [ ] **Step 5: 把 `/tests` 接到真实历史组件**

将 `app/tests/page.tsx` 改为：

```tsx
import { AnalysisHistory } from "../components/analysis-history";
import { AppShell } from "../components/app-shell";

export default function TestsPage() {
  return (
    <AppShell activeItem="tests">
      <AnalysisHistory />
    </AppShell>
  );
}
```

- [ ] **Step 6: 增加蓝白风格样式**

在 `app/globals.css` 的占位页样式前增加以下独立样式组，并复用现有 CSS 变量：

```css
.analysis-history-page {
  display: grid;
  gap: 18px;
}

.analysis-history-header h1 {
  margin: 0;
  color: var(--text);
  font-size: 24px;
  letter-spacing: -0.04em;
}

.analysis-history-header p {
  margin: 6px 0 0;
  color: var(--text-muted);
  font-size: 13px;
}

.analysis-history-notice,
.analysis-history-state,
.analysis-history-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-soft);
}

.analysis-history-notice {
  padding: 14px 16px;
  border-color: rgb(45 109 246 / 22%);
  background: rgb(45 109 246 / 6%);
  color: var(--primary-dark);
  font-size: 13px;
}

.analysis-history-list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.analysis-history-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 18px;
  padding: 18px 20px;
}

.analysis-history-copy {
  min-width: 0;
}

.analysis-history-heading,
.analysis-history-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
}

.analysis-history-heading h2 {
  margin: 0;
  color: var(--text);
  font-size: 16px;
}

.analysis-history-package {
  margin: 8px 0;
  color: var(--primary-dark);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.analysis-history-meta {
  color: var(--text-muted);
  font-size: 12px;
}

.analysis-history-status {
  color: #118a4e;
  font-size: 12px;
  font-weight: 750;
}

.analysis-history-link {
  align-self: center;
  color: var(--primary-dark);
  font-size: 13px;
  font-weight: 750;
  text-decoration: none;
}

.analysis-history-state {
  min-height: 220px;
  display: grid;
  place-content: center;
  gap: 6px;
  padding: 32px;
  text-align: center;
}

.analysis-history-state strong {
  color: var(--text);
}

.analysis-history-state p {
  margin: 0;
  color: var(--text-muted);
  font-size: 13px;
}

@media (max-width: 680px) {
  .analysis-history-item {
    grid-template-columns: 1fr;
  }

  .analysis-history-link {
    justify-self: start;
  }
}
```

- [ ] **Step 7: 更新匿名服务端渲染合同**

当前 `LocalLogin` 在客户端完成会话校验前会阻止受保护正文输出，因此匿名 SSR 不能断言“测试历史”正文。把 `tests/rendered-html.test.mjs` 中受保护页面断言统一为真实登录门禁：

```js
test("server-renders protected routes behind the local session gate", async () => {
  for (const path of [
    "/",
    "/tests",
    "/scenarios",
    "/problems",
    "/comparisons",
    "/analyses/analysis-live-1",
    "/analyses/analysis-live-1/report",
  ]) {
    const response = await render(path);
    assert.equal(response.status, 200);
    const html = await response.text();
    assert.match(html, /正在验证本地会话/);
    assert.doesNotMatch(html, /Acme Gallery|Pixel 8|首页启动慢/);
  }
});
```

保留元数据、404 路由和无演示数据断言。删除匿名 SSR 对“最新分析报告”“分析进度”“正在读取最终报告”等受保护正文的过期断言。

- [ ] **Step 8: 运行组件、导航和 SSR 测试**

Run:

```bash
npm run test:unit -- tests/analysis-history.test.tsx tests/app-shell-device.test.tsx
npm run build
node --test tests/rendered-html.test.mjs
```

Expected: 全部 PASS；`/tests` 构建和服务端渲染成功。

- [ ] **Step 9: 提交测试历史页**

```bash
git add app/components/analysis-history.tsx app/tests/page.tsx app/globals.css tests/analysis-history.test.tsx tests/rendered-html.test.mjs
git commit -m "feat: show recent successful analysis history"
```

### Task 6: 全量回归与后台真实验收

**Files:**
- Verify only; only fix files directly implicated by a failing check.

- [ ] **Step 1: 运行前端全量单元测试**

Run:

```bash
npm run test:unit
```

Expected: Vitest 全部 PASS。

- [ ] **Step 2: 运行前端 Lint、构建和 SSR**

Run:

```bash
npm run lint
npm run build
node --test tests/rendered-html.test.mjs
```

Expected: ESLint 无错误；构建成功；SSR 测试全部 PASS。

- [ ] **Step 3: 运行本地运行时相关 Python 测试**

Run:

```bash
PYTHONPATH=services/api/src:services/agent/src .venv/bin/python -m pytest \
  services/api/tests/unit/test_local_analysis_store.py \
  services/api/tests/unit/test_local_analysis_lifecycle.py \
  services/api/tests/unit/test_local_analysis_recovery.py \
  services/api/tests/integration/test_local_app.py \
  -q
```

Expected: 全部 PASS，无跨团队删除、持久化或生命周期回归。

- [ ] **Step 4: 静态检查计划范围内没有残留可见入口**

Run:

```bash
rg -n '证据与指标|在 Trace 中打开证据' app tests
rg -n 'label: "(问题|对比)"' app/components/app-shell.tsx
```

Expected: 第一条只允许出现在明确断言“不存在”的测试中；第二条无输出。

- [ ] **Step 5: 在后台重启本地服务并做 HTTP 验收**

使用现有重启脚本，不打开或控制用户浏览器：

```bash
npm run dev:restart
curl -fsS http://127.0.0.1:3000/tests > /tmp/perfpilot-tests.html
rg -n '正在验证本地会话' /tmp/perfpilot-tests.html
```

Expected: 服务健康，`/tests` 匿名响应遵守登录门禁。测试历史正文和固定提示已经由 `analysis-history.test.tsx` 验证，真实列表由下一步登录后的 API 请求验证。

- [ ] **Step 6: 使用 `ray_wu` 的现有认证在后台完成一份真实 Trace 分析**

使用已有本地 Trace、环境变量中的认证信息和 HTTP API，不把密码写入仓库、日志或命令历史。确认：

```text
POST analysis -> upload trace -> finalize -> poll report -> GET history(limit=10)
```

Expected:

- 新报告可以打开；
- 报告中的 Finding 仍引用服务端验证的 Evidence；
- 历史接口包含新报告且最多返回 10 份；
- 页面把 `completed` 或带可读报告的 `partially_completed` 统一显示为绿色“分析完成”；
- 不操作用户当前可见的主界面或已有浏览器会话。

- [ ] **Step 7: 检查工作区并提交必要的验证修复**

Run:

```bash
git diff --check
git status --short
git log --oneline -6
```

Expected: 无空白错误；仅 `.venv` 保持未跟踪；所有实现变更已经按任务提交。若验证暴露实现缺陷，先增加失败测试，再提交对应修复，不提交运行时数据或凭据。
