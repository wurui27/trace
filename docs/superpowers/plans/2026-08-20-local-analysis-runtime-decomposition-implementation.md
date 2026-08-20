# 本地分析运行时渐进拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在可靠性合同保持不变的前提下，将分析投影、恢复、远程采集和阶段执行从超大文件中拆出，降低修改冲突和状态回归风险。

**Architecture:** 使用 characterization tests 锁定第一份计划交付的 1.3 行为，再按单一职责逐个提取纯函数和协调器。每次迁移只改变 import/composition，不改变 API、持久化文档、任务签名、错误代码或用户文案。

**Tech Stack:** Python 3.12、FastAPI、asyncio、Pydantic 2、React 19、TypeScript 5、pytest、Vitest。

**Prerequisite:** 完成 `2026-08-20-analysis-lifecycle-reliability-implementation.md`，并确保其完整门禁通过。

---

## 文件结构

- `services/api/src/perfpilot_api/local_analysis_projection.py`：持久化文档和公开 1.0–1.3 响应投影。
- `services/api/src/perfpilot_api/local_analysis_recovery.py`：普通重启恢复决策和 recovery actions。
- `services/api/src/perfpilot_api/local_remote_capture.py`：远程采集发布、manifest 恢复和取消协调。
- `services/api/src/perfpilot_api/local_stage_execution.py`：SmartPerfetto、source、AI 和 report 的阶段执行器。
- `services/api/src/perfpilot_api/local_app.py`：只保留 composition、路由和薄运行时入口。
- `app/lib/perfpilot-analysis-api.ts`：Analysis 合同类型与 parser。
- `app/lib/perfpilot-api.ts`：HTTP client 和非 Analysis 合同。
- 新模块各自有对应单元测试；现有 acceptance tests 继续作为行为门禁。

### Task 1: 建立拆分前 characterization 门禁

**Files:**
- Create: `services/api/tests/unit/test_local_runtime_characterization.py`
- Create: `tests/perfpilot-analysis-contract.test.ts`

- [ ] **Step 1: 固定 Python 行为快照**

使用公开 helper 创建 trace、script device、remote device、source strong 和 source mismatch 五类分析，断言：

- 公开响应 canonical JSON bytes；
- persisted state canonical JSON bytes；
- report 可见与终态原子；
- cancel、failure 和 generation 错误代码；
- restart recovery actions 顺序；
- 任何输出不含临时路径和 token。

fixture 存放在测试代码中的 Python dict，不新增容易漂移的大型 golden 文件。

- [ ] **Step 2: 固定 TypeScript parser 行为**

将现有 1.0–1.3 合法和非法 payload 表格化。断言 parse 后对象、错误代码和 exact-key 拒绝行为。

- [ ] **Step 3: 运行 characterization**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_runtime_characterization.py
npx vitest run tests/perfpilot-analysis-contract.test.ts
```

Expected: 全部 PASS；这些测试记录当前已批准行为，不需要生产改动。

- [ ] **Step 4: 提交测试门禁**

```bash
git add services/api/tests/unit/test_local_runtime_characterization.py \
  tests/perfpilot-analysis-contract.test.ts
git commit -m "test: characterize local analysis runtime"
```

### Task 2: 提取分析持久化与公开投影

**Files:**
- Create: `services/api/src/perfpilot_api/local_analysis_projection.py`
- Create: `services/api/tests/unit/test_local_analysis_projection.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/unit/test_local_runtime_characterization.py`

- [ ] **Step 1: 写纯投影 RED**

定义输入 dataclass `LocalAnalysisView`，测试：

- `to_persisted_document` 生成闭合 state document；
- `from_persisted_document` 迁移旧 activity；
- `to_public_document` 按 1.0–1.3 分支生成响应；
- source weak/mismatch 不发布路径、symbol 或 Diff；
- runtime status 与主状态一致；
- unknown persisted keys 拒绝。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_projection.py
```

Expected: collection FAIL，`local_analysis_projection` 不存在。

- [ ] **Step 3: 实现纯投影 API**

模块只导出：

