# 分析生命周期可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立服务端唯一分析状态权威、可靠取消、活动检测、普通重启恢复和分层健康投影，消除无法解释的等待、状态冲突与迟到发布。

**Architecture:** 新增纯状态门禁 `local_analysis_lifecycle.py` 和有界协调器 `local_task_supervisor.py`，由现有本地运行时逐步调用。Analysis response 新增严格 1.3 分支，旧 1.0–1.2 保持精确兼容；前端只展示服务端返回的 current stage 和 activity，不再自行推断主状态。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、asyncio、JSON Schema 2020-12、React 19、TypeScript 5、Vitest、pytest。

---

## 文件结构

- `services/api/src/perfpilot_api/local_analysis_lifecycle.py`：纯状态迁移、generation、取消和报告发布不变量。
- `services/api/src/perfpilot_api/local_task_supervisor.py`：活动分类、有界控制重试和重启协调策略。
- `services/api/src/perfpilot_api/local_analysis_health.py`：团队私有依赖健康投影。
- `services/api/src/perfpilot_api/local_app.py`：将现有运行时接入上述边界，不再直接绕过门禁。
- `services/api/src/perfpilot_api/local_analysis_store.py`：持久化 1.3 lifecycle/activity 文档。
- `services/api/src/perfpilot_api/api/health.py`：保留 liveness，增加安全 readiness。
- `contracts/v1/analyses/analysis-response.schema.json`：闭合 1.3 响应。
- `contracts/v1/examples/analysis-response-v1.3.valid.json`：合同示例。
- `app/lib/perfpilot-api.ts`：严格 1.3 类型和 validator。
- `app/components/analysis-progress.tsx`：服务端权威进度与取消反馈。
- `app/components/dashboard.tsx`：按服务端状态刷新当前分析。
- `app/components/system-health-banner.tsx`：团队健康摘要。
- 对应 `services/api/tests` 和 `tests` 下的单元、合同、集成与验收测试。

### Task 1: 建立纯生命周期门禁

**Files:**
- Create: `services/api/src/perfpilot_api/local_analysis_lifecycle.py`
- Create: `services/api/tests/unit/test_local_analysis_lifecycle.py`

- [ ] **Step 1: 写终态、取消和 generation RED**

测试以下不变量：

```python
from datetime import UTC, datetime
from uuid import UUID

import pytest

from perfpilot_api.local_analysis_lifecycle import (
    AnalysisLifecycleError,
    LifecycleSnapshot,
    apply_transition,
)


ANALYSIS_ID = UUID("70000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 20, tzinfo=UTC)


def snapshot(
    state: str = "analyzing",
    *,
    generation: int = 1,
    canceled: bool = False,
    report_available: bool = False,
) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        analysis_id=ANALYSIS_ID,
        state=state,
        generation=generation,
        cancel_requested_at=NOW if canceled else None,
        report_available=report_available,
    )


def test_terminal_analysis_cannot_return_to_active_state() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(snapshot("completed", report_available=True), target="analyzing", now=NOW)


def test_cancel_marker_rejects_new_work_and_late_report() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        apply_transition(
            snapshot(canceled=True),
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )


def test_old_generation_cannot_publish() -> None:
    with pytest.raises(AnalysisLifecycleError, match="analysis generation rejected"):
        apply_transition(
            snapshot(generation=2),
            target="completed",
            now=NOW,
            result_generation=1,
            publish_report=True,
        )


def test_report_and_terminal_state_close_together() -> None:
    result = apply_transition(
        snapshot(),
        target="completed",
        now=NOW,
        result_generation=1,
        publish_report=True,
    )
    assert result.state == "completed"
    assert result.report_available is True
    assert result.completed_at == NOW
```

再加重复 `completed`、重复 `canceled` 幂等、`creating -> analyzing` 非法跳转和 `report_available=True` 但非终态拒绝测试。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_lifecycle.py
```

Expected: collection FAIL，`perfpilot_api.local_analysis_lifecycle` 不存在。

- [ ] **Step 3: 实现状态模型和门禁**

实现以下闭合核心：

```python
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