```python
@dataclass(frozen=True, slots=True)
class LocalAnalysisView:
    analysis_id: UUID
    team_id: UUID
    schema_version: str
    state: str
    version: int
    generation: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    report_available: bool
    runtime_status: Mapping[str, object]
    payload: Mapping[str, object]


def to_persisted_document(value: LocalAnalysisView) -> dict[str, object]:
    return validate_persisted_analysis_document({
        **value.payload,
        "analysis_id": str(value.analysis_id),
        "team_id": str(value.team_id),
        "state": value.state,
        "version": value.version,
        "generation": value.generation,
        "runtime_status": dict(value.runtime_status),
    })


def to_public_document(value: LocalAnalysisView) -> dict[str, object]:
    return validate_analysis_response_document(project_public_analysis(value))
```

`validate_*` 和 `project_public_analysis` 是本模块私有具体函数，使用当前 exact-key sets。它不访问磁盘、网络、Agent 或 event loop。

- [ ] **Step 4: 替换原调用点**

`local_app.py` 保留 `_LocalAnalysis`，增加一个转换方法生成 `LocalAnalysisView`。删除重复的 exact-key projection 和 restore parsing；路由调用新模块。

- [ ] **Step 5: 运行门禁**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_projection.py \
  services/api/tests/unit/test_local_runtime_characterization.py \
  services/api/tests/integration/test_local_app.py \
  -k 'analysis or report or source or runtime_status'
```

Expected: 全部 PASS，characterization bytes 不变。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_analysis_projection.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_analysis_projection.py \
  services/api/tests/unit/test_local_runtime_characterization.py
git commit -m "refactor: extract local analysis projection"
```

### Task 3: 提取普通重启恢复决策

**Files:**
- Create: `services/api/src/perfpilot_api/local_analysis_recovery.py`
- Create: `services/api/tests/unit/test_local_analysis_recovery.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写 recovery action RED**

纯函数输入 persisted analysis summary 和 artifact presence，输出有序 action：

```python
def test_completed_smartperfetto_resumes_synthesis_without_recapture() -> None:
    actions = plan_recovery(
        RecoverySnapshot(
            state="analyzing",
            canceled=False,
            smartperfetto_state="completed",
            evidence_manifest_present=True,
            report_present=False,
            source_state="available",
            remote_publication="published",
        )
    )
    assert actions == (RecoveryAction.RESUME_SYNTHESIS,)
```

覆盖：

- cancel marker -> CLOSE_CANCELED；
- publishing/published 且无 manifest -> RECONCILE_PUBLICATION；
- accepted manifest + SmartPerfetto running -> RESUME_REMOTE_ANALYSIS；
- SmartPerfetto completed + evidence -> RESUME_SYNTHESIS；
- report present + active state -> CLOSE_COMPLETED；
- invalid identity/artifact -> FAIL_INVALID_RECOVERY；
- terminal -> NOOP。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_recovery.py
```

Expected: collection FAIL。

- [ ] **Step 3: 实现纯恢复规划器**

定义 `RecoverySnapshot`、`RecoveryAction` enum 和 `plan_recovery`。函数无 I/O，不创建 asyncio task。相同输入必须返回相同 action 顺序。

- [ ] **Step 4: 将 I/O 留在薄适配器**

`_LocalRuntime.start`：

1. 读取并校验 persisted documents；
2. 构造 `RecoverySnapshot`；
3. 调 `plan_recovery`；
4. 按 action 调现有 artifact/task/gateway 方法；
5. 每个 action 完成后持久化；
6. 失败交给 DurableTaskSupervisor，不写无限循环。

- [ ] **Step 5: 运行重启矩阵**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_recovery.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_analysis_reliability.py \
  -k 'restart or resume or recovery'
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_analysis_recovery.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_analysis_recovery.py \
  services/api/tests/integration/test_local_app.py
git commit -m "refactor: extract analysis recovery planning"
```

### Task 4: 提取远程采集发布与恢复

**Files:**
- Create: `services/api/src/perfpilot_api/local_remote_capture.py`
- Create: `services/api/tests/unit/test_local_remote_capture_runtime.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/tests/acceptance/test_remote_agent_capture.py`

- [ ] **Step 1: 写 publication coordinator RED**

测试 `RemoteCaptureCoordinator`：

- 同分析 concurrent finalize 只 inspect 一次；
- intent 持久化前失败不注册 input/task；
- intent 持久化后 enqueue 失败可用相同 metadata 重试；
- enqueue 后 final persist 失败重启可重建同一 task；
- cancel during inspect 和 cancel after enqueue 都不留下 ghost task；
- different analyses 并行；
- completion manifest exact binding。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_remote_capture_runtime.py
```

Expected: collection FAIL。

- [ ] **Step 3: 实现 coordinator**

构造函数显式注入：