AnalysisState = Literal[
    "creating", "created", "uploading", "queued", "scheduled", "running",
    "analyzing", "completed", "partially_completed", "failed", "canceled", "deleted",
]
_ACTIVE = frozenset({"creating", "created", "uploading", "queued", "scheduled", "running", "analyzing"})
_TERMINAL = frozenset({"completed", "partially_completed", "failed", "canceled", "deleted"})
_ALLOWED = {
    "creating": frozenset({"created", "failed", "canceled"}),
    "created": frozenset({"uploading", "queued", "failed", "canceled"}),
    "uploading": frozenset({"queued", "failed", "canceled"}),
    "queued": frozenset({"scheduled", "running", "analyzing", "failed", "canceled"}),
    "scheduled": frozenset({"running", "analyzing", "failed", "canceled"}),
    "running": frozenset({"analyzing", "completed", "partially_completed", "failed", "canceled"}),
    "analyzing": frozenset({"completed", "partially_completed", "failed", "canceled"}),
}


class AnalysisLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    analysis_id: UUID
    state: AnalysisState
    generation: int
    cancel_requested_at: datetime | None
    report_available: bool
    completed_at: datetime | None = None


def apply_transition(
    current: LifecycleSnapshot,
    *,
    target: AnalysisState,
    now: datetime,
    result_generation: int | None = None,
    publish_report: bool = False,
) -> LifecycleSnapshot:
    if result_generation is not None and result_generation != current.generation:
        raise AnalysisLifecycleError("analysis generation rejected")
    if current.state in _TERMINAL:
        if target == current.state and (not publish_report or current.report_available):
            return current
        raise AnalysisLifecycleError("analysis transition rejected")
    if current.cancel_requested_at is not None and target != "canceled":
        raise AnalysisLifecycleError("analysis transition rejected")
    if target != current.state and target not in _ALLOWED[current.state]:
        raise AnalysisLifecycleError("analysis transition rejected")
    if publish_report and target not in {"completed", "partially_completed"}:
        raise AnalysisLifecycleError("analysis transition rejected")
    report_available = current.report_available or publish_report
    if report_available and target in _ACTIVE:
        raise AnalysisLifecycleError("analysis transition rejected")
    return replace(
        current,
        state=target,
        report_available=report_available,
        completed_at=now if target in _TERMINAL else current.completed_at,
    )


def request_cancel(current: LifecycleSnapshot, *, now: datetime) -> LifecycleSnapshot:
    if current.state in _TERMINAL or current.cancel_requested_at is not None:
        return current
    return replace(current, cancel_requested_at=now)


class AnalysisLifecycleCoordinator:
    def transition(
        self,
        current: LifecycleSnapshot,
        *,
        target: AnalysisState,
        now: datetime,
        result_generation: int | None = None,
        publish_report: bool = False,
    ) -> LifecycleSnapshot:
        return apply_transition(
            current,
            target=target,
            now=now,
            result_generation=result_generation,
            publish_report=publish_report,
        )

    def cancel(self, current: LifecycleSnapshot, *, now: datetime) -> LifecycleSnapshot:
        return request_cancel(current, now=now)
```

`AnalysisLifecycleCoordinator` 是运行时唯一使用的门禁；纯函数保留给单元测试和恢复规划器。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_lifecycle.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/src/perfpilot_api/local_analysis_lifecycle.py \
  services/api/tests/unit/test_local_analysis_lifecycle.py
git commit -m "feat: guard local analysis lifecycle"
```

### Task 2: 持久化阶段活动并增加 Analysis 1.3 合同

**Files:**
- Modify: `services/api/src/perfpilot_api/local_analysis_store.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `contracts/v1/analyses/create-request.schema.json`
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Create: `contracts/v1/examples/analysis-create-v1.3.valid.json`
- Create: `contracts/v1/examples/analysis-response-v1.3.valid.json`
- Modify: `services/api/tests/unit/test_local_analysis_store.py`
- Modify: `services/api/tests/unit/test_analysis_contracts.py`
- Modify: `services/api/tests/contract/test_contract_examples.py`

- [ ] **Step 1: 写 1.3 合同和迁移 RED**

新增有效示例，`runtime_status` 精确包含：

```json
{
  "current_stage": "source_code",
  "stage_state": "running",
  "started_at": "2026-08-20T08:00:00Z",
  "updated_at": "2026-08-20T08:06:24Z",
  "last_progress_at": "2026-08-20T08:06:12Z",
  "attempt": 1,
  "max_attempts": 2,
  "generation": 1,
  "waiting_for": null,
  "progress_summary": "已读取 1247 个文件，找到 18 个相关源码片段",
  "available_actions": ["cancel"]
}
```

测试要求：

- 1.3 必须有 `runtime_status`；
- unknown stage/state/action 和额外字段拒绝；
- 时间必须为 UTC date-time，attempt/generation 必须为正整数；
- progress summary 最长 240 字；
- 1.0–1.2 示例继续通过且禁止 1.3 字段；
- 旧 persisted state 缺 activity 时迁移成当前阶段、`last_progress_at=updated_at`；
- 持久化未知 activity 字段拒绝。
- 1.3 create request 支持现有 trace、remote device 和 script device 字段；旧 1.0–1.2 request/response 保持原形。

- [ ] **Step 2: 运行合同 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_store.py \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/contract/test_contract_examples.py \
  -k 'runtime_status or schema_1_3 or lifecycle_activity'
```

Expected: FAIL，schema 不接受 1.3 或缺少 runtime status 投影。

- [ ] **Step 3: 扩展持久化模型**

在 `_LocalAnalysis` 增加 `runtime_status: dict[str, object]`，默认值由当前 state 生成。把 `runtime_status` 加入 `_PERSISTED_ANALYSIS_KEYS` 和 exact-key validator。

增加单一更新函数：

```python
def _record_progress(
    analysis: _LocalAnalysis,
    *,
    stage: str,
    state: str,
    summary: str,
    waiting_for: str | None,
    now: datetime,
) -> None:
    if len(summary) > 240:
        raise ValueError("analysis progress rejected")
    previous = analysis.runtime_status
    started_at = (
        previous.get("started_at")
        if previous.get("current_stage") == stage
        else now.isoformat()
    )
    analysis.runtime_status = {
        "current_stage": stage,
        "stage_state": state,
        "started_at": started_at,
        "updated_at": now.isoformat(),
        "last_progress_at": now.isoformat(),
        "attempt": int(previous.get("attempt", 1)),
        "max_attempts": int(previous.get("max_attempts", 2)),
        "generation": analysis.generation,
        "waiting_for": waiting_for,
        "progress_summary": summary,
        "available_actions": ["cancel"] if analysis.state not in _TERMINAL_ANALYSIS_STATES else [],
    }
```

所有文档写入继续经过 `LocalAnalysisStore.save_state(team_id, analysis_id, document)` 的原子 replace/fsync 边界。

- [ ] **Step 4: 增加 schema 1.3 分支**

在 create 和 response schema 中：

- create request 顶层 version enum 加 `1.3`，并用既有 analysis mode 分支闭合允许字段；
- response 顶层 version enum 加 `1.3`；
- 1.3 required 加 `runtime_status`；
- `runtimeStatus` 使用 `additionalProperties: false`；
- 1.0–1.2 分支明确禁止 `runtime_status`；
- 1.3 的 mode/source/capture 分支继承 1.2 兼容形状，不改变 create request 合同。

新前端创建请求发送 1.3，服务端为这些分析返回 1.3。旧客户端继续发送 1.0–1.2，并获得原版本响应；旧持久化分析不改变公开 schema version。内部 activity migration 不强制升级旧公开合同。

- [ ] **Step 5: 运行合同和 store 测试**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_store.py \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/contract/test_contract_examples.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_analysis_store.py \
  services/api/src/perfpilot_api/local_app.py \
  contracts/v1/analyses/create-request.schema.json \
  contracts/v1/analyses/analysis-response.schema.json \
  contracts/v1/examples/analysis-create-v1.3.valid.json \
  contracts/v1/examples/analysis-response-v1.3.valid.json \
  services/api/tests/unit/test_local_analysis_store.py \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/contract/test_contract_examples.py
git commit -m "feat: persist analysis stage activity"
```

### Task 3: 将所有终态写入接入生命周期门禁

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`

- [ ] **Step 1: 写状态冲突 RED**

增加四组并发测试：

- 报告写入成功后，主状态和 report stage 同时 completed；
- completion 到达后再 cancel，只返回既有终态；
- cancel 先提交后，迟到 Agent completion 被拒绝；
- AI generation 1 迟到时，generation 2 报告不被覆盖。

关键断言：

```python
assert terminal["state"] == "completed"
assert terminal["report_available"] is True
assert next(stage for stage in terminal["stages"] if stage["stage"] == "report")["state"] == "completed"

assert canceled["state"] == "canceled"
assert client.get(report_url).status_code == 404
assert persisted["generation"] == 2
assert persisted_report["report_version"] == 2
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  -k 'terminal_state_is_atomic or late_completion or stale_generation'
```