```python
@dataclass(frozen=True, slots=True)
class RemoteCaptureDependencies:
    inspector: LocalApkInspector
    tasks: AgentTaskService
    artifacts: LocalAgentArtifactService
    persist: Callable[[LocalAnalysisView], Awaitable[None]]
    transition: Callable[..., Awaitable[None]]


class RemoteCaptureCoordinator:
    def __init__(self, dependencies: RemoteCaptureDependencies) -> None:
        self._dependencies = dependencies
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
```

公开方法只有 `finalize`、`reconcile`、`cancel` 和 `accept_completion`。每个方法持有 team+analysis lock，提交前重新检查 cancel/generation。安全 AAPT2、artifact service 和 task service 保持原模块，不复制实现。

- [ ] **Step 4: 替换 local_app 方法**

路由仍在 `local_app.py`，只调用 coordinator。删除原 `_finalize_remote_device`、`_publish_remote_device`、`_retry_remote_publication`、`_restore_remote_capture` 和对应 lock map。

- [ ] **Step 5: 运行远程采集全套**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_remote_capture_runtime.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  agents/device-agent/tests
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_remote_capture.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_remote_capture_runtime.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_remote_agent_capture.py
git commit -m "refactor: extract remote capture coordinator"
```

### Task 5: 提取 SmartPerfetto、源码、AI 和报告阶段

**Files:**
- Create: `services/api/src/perfpilot_api/local_stage_execution.py`
- Create: `services/api/tests/unit/test_local_stage_execution.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`

- [ ] **Step 1: 写 executor 组合 RED**

定义统一结果：

```python
@dataclass(frozen=True, slots=True)
class StageResult:
    state: Literal["completed", "degraded", "failed", "canceled"]
    evidence: Mapping[str, object] | None
    failure_code: str | None
    progress_summary: str
```

测试：

- SmartPerfetto startup 失败、scroll 成功返回 degraded 且保留 scroll；
- source mismatch 返回 degraded，不丢 Trace evidence；
- AI invalid 返回 failed，但原始 SmartPerfetto/report core 保留；
- cancel marker 在每个 I/O 提交前中止；
- old generation result 拒绝；
- report publish 使用已验证 projection，不重新调用 provider；
- executor 不直接修改主分析状态。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_stage_execution.py
```

Expected: collection FAIL。

- [ ] **Step 3: 实现四个 focused executor**

模块定义：

- `execute_smartperfetto_stage`；
- `execute_source_stage`；
- `execute_ai_stage`；
- `execute_report_stage`。

每个函数显式接收 gateway/service、immutable input、progress callback、cancel/generation guard。它们返回 `StageResult`；`AnalysisLifecycleCoordinator` 决定下一阶段和终态。

SmartPerfetto/source/AI 不传 total timeout，只由 progress callback 更新 supervisor。报告 stage 保留 30 秒配置化 deadline。

- [ ] **Step 4: 替换原流程**

`_execute_run` 和 `_execute_remote_capture` 变成顺序协调：

```python
trace = await execute_smartperfetto_stage(context.smartperfetto)
source = await execute_source_stage(context.source, trace.evidence)
ai = await execute_ai_stage(context.ai, trace.evidence, source.evidence)
report = await execute_report_stage(context.report, trace, source, ai)
await context.lifecycle.close_with_report(report, generation=context.generation)
```

每步后持久化 evidence/activity。cancel/generation guard 是构造 context 时注入的具体函数。

- [ ] **Step 5: 运行阶段和 acceptance**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_stage_execution.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_analysis_reliability.py \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_stage_execution.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_stage_execution.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py
git commit -m "refactor: extract local analysis stages"
```

### Task 6: 拆分前端 Analysis 合同

**Files:**
- Create: `app/lib/perfpilot-analysis-api.ts`
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `tests/perfpilot-analysis-contract.test.ts`
- Modify: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: 写 import-surface RED**

测试组件和 client 可以从原 `perfpilot-api.ts` 继续导入 `AnalysisResponse`，同时新模块直接导出：

- Analysis types；
- `parseAnalysisResponse`；
- `parseAnalysisListResponse`；
- `analysisIsTerminal`。

非法 fixtures 的错误代码和消息保持不变。

- [ ] **Step 2: 运行 RED**

Run:

```bash
npx vitest run \
  tests/perfpilot-analysis-contract.test.ts \
  tests/perfpilot-api.test.ts
```

Expected: FAIL，新模块不存在。

- [ ] **Step 3: 移动完整 Analysis 合同**

把 Analysis 专属常量、interfaces、validators 和 parser 移到新文件。新文件不 import HTTP client，避免循环依赖。`perfpilot-api.ts`：

```typescript
export {
  analysisIsTerminal,
  parseAnalysisListResponse,
  parseAnalysisResponse,
} from "./perfpilot-analysis-api";
export type {
  AnalysisResponse,
  AnalysisRuntimeStatus,
  AnalysisStage,
  AnalysisState,
} from "./perfpilot-analysis-api";
```

HTTP 方法内部 import parser。其他组件无需批量改 import。

- [ ] **Step 4: 运行前端全套**

Run:

```bash
npx vitest run \
  tests/perfpilot-analysis-contract.test.ts \
  tests/perfpilot-api.test.ts \
  tests/analysis-progress.test.tsx \
  tests/dashboard-analysis-coordinator.test.tsx
npm run lint
npm run build
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/lib/perfpilot-analysis-api.ts \
  app/lib/perfpilot-api.ts \
  tests/perfpilot-analysis-contract.test.ts \
  tests/perfpilot-api.test.ts
git commit -m "refactor: isolate analysis API contracts"
```

### Task 7: 完成结构和行为门禁

**Files:**
- Modify: `services/api/tests/unit/test_local_runtime_characterization.py`
- Modify: `services/api/tests/acceptance/test_analysis_reliability.py`
- Modify: `README.md`

- [ ] **Step 1: 增加依赖方向测试**

使用 AST/import 检查：

- projection/recovery 模块不 import `local_app`；
- lifecycle/supervisor 不 import FastAPI；
- stage executors 不 import route modules；
- frontend analysis parser 不 import HTTP client；
- `local_app.py` 只 composition 并调用公开模块。

- [ ] **Step 2: 运行结构 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_runtime_characterization.py \
  -k 'dependency_direction or module_boundaries'
```

Expected: 在尚存反向依赖时 FAIL。

- [ ] **Step 3: 删除迁移后死代码**

使用 `rg` 确认旧 private helpers 无调用，再用 `apply_patch` 删除。不得删除 legacy 1.0–1.2 parser、persisted migration 或 error code。

运行：

```bash
rg -n '_retry_remote_publication|_restore_remote_capture|_analysis_document|analysisStillChanging' \
  services/api/src/perfpilot_api/local_app.py app/lib/perfpilot-api.ts
```

Expected: 只保留新模块 import/call；不保留重复实现。

- [ ] **Step 4: 运行最终门禁**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q services/api/tests agents/device-agent/tests

npx vitest run
.venv/bin/ruff check services/api/src services/api/tests \
  agents/device-agent/src agents/device-agent/tests
npm run lint
npm run build
git diff --check
```

Expected: 全部 PASS；只允许既有外部环境测试按标记 skip。

- [ ] **Step 5: 检查文件规模和职责**

Run:

```bash
wc -l \
  services/api/src/perfpilot_api/local_app.py \
  services/api/src/perfpilot_api/local_analysis_projection.py \
  services/api/src/perfpilot_api/local_analysis_recovery.py \
  services/api/src/perfpilot_api/local_remote_capture.py \
  services/api/src/perfpilot_api/local_stage_execution.py \
  app/lib/perfpilot-api.ts \
  app/lib/perfpilot-analysis-api.ts
```

Expected:

- 每个新 Python 模块少于 1,200 行；
- `local_app.py` 比拆分前至少减少 1,500 行；
- `perfpilot-analysis-api.ts` 少于 1,200 行；
- 没有模块同时负责路由、I/O、状态迁移和序列化。

- [ ] **Step 6: 更新 README 并提交**

README 增加模块职责表和“修改分析流程时应改哪个模块”。

```bash
git add services/api/tests/unit/test_local_runtime_characterization.py \
  services/api/tests/acceptance/test_analysis_reliability.py \
  README.md
git commit -m "docs: explain local analysis runtime boundaries"
```

## 完成定义

- 所有公开 API、错误代码、持久化文档和 UI 文案保持兼容。
- `local_app.py` 只保留 composition、路由和薄入口。
- 投影、恢复、远程采集和阶段执行可以独立理解与测试。
- 前端 Analysis parser 与通用 HTTP client 分离。
- 可靠性 acceptance 在每次拆分后持续通过。
- 没有重复实现、反向依赖或未使用迁移代码。