Expected: 至少一个测试显示报告和主状态分两次提交，或迟到结果仍可修改分析。

- [ ] **Step 3: 增加运行时适配器**

在 `_LocalRuntime` 增加：

```python
async def _transition(
    self,
    analysis: _LocalAnalysis,
    *,
    target: AnalysisState,
    now: datetime,
    result_generation: int | None = None,
    publish_report: bool = False,
) -> None:
    current = LifecycleSnapshot(
        analysis_id=analysis.analysis_id,
        state=analysis.state,
        generation=analysis.generation,
        cancel_requested_at=analysis.cancel_requested_at,
        report_available=analysis.report is not None,
        completed_at=analysis.completed_at,
    )
    updated = self.lifecycle.transition(
        current,
        target=target,
        now=now,
        result_generation=result_generation,
        publish_report=publish_report,
    )
    analysis.state = updated.state
    analysis.completed_at = updated.completed_at
```

`_LocalRuntime` 在构造时创建一个 `AnalysisLifecycleCoordinator`，所有终态、取消和报告关闭都使用该实例。

将 completion、failure、cancel、AI rerun 和 report publish 的直接状态赋值改为调用 `_transition`。报告发布必须先准备临时文档，再在分析锁内检查 generation/cancel，写 report 和 state；若 state 持久化发生 committed durability error，按现有 committed 信号保留一致的新状态。

- [ ] **Step 4: 运行相关回归**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  -k 'cancel or completion or report or generation or restart'
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/src/perfpilot_api/local_app.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py
git commit -m "fix: serialize analysis terminal state"
```

### Task 4: 统一所有阶段的取消语义

**Files:**
- Create: `services/api/src/perfpilot_api/local_cancellation.py`
- Create: `services/api/tests/unit/test_local_cancellation.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `app/components/dashboard.tsx`
- Modify: `tests/dashboard-analysis-coordinator.test.tsx`

- [ ] **Step 1: 写 coordinator RED**

测试三类目标：成功确认、异常、无响应。取消协调器必须先关闭本地权威状态，再在 10 秒 cleanup budget 内尽力取消远端；超时不恢复用户状态。

```python
async def test_cancel_closes_locally_when_remote_never_acknowledges() -> None:
    target = BlockingCancellationTarget()
    result = await cancel_targets(
        (target,),
        timeout_seconds=0.01,
    )
    assert result.accepted is True
    assert result.pending_cleanup == ("agent_capture",)
    assert target.started is True
```

另测 CancelledError 不吞、重复取消幂等、一个目标失败不阻塞其他目标。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_cancellation.py
```

Expected: collection FAIL，`local_cancellation` 不存在。

- [ ] **Step 3: 实现 bounded cancellation**

`local_cancellation.py` 定义 `CancellationTarget(name, cancel)` 和：

```python
async def cancel_targets(
    targets: tuple[CancellationTarget, ...],
    *,
    timeout_seconds: float = 10.0,
) -> CancellationResult:
    tasks = {asyncio.create_task(item.cancel()): item.name for item in targets}
    done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    failed = tuple(
        tasks[task]
        for task in done
        if not task.cancelled() and task.exception() is not None
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return CancellationResult(
        accepted=True,
        failed=tuple(sorted(failed)),
        pending_cleanup=tuple(sorted(tasks[task] for task in pending)),
    )
```

`_LocalRuntime.cancel` 顺序固定为：

1. 在分析锁内调用 `request_cancel`；
2. 持久化；
3. 返回路由可序列化 snapshot；
4. 后台调用 `cancel_targets`；
5. 撤销 Agent/source lease 和 artifact grants；
6. 再持久化 cleanup 摘要，但不修改 canceled 终态。

API 响应中 `runtime_status.stage_state` 立即为 `canceled` 或 `cancel_requested`，`available_actions=[]`。

- [ ] **Step 4: 修正前端取消**

`dashboard.tsx` 首次点击后设置本地 `cancelPending`，按钮 disabled，文案“正在取消”；成功响应立即使用服务端 snapshot。请求失败才恢复按钮并显示稳定错误。不要通过 3 秒 timer 猜测远端已经停止。

- [ ] **Step 5: 运行取消回归**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_cancellation.py \
  services/api/tests/integration/test_local_app.py \
  agents/device-agent/tests/integration/test_cancellation.py \
  -k 'cancel'

npx vitest run tests/dashboard-analysis-coordinator.test.tsx
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_cancellation.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_cancellation.py \
  services/api/tests/integration/test_local_app.py \
  app/components/dashboard.tsx tests/dashboard-analysis-coordinator.test.tsx
git commit -m "fix: coordinate analysis cancellation"
```

### Task 5: 增加活动检测和有界控制重试

**Files:**
- Create: `services/api/src/perfpilot_api/local_task_supervisor.py`
- Create: `services/api/tests/unit/test_local_task_supervisor.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写策略 RED**

定义并测试策略：

```python
SMARTPERFETTO_POLICY = StagePolicy(
    total_timeout_seconds=None,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
SOURCE_POLICY = StagePolicy(
    total_timeout_seconds=None,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
AI_POLICY = StagePolicy(
    total_timeout_seconds=None,
    idle_warning_seconds=180,
    idle_escalation_seconds=600,
    control_retry_attempts=2,
)
```

测试：

- 2 分钟无活动仍 active；
- 3 分钟进入 slow warning，不失败；
- 10 分钟进入 waiting_for_upstream，不失败；
- 新 heartbeat/progress 清除 warning；
- device claim 超过配置 deadline 进入 failed；
- 控制调用最多执行 2 次；
- activity reconciliation 查询同一 upstream run，不创建新 run。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_task_supervisor.py
```

Expected: collection FAIL，`local_task_supervisor` 不存在。

- [ ] **Step 3: 实现纯策略和 supervisor**

实现 `StagePolicy`、`ActivityState`、`classify_activity` 和 `run_control_operation`。`run_control_operation` 接收 idempotency key，捕获明确 retryable 类型，只执行 `control_retry_attempts` 次。

`DurableTaskSupervisor.tick(now)`：

- 读取活动分析；
- 根据 current stage 和 `last_progress_at` 分类；
- 对 SmartPerfetto/source/AI 只查询原任务；
- 更新 `waiting_for` 和提示；
- 对设备 claim/capture/report publish 执行配置化 deadline；
- 每次更新通过 lifecycle coordinator 和 store；
- 如果发现 cancel marker，交给 Cancellation Coordinator。

删除 `_retry_agent_cancellation` 和 `_retry_remote_publication` 中的 `while True`。改为 supervisor 的下一次 tick 重试，持久化 attempt 和 next retry time。

- [ ] **Step 4: 写重启和无重复集成测试**

测试普通重启发生在：

- Agent 已领取但未 completion；
- SmartPerfetto 已提交；
- source completion 已保存；
- AI 已返回但报告未发布。

重启后断言同一 task/run/generation 被恢复，submit/install/capture/provider 调用次数不增加。

- [ ] **Step 5: 运行 supervisor 回归**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_task_supervisor.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  -k 'restart or resume or idle or retry or publication'
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/src/perfpilot_api/local_task_supervisor.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_task_supervisor.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: supervise local analysis activity"
```

### Task 6: 增加分层健康投影

**Files:**
- Create: `services/api/src/perfpilot_api/local_analysis_health.py`
- Create: `services/api/tests/unit/test_local_analysis_health.py`
- Modify: `services/api/src/perfpilot_api/api/health.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/unit/test_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写 liveness/readiness/team health RED**

测试：

- `/v1/health` 继续匿名返回 `{"status":"ok"}`，用于进程 liveness；
- `/v1/readiness` 只返回安全汇总，不泄漏 URL/path/token；
- `/v1/teams/{team_id}/health` 要求登录和 team authority；
- team A 看不到 team B 的 Agent/device/source 状态；
- SmartPerfetto down + Trace upload 可创建时为 degraded；
- 存储不可写为 unavailable；
- Agent offline 只影响 device/source 能力；
- supervisor tick 过旧为 unavailable。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_health.py \
  services/api/tests/unit/test_app.py \
  services/api/tests/integration/test_local_app.py \
  -k 'readiness or team_health'
```

Expected: collection FAIL 或新路由 404。

- [ ] **Step 3: 实现 health aggregator**

定义闭合结果：

```python
@dataclass(frozen=True, slots=True)
class CapabilityHealth:
    name: Literal["smartperfetto", "ai", "agent", "device", "source", "storage", "supervisor"]
    state: Literal["healthy", "degraded", "unavailable"]
    message: str
    last_checked_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisHealth:
    state: Literal["healthy", "degraded", "unavailable"]
    capabilities: tuple[CapabilityHealth, ...]
```

readiness 不主动触发昂贵 AI 请求；它读取最近一次安全探测结果。HealthAggregator 按最差必要能力计算总体状态，并根据 analysis mode 返回可用性。团队接口只查询该团队 Agent/device/source。

- [ ] **Step 4: 运行健康测试**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_health.py \
  services/api/tests/unit/test_app.py \
  services/api/tests/integration/test_local_app.py \
  -k 'health or readiness'
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/src/perfpilot_api/local_analysis_health.py \
  services/api/src/perfpilot_api/api/health.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_analysis_health.py \
  services/api/tests/unit/test_app.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: expose analysis capability health"
```

### Task 7: 让前端只展示服务端权威状态

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/analysis-progress.tsx`
- Modify: `app/components/dashboard.tsx`
- Create: `app/components/system-health-banner.tsx`
- Modify: `app/globals.css`
- Modify: `tests/perfpilot-api.test.ts`
- Modify: `tests/analysis-progress.test.tsx`
- Modify: `tests/dashboard-analysis-coordinator.test.tsx`
- Create: `tests/system-health-banner.test.tsx`

- [ ] **Step 1: 写 TypeScript validator RED**

合法 1.3 解析成功；以下均拒绝：

- 缺 `runtime_status`；
- unknown stage/action；
- 负 attempt/generation；
- progress summary 超长；
- 1.2 响应携带 1.3 私有字段；
- health 响应携带 URL/path/token/unknown capability。

同时断言新的 trace、remote device 和 script device 创建请求发送 `schema_version: "1.3"`；已有 legacy fixture 仍可发送 1.0–1.2。

同时保留 1.0–1.2 fixtures 通过。

- [ ] **Step 2: 运行客户端 RED**

Run:

```bash
npx vitest run tests/perfpilot-api.test.ts
```

Expected: FAIL，`analysisResponse` 拒绝 schema 1.3。

- [ ] **Step 3: 实现严格类型和 parser**

增加 `AnalysisRuntimeStatus`、`AnalysisCapabilityHealth` 和 1.3 discriminated branch。三个新建分析客户端方法发送 1.3；legacy 解析分支保留。`analysisStillChanging` 改为：

```typescript
function analysisStillChanging(analysis: AnalysisResponse): boolean {
  return !["completed", "partially_completed", "failed", "canceled", "deleted"]
    .includes(analysis.state);
}
```

不再读取 stages 推断主状态。

- [ ] **Step 4: 写进度和健康 UI RED**

断言：

- source stage 展示已读文件数和最近更新时间；
- 3/10 分钟提示为黄色且不显示失败；
- `available_actions` 不含 cancel 时不渲染取消；
- canceled 立即停止普通轮询；
- report available + completed 同屏；
- degraded banner 说明不可用能力，不阻止可用模式；
- “复制诊断信息”不含 path/token/source content。

- [ ] **Step 5: 实现 UI**

`AnalysisProgressView` 只使用 `runtime_status` 生成当前阶段标题、进度、等待对象和动作。旧 1.0–1.2 使用现有 legacy 渲染分支。

`SystemHealthBanner`：

- healthy 不占用主页面空间；
- degraded 显示一行黄色摘要和可展开能力列表；
- unavailable 显示红色摘要并禁用受影响分析入口；
- 不展示服务 URL、内部路径或 token。

- [ ] **Step 6: 运行前端门禁**

Run:

```bash
npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/analysis-progress.test.tsx \
  tests/dashboard-analysis-coordinator.test.tsx \
  tests/system-health-banner.test.tsx
npm run lint
npm run build
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add app/lib/perfpilot-api.ts \
  app/components/analysis-progress.tsx \
  app/components/dashboard.tsx \
  app/components/system-health-banner.tsx \
  app/globals.css \
  tests/perfpilot-api.test.ts \
  tests/analysis-progress.test.tsx \
  tests/dashboard-analysis-coordinator.test.tsx \
  tests/system-health-banner.test.tsx
git commit -m "feat: explain live analysis activity"
```

### Task 8: 增加故障矩阵和真实设备验收入口

**Files:**
- Create: `services/api/tests/acceptance/test_analysis_reliability.py`
- Modify: `services/api/tests/acceptance/test_remote_agent_capture.py`
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`
- Create: `scripts/verify-real-device-reliability.py`
- Create: `services/api/tests/unit/test_verify_real_device_reliability.py`
- Modify: `README.md`

- [ ] **Step 1: 写跨层故障 RED**

验收覆盖：

- cancel 与 Agent completion 同时到达；
- cancel 与 AI completion 同时到达；
- report replace 成功但响应丢失；
- 普通重启发生在每个阶段边界；
- SmartPerfetto/source/AI 10 分钟无进度只进入 waiting，不失败；
- 新进度恢复 active 文案；
- duplicate finalize/completion/report 不产生副本；
- 磁盘不可写时创建前拒绝；
- 清空后重启只删除 analyses，不删除账号、Agent、workspace。

- [ ] **Step 2: 运行 acceptance RED**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/acceptance/test_analysis_reliability.py
```

Expected: 首次运行在缺少验收文件或相应 fault seam 处 FAIL。

- [ ] **Step 3: 实现真实设备验证脚本**

脚本只接受显式参数，不保存凭据：

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/python \
  scripts/verify-real-device-reliability.py \
  --server-url https://server.example \
  --package com.rivotek.mediacenter \
  --activity mediacenteractivity \
  --test-type cold_start \
  --duration-seconds 15 \
  --source-workspace-id 71000000-0000-4000-8000-000000000001
```

脚本依次：

1. 检查已登录用户、approved Agent 和 ready device；
2. 创建一次冷启动分析；
3. 验证服务器 host ADB probe 为零；
4. 轮询 1.3 runtime status 并打印中文进度；
5. 验证 SmartPerfetto、源码和 AI 活动；
6. 检查最终绿色 completed；
7. 检查原始 HTML；
8. 检查中文报告和 strong source refs；
9. 检查每个 generation 只有一个报告；
10. 输出安全 JSON 摘要，不输出 cookie、token、绝对路径或源码。

单元测试使用 fake client 验证参数、失败码、隐私和成功摘要。脚本不自动进入 CI，因为 CI 没有真实设备；发布前人工执行并保存退出码，不保存分析备份。

- [ ] **Step 4: 运行最终门禁**

Run:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_analysis_lifecycle.py \
  services/api/tests/unit/test_local_task_supervisor.py \
  services/api/tests/unit/test_local_cancellation.py \
  services/api/tests/unit/test_local_analysis_health.py \
  services/api/tests/unit/test_verify_real_device_reliability.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/contract/test_contract_examples.py \
  services/api/tests/acceptance/test_analysis_reliability.py \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  agents/device-agent/tests

npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/analysis-progress.test.tsx \
  tests/dashboard.test.tsx \
  tests/system-health-banner.test.tsx

.venv/bin/ruff check services/api/src services/api/tests \
  agents/device-agent/src agents/device-agent/tests scripts
npm run lint
npm run build
git diff --check
```

Expected: 全部 PASS；只允许现有环境依赖测试按既有标记 skip。

- [ ] **Step 5: 更新 README**

写明：

- `/v1/health` 是 liveness；
- readiness/team health 的含义；
- SmartPerfetto/source/AI 无固定总截止时间；
- 普通重启恢复与显式 reset 删除的区别；
- 真实设备验收命令。

- [ ] **Step 6: 提交**

```bash
git add services/api/tests/acceptance/test_analysis_reliability.py \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  scripts/verify-real-device-reliability.py \
  services/api/tests/unit/test_verify_real_device_reliability.py \
  README.md
git commit -m "test: verify reliable analysis lifecycle"
```

## 完成定义

- 所有活动分析都有服务端权威 current stage、最近进度和等待对象。
- 终态、generation、取消和报告发布由单一门禁保护。
- SmartPerfetto、源码和 AI 有进度时不受固定总时长限制。
- 控制操作没有无限重试。
- 点击取消后页面立即进入取消状态，迟到结果无法发布。
- 普通重启不重复任务；显式 reset 仍删除分析且不备份。
- 健康状态能区分 API 存活和依赖能力。
- 旧 1.0–1.2 分析继续可读。
- 真实设备冷启动、源码匹配、中文总结和报告发布验收可重复执行。
