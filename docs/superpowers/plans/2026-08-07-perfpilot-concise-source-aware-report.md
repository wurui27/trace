# PerfPilot 精简源码感知报告 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有 SmartPerfetto、Android Memory、旧报告和单轮 AI 行为的前提下，交付“上传 Trace 无需设备、可选 Agent 本机源码、最多三个痛点与行动、强匹配源码 Diff、隔离 Gradle 验证、网页与 PDF 最终报告”的完整闭环。

**Architecture:** SmartPerfetto 继续产生唯一的性能测量事实；Source Agent 只在用户明确绑定的本机 Git 工作区中创建不可变私有快照、筛选有界源码上下文，并在私有临时 worktree 验证候选补丁；服务端负责租约、匹配等级、AI 输入输出和 Diff 的确定性校验；PerfPilot AI 只执行一个逻辑轮次，正常路径一次 Provider 请求，失败时沿用同一轮最多一次重试；网页将同一份 `AnalysisReport 1.2` 分成“结论 / 源码修复 / 技术附录”。

**Tech Stack:** Python 3.12, FastAPI 0.139.x, Pydantic 2.13.x, SQLAlchemy 2 async, Alembic, PostgreSQL 17, S3-compatible object storage, Ed25519 signed Agent tasks, Git CLI, Gradle Wrapper, React 19, TypeScript 5.9, Vinext/Cloudflare Worker, Vitest, pytest, JSON Schema 2020-12.

---

## 实施前的命名校正

代码映射确认：当前 `AnalysisResponse.source_analysis` 已经表示 SmartPerfetto 来源信息，字段为 `engine/rounds/verification/session_id/run_id`。为了保持旧客户端和历史本地数据可读，本计划执行以下固定命名，不复用也不重解释旧字段：

- `source_analysis`：继续只表示 SmartPerfetto 内核来源；1.0 客户端行为不变。
- `source_binding`：创建分析时选择的源码 Provider 绑定。
- `source_code_analysis`：新的源码上下文、匹配和补丁验证状态。
- `source_context`：只在 Agent、服务端内部制品和 AI 2.0 投影中使用的有界源码片段。
- `source_fix`：AI 输出中的候选修改；只有验证状态为 `verified` 才形成可下载补丁。

设计文档中表示“源码分析状态”的 `source_analysis` 在代码和新契约中一律落实为 `source_code_analysis`。除此命名校正外，设计决策不变。

## 文件职责

- `contracts/v1/analyses/*.schema.json`：创建请求 1.0/1.1 和分析响应 1.0/1.1 的闭合兼容契约。
- `contracts/v1/agents/*.schema.json`：Heartbeat 1.0/1.1、设备采集任务和源码任务的判别联合契约。
- `contracts/v1/ai/*.schema.json`：旧 1.0 与新精简、源码感知 2.0 投影和输出。
- `contracts/v1/reports/analysis-report.schema.json`：旧 1.0/1.1 和新三层 `AnalysisReport 1.2`。
- `agents/device-agent/src/perfpilot_agent/source_models.py`：本机工作区、验证配置和公开 Heartbeat 元数据模型。
- `agents/device-agent/src/perfpilot_agent/source_registry.py`：只在 Agent 本机保存绝对路径的原子注册表。
- `agents/device-agent/src/perfpilot_agent/source_snapshot.py`：Git tracked 工作树快照、私有快照仓库、哈希和缓存生命周期。
- `agents/device-agent/src/perfpilot_agent/source_rules.py`：确定性 Android 性能规则候选筛选。
- `agents/device-agent/src/perfpilot_agent/source_runner.py`：`source_context` 与 `patch_verification` 任务分派。
- `agents/device-agent/src/perfpilot_agent/patch_validation.py`：Diff 复核、私有 worktree、固定 argv Gradle 执行和清理。
- `services/api/src/perfpilot_api/services/source_workspaces.py`：从 Agent capability 投影可选工作区并校验绑定。
- `services/api/src/perfpilot_api/services/source_tasks.py`：源码任务持久化、租约、续租、取消和幂等完成。
- `services/api/src/perfpilot_api/reports/source_context.py`：源码上下文边界、引用闭合和 `strong|weak|none` 匹配。
- `services/api/src/perfpilot_api/reports/source_patch.py`：服务端静态 Unified Diff 校验和下载授权。
- `services/api/src/perfpilot_api/workers/source_orchestrator.py`：SmartPerfetto 后的源码任务、120 秒降级和补丁验证协调。
- `services/api/src/perfpilot_api/reports/projection.py`：创建不超过 256 KiB 的 1.0 或 2.0 AI 投影。
- `services/api/src/perfpilot_api/ai/synthesis.py`：1.0/2.0 结构、引用、数值、规则、源码和 Diff 校验。
- `services/api/src/perfpilot_api/ai/local_report.py`：本地闭环使用同一 2.0 投影、同一逻辑轮次和重试语义。
- `services/api/src/perfpilot_api/reports/writer.py`：不可变 `AnalysisReport 1.2` 发布和旧版本读取。
- `app/components/source-workspace-field.tsx`：Trace 上传和在线采集共用的可选源码工作区选择器。
- `app/components/analysis-report.tsx`：报告版本分派；旧报告保持原渲染。
- `app/components/concise-report-summary.tsx`：最多三个指标、痛点和行动的默认结论。
- `app/components/source-fixes-panel.tsx`：源码建议、验证状态、Diff 预览和补丁下载。
- `app/components/technical-appendix.tsx`：折叠显示全部原始指标、证据和生成信息。
- `app/components/full-analysis-report.tsx`、`app/lib/report-print.ts`：三层报告打印/PDF 和验证中禁用规则。

## 全程不变量

- 上传已有 Trace 的表单不读取、显示或要求 Android 设备；只有在线抓取 Trace 才要求实时 Agent 和 ADB 设备。
- 绝对源码路径只存在 Agent 本机注册表和进程内 `Path`；不得进入 HTTP、签名任务、数据库、对象键、报告、日志或遥测。
- `workspace_id` 使用随机 UUID，不能由路径、仓库 remote 或名称派生。
- 快照只包含 Git tracked 文件的当前内容，包括 staged/unstaged 修改和 tracked 删除；排除 untracked、ignored、symlink、submodule、构建产物、二进制和敏感文件。
- Agent 发出的源码上下文最多覆盖三个核心 finding，总计不超过 96 KiB；单个 completion JSON 不超过 128 KiB。
- SmartPerfetto 是 metric、threshold、finding、severity 和 evidence 的唯一性能事实来源；AI 不得创建新的测量事实。
- `strong|weak|none` 由服务端确定，AI 只能引用；只有 `strong` 可以保留 `source_fix.diff`。
- 2.0 输出最多三个关键指标、三个 finding、三个 P0/P1/P2 建议、三个 source fix 和三个复测项。
- 正常路径一个 Provider 请求；可重试错误最多追加一次 attempt，仍只有一个 `role=report` 的逻辑 round。
- 每个补丁只修改一个已提供的 Kotlin、Java 或 XML 文件；所有补丁正文合计不超过 64 KiB。
- Gradle 命令来自本机登记的验证配置，以固定 argv 启动，不使用 shell；真实工作树和真实 `.git` 不发生写入。
- 只有 `verified` 补丁可下载；任何源码链路失败都不能抹掉已完成的 SmartPerfetto 主报告。
- `source_archive` 只为旧 API/历史记录继续读取，不进入新源码分析，也不再出现在新建分析 UI。
- 每个任务在 `main` 上完成一个独立提交；除非用户另行要求，计划执行阶段不自动 push 或部署。
- 当前 `infra/ubuntu-user/systemd/perfpilot-api.service` 启动的是 `local_app` 测试 API，只承诺 Trace-only 精简报告；跨机器 Source Agent 必须连接 `main.create_app` 生产控制面（PostgreSQL、对象存储和现有 Agent control routes）。Task 12 的真实源码烟测以 `2026-08-05-perfpilot-ubuntu-lan-deployment.md` 已完成为前置条件，不得通过向 `local_app` 暴露源码路径来绕过该边界。

### Task 1: 冻结 1.1/2.0/1.2 版本化契约

**Files:**
- Modify: `contracts/v1/analyses/create-request.schema.json`
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Modify: `contracts/v1/agents/heartbeat-request.schema.json`
- Modify: `contracts/v1/agents/task-poll-response.schema.json`
- Create: `contracts/v1/agents/source-task-snapshot.schema.json`
- Create: `contracts/v1/agents/source-task-completion.schema.json`
- Modify: `contracts/v1/ai/analysis-projection.schema.json`
- Modify: `contracts/v1/ai/synthesis-output.schema.json`
- Modify: `contracts/v1/reports/analysis-report.schema.json`
- Create: `contracts/v1/examples/source-task-snapshot.valid.json`
- Create: `contracts/v1/examples/source-task-completion.valid.json`
- Create: `contracts/v1/examples/analysis-projection-v2.valid.json`
- Create: `contracts/v1/examples/synthesis-output-v2.valid.json`
- Create: `contracts/v1/examples/analysis-report-v1.2.valid.json`
- Modify: `services/api/tests/contract/test_agent_contracts.py`
- Modify: `services/api/tests/contract/test_ai_report_contracts.py`
- Modify: `services/api/tests/contract/test_contract_examples.py`

- [ ] **Step 1: 写 RED 契约测试，固定兼容矩阵**

在 `test_ai_report_contracts.py` 中增加参数化测试，证明旧 fixtures 仍通过，新 fixtures 在缺少必填字段或出现额外字段时失败：

```python
@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("ai/analysis-projection.schema.json", "analysis-projection-v2.valid.json"),
        ("ai/synthesis-output.schema.json", "synthesis-output-v2.valid.json"),
        ("reports/analysis-report.schema.json", "analysis-report-v1.2.valid.json"),
    ],
)
def test_source_aware_examples_are_closed_and_valid(
    schema_name: str,
    example_name: str,
) -> None:
    document = _example(example_name)
    _validator(schema_name).validate(document)
    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate({**document, "unexpected": True})
```

在 `test_agent_contracts.py` 中固定 source task 不需要 `device_digest`，device task 仍需要：

```python
def test_source_task_is_not_a_device_capture_task() -> None:
    source = _example("source-task-snapshot.valid.json")
    _validator("agents/source-task-snapshot.schema.json").validate(source)
    assert source["task_type"] == "source_context"
    assert "device_digest" not in source
```

- [ ] **Step 2: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/contract/test_agent_contracts.py \
  services/api/tests/contract/test_ai_report_contracts.py \
  services/api/tests/contract/test_contract_examples.py -q
```

Expected: 新 examples 或新 schema 不存在，测试失败；旧契约测试仍可收集。

- [ ] **Step 3: 定义分析创建和状态 1.1**

`source_binding` 只能是以下闭合对象；`snapshot_policy` 固定为 `tracked_worktree`，`validation_profile_id` 是 Agent 生成的 UUID 或 null。不得出现 `path`、`repo_url`、`remote` 或任意自由命令：

```json
{
  "provider_kind": "agent_workspace",
  "agent_id": "91000000-0000-4000-8000-000000000001",
  "workspace_id": "92000000-0000-4000-8000-000000000001",
  "snapshot_policy": "tracked_worktree",
  "validation_profile_id": "94000000-0000-4000-8000-000000000001"
}
```

`create-request 1.0` 保持原闭合分支；`1.1` 的 `trace_upload` 和 `device` 分支可选 `source_binding`。设备采集中的 `device_id` 与 `source_binding.agent_id` 不要求相同。`analysis-response 1.1` 保留旧 `source_analysis` 并新增以下闭合字段：

```json
{
  "requested": true,
  "provider_kind": "agent_workspace",
  "agent_id": "91000000-0000-4000-8000-000000000001",
  "workspace_id": "92000000-0000-4000-8000-000000000001",
  "snapshot_policy": "tracked_worktree",
  "validation_profile_id": "94000000-0000-4000-8000-000000000001",
  "context_state": "available",
  "match_summary": "strong",
  "verification_state": "verified",
  "failure_code": null
}
```

`requested=false` 时 provider/agent/workspace/policy/profile 为 null，状态固定为 `not_requested/none/not_requested`。

- [ ] **Step 4: 定义 Agent 源码任务和 completion**

`source-task-snapshot 1.0` 使用 `oneOf` 判别两个任务：

```json
{
  "schema_version": "1.0",
  "task_type": "source_context",
  "execution_id": "93000000-0000-4000-8000-000000000001",
  "analysis_id": "82000000-0000-4000-8000-000000000001",
  "team_id": "81000000-0000-4000-8000-000000000001",
  "agent_id": "91000000-0000-4000-8000-000000000001",
  "workspace_id": "92000000-0000-4000-8000-000000000001",
  "snapshot_policy": "tracked_worktree",
  "validation_profile_id": null,
  "lease_version": 1,
  "expires_at": "2026-08-07T09:00:00Z",
  "finding_hints": [],
  "limits": {"max_findings": 3, "max_files": 12, "max_bytes": 98304}
}
```

`patch_verification` 额外要求 `snapshot_id`、64 字符 `snapshot_hash`、`fix_id`、非空 UUID `validation_profile_id` 和最多 65,536 UTF-8 bytes 的 `patch`。`source_context` 允许 `validation_profile_id=null`，但仍固定 `snapshot_policy=tracked_worktree`。completion 统一包含任务类型、execution/analysis/workspace/lease、终态、`result` 与 Ed25519 签名；结果 JSON 最大 128 KiB，不包含路径和完整 Gradle 日志。

- [ ] **Step 5: 定义 AI 2.0 和报告 1.2 的精确上限**

`analysis-projection 2.0` 保留 1.0 的 facts，并增加可选 `source_context`。`synthesis-output 2.0` 要求：

```json
{
  "schema_version": "2.0",
  "verdict": "启动卡顿的首要问题是主线程同步初始化。",
  "executive_summary": "先移出首帧前的非必要初始化，再以相同场景复测。",
  "key_metric_ids": ["84000000-0000-4000-8000-000000000001"],
  "top_findings": [],
  "recommendations": [],
  "source_fixes": [],
  "retest_plan": [],
  "limitations": []
}
```

数组最大值分别为 3/3/3/3/3/20。recommendation priority 只接受 `p0|p1|p2` 且唯一。source fix 必须 `match_grade=strong`，单文件、最多两个 source refs、允许扩展名 `.kt|.java|.xml`。`AnalysisReport 1.2` 要求 `synthesis.output.schema_version=2.0`，新增 `source_code`，同时完整保留 `scenario_reports` 供附录使用。

- [ ] **Step 6: 运行 GREEN、静态检查并提交**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/contract/test_agent_contracts.py \
  services/api/tests/contract/test_ai_report_contracts.py \
  services/api/tests/contract/test_contract_examples.py -q
uv run --offline --locked ruff check services/api/tests/contract
git diff --check
git add contracts/v1 services/api/tests/contract
git commit -m "feat: define source-aware report contracts"
```

Expected: 新旧 examples 全部通过；闭合字段、版本分支、最大数组和判别联合测试全部为 GREEN。

### Task 2: 实现 Agent 本机源码工作区注册与安全 Heartbeat

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/source_models.py`
- Create: `agents/device-agent/src/perfpilot_agent/source_registry.py`
- Modify: `agents/device-agent/src/perfpilot_agent/cli.py`
- Modify: `agents/device-agent/src/perfpilot_agent/devices.py`
- Modify: `agents/device-agent/src/perfpilot_agent/config.py`
- Create: `agents/device-agent/tests/unit/test_source_registry.py`
- Modify: `agents/device-agent/tests/unit/test_cli.py`
- Modify: `agents/device-agent/tests/unit/test_devices.py`
- Modify: `agents/device-agent/tests/integration/test_heartbeat.py`

- [ ] **Step 1: 写 RED 注册表和 CLI 测试**

使用临时真实 Git 仓库覆盖 add/list/remove/doctor，固定随机 ID 和路径隐私：

```python
PROFILE_ID = UUID("94000000-0000-4000-8000-000000000001")


def test_registers_random_workspace_without_exposing_path(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "真实源码")
    registry = SourceWorkspaceRegistry(tmp_path / "agent-state", uuid_factory=lambda: WORKSPACE_ID)
    view = registry.add(
        name="Demo Android",
        path=repo,
        validation_profiles=(
            ValidationProfile(
                PROFILE_ID,
                "Android check",
                ("./gradlew", ":app:lintDebug", "--no-daemon", "--console=plain"),
                ".",
                600,
                (0,),
            ),
        ),
    )
    assert view.workspace_id == WORKSPACE_ID
    assert str(repo) not in repr(view)
    assert str(repo) not in json.dumps(view.public_document(), ensure_ascii=False)
    assert registry.registry_path.stat().st_mode & 0o077 == 0
```

分别断言拒绝相对路径、非 Git 目录、`workspace_root` 内部目录、重复名称、非法 profile ID、shell 字符串、绝对 working directory 和超过 1,200 秒 timeout。

- [ ] **Step 2: 运行 Agent 测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-device-agent pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_source_registry.py \
  agents/device-agent/tests/unit/test_cli.py \
  agents/device-agent/tests/unit/test_devices.py -q
```

Expected: `perfpilot_agent.source_registry` 导入失败。

- [ ] **Step 3: 实现闭合本机模型和原子注册表**

`source_models.py` 使用不可变 dataclass：

```python
@dataclass(frozen=True, slots=True)
class ValidationProfile:
    profile_id: UUID
    name: str
    argv: tuple[str, ...]
    working_directory: str
    timeout_seconds: int
    allowed_exit_codes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SourceWorkspace:
    workspace_id: UUID
    name: str
    path: Path = field(repr=False)
    validation_profiles: tuple[ValidationProfile, ...]
```

注册表固定为 `<workspace_root>/source-workspaces.json`，写入临时同目录文件、`fsync`、`os.replace`，POSIX mode `0600`；Windows 使用现有平台层进行仅当前用户 ACL 的 best-effort，并在无法保证权限时拒绝保存。反序列化时再次校验绝对路径和 Git 工作树，不把原始 JSON 带进异常。

- [ ] **Step 4: 增加可脚本化 CLI**

新增命令；工作区和验证配置分开登记，profile ID 与 workspace ID 都由 Agent 随机生成：

```text
perfpilot-agent source add --name "Demo Android" --path "/absolute/path/to/project"
perfpilot-agent source list --json
perfpilot-agent source remove --workspace-id 92000000-0000-4000-8000-000000000001
perfpilot-agent source doctor --workspace-id 92000000-0000-4000-8000-000000000001 --json
perfpilot-agent source validation add \
  --workspace-id 92000000-0000-4000-8000-000000000001 \
  --name "Android check" --working-directory . --timeout-seconds 600 --allowed-exit-code 0 \
  -- ./gradlew :app:lintDebug --no-daemon --console=plain
perfpilot-agent source validation list \
  --workspace-id 92000000-0000-4000-8000-000000000001 --json
perfpilot-agent source validation remove \
  --workspace-id 92000000-0000-4000-8000-000000000001 \
  --profile-id 94000000-0000-4000-8000-000000000001
```

`--` 后的 argv 按 token 原样登记并始终以 `shell=False` 执行；首个 token 必须是仓库内相对 wrapper，POSIX 示例为 `./gradlew`，Windows 示例为 `gradlew.bat`。拒绝管道、重定向、命令替换和绝对 executable。list/doctor JSON 只输出 workspace ID、名称、Git branch/HEAD、tracked dirty count、profile ID/name 和状态，不输出 argv。

- [ ] **Step 5: Heartbeat 1.1 上报公开元数据**

`HeartbeatPublisher` 接受 `SourceWorkspaceRegistry`，把以下对象放入 `source_workspaces`；上报失败不影响设备 Heartbeat：

```python
{
    "workspace_id": str(workspace.workspace_id),
    "name": workspace.name,
    "state": "ready",
    "git_branch": branch,
    "git_head": head,
    "tracked_dirty_count": dirty_count,
    "snapshot_policy": "tracked_worktree",
    "validation_profiles": [
        {"profile_id": str(profile.profile_id), "name": profile.name}
        for profile in workspace.validation_profiles
    ],
}
```

不发送 remote URL、绝对路径、Gradle argv 或 Git diff。

- [ ] **Step 6: 运行 GREEN、跨平台模型测试并提交**

```bash
uv run --offline --locked --package perfpilot-device-agent pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_source_registry.py \
  agents/device-agent/tests/unit/test_cli.py \
  agents/device-agent/tests/unit/test_devices.py \
  agents/device-agent/tests/integration/test_heartbeat.py -q
uv run --offline --locked ruff check agents/device-agent
git diff --check
git add agents/device-agent
git commit -m "feat: register Agent source workspaces"
```

Expected: macOS/Linux path fixtures、Windows drive path normalization、Unicode 名称和 CRLF JSON 都通过，测试输出与日志不含临时仓库绝对路径。

### Task 3: 暴露工作区目录并持久化分析绑定

**Files:**
- Create: `services/api/src/perfpilot_api/services/source_workspaces.py`
- Modify: `services/api/src/perfpilot_api/services/device_directory.py`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/api/agents.py`
- Modify: `services/api/src/perfpilot_api/config.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/jobs.py`
- Create: `services/api/migrations/control/versions/0012_source_bindings.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_migrations.py`
- Modify: `services/api/tests/integration/test_agent_api.py`
- Modify: `services/api/tests/integration/test_analysis_api.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/tests/unit/test_analysis_service.py`
- Create: `services/api/tests/unit/test_source_workspaces.py`

- [ ] **Step 1: 写 RED Heartbeat、目录和创建请求测试**

覆盖以下行为：Agent Heartbeat 1.1 将脱敏工作区写入 `Agent.capabilities`; `GET /v1/teams/{team_id}/source-workspaces` 只返回当前团队、online Agent、ready workspace；创建 Trace/device analysis 可以带绑定；`gerrit`、路径字段、离线 Agent、未知 workspace/profile 和跨团队 ID 返回 422/404；不带绑定时行为和请求 hash 保持兼容。

```python
response = await client.get(
    f"/v1/teams/{TEAM_ID}/source-workspaces",
    headers=_browser_headers(csrf),
    cookies=_session_cookie(),
)
assert response.json() == {
    "schema_version": "1.0",
    "workspaces": [{
        "provider_kind": "agent_workspace",
        "agent_id": str(AGENT_ID),
        "agent_name": "Ray Mac",
        "workspace_id": str(WORKSPACE_ID),
        "name": "Demo Android",
        "state": "ready",
        "git_branch": "main",
        "git_head": "1" * 40,
        "tracked_dirty_count": 1,
        "snapshot_policy": "tracked_worktree",
        "validation_profiles": [{
            "profile_id": "94000000-0000-4000-8000-000000000001",
            "name": "Android check",
        }],
    }],
}
```

- [ ] **Step 2: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_source_workspaces.py \
  services/api/tests/unit/test_analysis_service.py \
  services/api/tests/integration/test_agent_api.py \
  services/api/tests/integration/test_analysis_api.py -q
```

Expected: endpoint、service 和 GlobalJob 字段不存在。

- [ ] **Step 3: 校验并保存 Heartbeat capability**

`AgentHeartbeatRequest` 接受 schema 1.0 原形和 1.1 `source_workspaces`，Pydantic 模型总数上限 32、profile 每工作区上限 8。`DeviceDirectory.record_heartbeat` 将公开数组写入 `Agent.capabilities["source_workspaces"]`，同时保留当前 `clock/disk/execution_slot`。拒绝嵌套未知键、绝对路径样式、非 40 位 Git SHA 和重复 workspace ID。

在 `Settings` 增加 `source_code_analysis_enabled`（生产默认 false）、`source_context_deadline_seconds=120`、`source_context_max_bytes=98304` 和 `source_patch_max_bytes=65536`，使用严格范围校验。`main.create_app()` 显式构造并注入 `SourceWorkspaceService`；功能关闭时 list 返回空数组，创建请求携带 binding 时返回稳定 `source_code_analysis_disabled`，而不是静默忽略。

- [ ] **Step 4: 增加工作区读取服务和 API**

实现：

```python
@dataclass(frozen=True, slots=True)
class SourceWorkspaceView:
    provider_kind: Literal["agent_workspace"]
    agent_id: UUID
    agent_name: str
    workspace_id: UUID
    name: str
    state: Literal["ready", "invalid"]
    git_branch: str | None
    git_head: str
    tracked_dirty_count: int
    snapshot_policy: Literal["tracked_worktree"]
    validation_profiles: tuple[SourceValidationProfileView, ...]
```

`SourceWorkspaceService.list_for_team()` 只读 Agent capabilities，不创建独立 workspace 数据表；`require_binding()` 必须重新读取 online Agent 的最新 capability，禁止相信浏览器传来的名称、Git SHA 或状态。

- [ ] **Step 5: 迁移并持久化不含路径的绑定**

`0012_source_bindings.py` 给 `global_jobs` 增加 nullable `source_provider_kind`、`source_agent_id`、`source_workspace_id`、`source_snapshot_policy`、`source_validation_profile_id`，加入 provider/policy enum、核心 ID 成组存在和团队外键约束。profile ID 使用 UUID，但允许为空，因为没有验证配置时仍可进行源码匹配。ORM 约束核心为：

```sql
num_nonnulls(
  source_provider_kind,
  source_agent_id,
  source_workspace_id,
  source_snapshot_policy
) IN (0, 4)
AND (source_provider_kind IS NULL OR source_provider_kind = 'agent_workspace')
AND (source_snapshot_policy IS NULL OR source_snapshot_policy = 'tracked_worktree')
AND (source_provider_kind IS NOT NULL OR source_validation_profile_id IS NULL)
```

`canonical_trace_analysis_request_hash` 和 device 请求 hash 在 schema 1.1 时包含 provider、agent、workspace、snapshot policy 和 nullable validation profile；1.0 无绑定 hash 字节不变。`AnalysisView` 新增 `source_binding` 与 `source_code_analysis`，本任务先投影 `not_requested|waiting_for_agent`，后续任务推进状态。

- [ ] **Step 6: 本地存储兼容**

`_LocalAnalysis` 与 `local_analysis_store.py` 保存 `source_binding` 和独立 `source_code_analysis`。读取旧 JSON 时默认未请求；现有 `source_run/source_analysis` 继续表示 SmartPerfetto。旧 `source_archive` input slot 继续返回，但不会推导绑定。

- [ ] **Step 7: 运行迁移、GREEN 并提交**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/integration/test_migrations.py \
  services/api/tests/integration/test_agent_api.py \
  services/api/tests/integration/test_analysis_api.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/unit/test_analysis_service.py \
  services/api/tests/unit/test_source_workspaces.py -q
uv run --offline --locked ruff check services/api/src/perfpilot_api services/api/migrations services/api/tests
git diff --check
git add services/api
git commit -m "feat: bind analyses to Agent source workspaces"
```

Expected: migration upgrade/downgrade和 ORM parity 通过；数据库列、响应、异常和 caplog 中搜索不到测试仓库绝对路径。

### Task 4: 建立独立源码任务租约并让 Agent 判别分派

**Files:**
- Create: `services/api/src/perfpilot_api/db/control/models/source_tasks.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/__init__.py`
- Create: `services/api/migrations/control/versions/0013_source_code_tasks.py`
- Create: `services/api/src/perfpilot_api/services/source_tasks.py`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/api/src/perfpilot_api/security/task_snapshots.py`
- Modify: `agents/device-agent/src/perfpilot_agent/control_client.py`
- Modify: `agents/device-agent/src/perfpilot_agent/security.py`
- Modify: `agents/device-agent/src/perfpilot_agent/service.py`
- Modify: `agents/device-agent/src/perfpilot_agent/cli.py`
- Create: `services/api/tests/unit/test_source_task_service.py`
- Modify: `services/api/tests/unit/test_task_snapshots.py`
- Modify: `services/api/tests/integration/test_agent_task_api.py`
- Modify: `services/api/tests/integration/test_migrations.py`
- Modify: `agents/device-agent/tests/unit/test_security.py`
- Modify: `agents/device-agent/tests/integration/test_task_loop.py`

- [ ] **Step 1: 写 RED 任务状态和判别联合测试**

固定两个互不混淆的执行路径：现有 capture task 仍由 `AgentLease` 和 `TaskExecutor` 处理；源码任务由新 `SourceTask` 和 `SourceTaskExecutor` 处理。测试必须证明 source task 没有 device 字段、capture task 不能声明 workspace、旧 Agent 遇到未知 task type 会安全拒绝而不是当成采集任务。

```python
@pytest.mark.asyncio
async def test_task_loop_dispatches_source_context_without_adb() -> None:
    control = FakeControl(tasks=[_signed_source_task("source_context")])
    capture = AsyncMock()
    source = AsyncMock()
    await TaskLoop(control=control, executor=capture, source_executor=source, state=AgentRuntimeState()).run_once()
    capture.execute.assert_not_awaited()
    source.execute.assert_awaited_once()
```

- [ ] **Step 2: 运行测试并确认 RED**

```bash
uv run --offline --locked pytest -p no:cacheprovider \
  services/api/tests/unit/test_source_task_service.py \
  services/api/tests/unit/test_task_snapshots.py \
  agents/device-agent/tests/unit/test_security.py \
  agents/device-agent/tests/integration/test_task_loop.py -q
```

Expected: `SourceTask`、source task signer 和 `source_executor` 不存在。

- [ ] **Step 3: 新建源码任务控制表**

`SourceTask` 字段固定为：id、team_id、analysis_id、agent_id、workspace_id、task_type、state、lease_version、lease_token_digest、expires_at、request_document、request_sha256、completion_artifact_id、completion_sha256、failure_code、created_at、updated_at、started_at、completed_at。`request_document` 只含可公开签名字段；completion artifact ID/checksum 只绑定 tenant-private 对象，不在控制库保存源码内容。

```python
class SourceTask(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, ControlBase):
    __tablename__ = "source_tasks"
    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
```

约束：task type 为 `source_context|patch_verification`；state 为 `queued|leased|running|cancel_requested|completed|failed|canceled|expired`；同 analysis 只允许一个非终态 context task，同 fix 只允许一个非终态 validation task；`AgentLease` 不改为 nullable device。

- [ ] **Step 4: 实现幂等租约服务**

`SourceTaskService` 暴露 `create_context_task`、`create_patch_task`、`lease_next`、`renew`、`request_cancel`、`ack_cancel`、`complete`、`expire_stale`。lease token 只返回一次，数据库只存 SHA-256 digest；所有 mutation 带 `execution_id + agent_id + lease_version + token` fence。相同 completion canonical SHA-256 幂等返回成功，不同 completion 返回 409。

`main.create_app()` 将 `SourceTaskService` 注入 Agent control router。定义 `SourceTaskCompletionRecorder` Protocol：HTTP complete 必须先把正文交给 recorder 获得 `artifact_id + checksum`，再以同一个 fence 标记任务完成。Task 4 的测试 recorder 只返回固定 artifact binding；Task 6 用 tenant-private `SourceArtifactService` 替换它。控制表不保存源码正文或完整日志。

- [ ] **Step 5: 扩展 Agent control 路由而不破坏设备任务**

`GET /v1/agent/tasks/next` 先完成当前 active lease 恢复，再按最老创建时间在设备任务和源码任务中选择一个；响应使用：

```json
{
  "schema_version": "1.1",
  "task_kind": "source",
  "lease_token": "opaque-token",
  "snapshot": {},
  "signature_b64": "base64-ed25519-signature"
}
```

1.0 device poll 响应字节结构不变。renew/cancel-ack/complete 根据签名任务中的 task kind 路由到对应 service；不依赖客户端自由传入的 kind。源码 completion canonical JSON 超过 128 KiB 返回 413。

- [ ] **Step 6: Agent 验签并分派 union**

在 `security.py` 定义 `VerifiedCaptureTask | VerifiedSourceTask`。先用闭合 Pydantic 模型解析，再验证 audience、agent、team、execution、lease version、expiry、canonical bytes 和 Ed25519 signature。`TaskLoop` 只在 `isinstance(task, VerifiedSourceTask)` 时调用 `SourceTaskExecutor`；源码任务不得初始化或调用 ADB runner。

- [ ] **Step 7: 运行 GREEN、迁移测试并提交**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked pytest -p no:cacheprovider \
  services/api/tests/integration/test_migrations.py \
  services/api/tests/integration/test_agent_task_api.py \
  services/api/tests/unit/test_source_task_service.py \
  services/api/tests/unit/test_task_snapshots.py \
  agents/device-agent/tests/unit/test_security.py \
  agents/device-agent/tests/integration/test_task_loop.py -q
uv run --offline --locked ruff check services/api agents/device-agent
git diff --check
git add services/api agents/device-agent
git commit -m "feat: lease signed source analysis tasks"
```

Expected: 两种任务的租约恢复、过期、取消、续租和重复完成均通过；源码任务测试中 ADB mock 零调用。

### Task 5: 创建 Git tracked 私有快照并筛选有界源码上下文

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/source_snapshot.py`
- Create: `agents/device-agent/src/perfpilot_agent/source_rules.py`
- Create: `agents/device-agent/src/perfpilot_agent/source_runner.py`
- Modify: `agents/device-agent/src/perfpilot_agent/cli.py`
- Modify: `agents/device-agent/src/perfpilot_agent/service.py`
- Create: `agents/device-agent/tests/unit/test_source_snapshot.py`
- Create: `agents/device-agent/tests/unit/test_source_rules.py`
- Create: `agents/device-agent/tests/unit/test_source_runner.py`
- Create: `agents/device-agent/tests/integration/test_source_context_execution.py`

- [ ] **Step 1: 写 RED 快照语义测试**

临时仓库必须同时包含 committed、staged、unstaged、tracked deleted、untracked、ignored、symlink、二进制和模拟 submodule。测试快照只包含前三类当前字节和删除记录：

```python
def test_snapshot_captures_current_tracked_tree_without_touching_real_git(tmp_path: Path) -> None:
    repo = _mixed_git_repo(tmp_path / "app")
    before = _tree_digest(repo)
    git_before = _tree_digest(repo / ".git")
    result = SourceSnapshotter(cache_root=tmp_path / "cache").create(repo, WORKSPACE_ID)
    assert result.read_text("app/src/main/java/demo/MainActivity.kt") == "staged + unstaged\n"
    assert "deleted.kt" in result.deleted_paths
    assert "untracked.kt" not in result.paths
    assert _tree_digest(repo) == before
    assert _tree_digest(repo / ".git") == git_before
```

补充 Windows 路径分隔、CRLF/LF、Unicode 文件名、超过 512 MiB、敏感文件名/内容和缓存 TTL 测试。

- [ ] **Step 2: 写 RED 候选筛选与敏感扫描测试**

输入最多三个 finding hints。排序依次使用直接类/方法/Trace section 命中、Android 组件/包名、确定性性能规则、文件稳定次序。输出固定最多 12 文件、每片段最多 160 行、总计 98,304 bytes；敏感命中只生成 exclusion，不发送内容。

```python
assert [item.relative_path for item in context.fragments] == [
    "app/src/main/java/demo/Startup.kt",
    "app/src/main/AndroidManifest.xml",
]
assert context.total_bytes <= 96 * 1024
assert all(not Path(item.relative_path).is_absolute() for item in context.fragments)
assert "api_key" not in context.canonical_bytes.decode("utf-8")
```

- [ ] **Step 3: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-device-agent pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_source_snapshot.py \
  agents/device-agent/tests/unit/test_source_rules.py \
  agents/device-agent/tests/unit/test_source_runner.py -q
```

Expected: 三个新模块导入失败。

- [ ] **Step 4: 实现只读工作树采集和私有 Git 仓库**

使用固定 argv 的 Git 子进程，不执行 hooks：`git -c core.hooksPath=/dev/null ls-files -s -z`、`git hash-object`、`git cat-file`。逐个 `lstat` 拒绝 symlink；解析 mode `160000` 排除 submodule。读取当前 tracked 文件字节并按 allowlist/大小/UTF-8 校验。把允许文件写入 `<workspace_root>/source-cache/<snapshot_id>/tree`，在该目录初始化独立私有 Git 仓库并提交，所有作者时间固定为任务创建时间以保持可复现。

快照哈希定义为排序后的 `relative_path + NUL + mode + NUL + sha256(content)` canonical SHA-256；tracked 删除进入单独排序数组。真实仓库只执行只读命令，不运行 `git worktree add`、index 写入、stash、commit 或 checkout。

- [ ] **Step 5: 实现 Android 性能规则筛选**

首版规则 ID 固定为：

```python
ANDROID_RULES = (
    SourceRule("android.startup.main_thread_io", ("StrictMode", "readBytes", "SQLiteDatabase")),
    SourceRule("android.startup.eager_initialization", ("Application.onCreate", "ContentProvider", "Initializer")),
    SourceRule("android.ui.blocking_wait", ("runBlocking", "Thread.sleep", "Future.get", "CountDownLatch.await")),
    SourceRule("android.compose.unstable_recomposition", ("@Composable", "remember", "derivedStateOf")),
    SourceRule("android.memory.listener_leak", ("registerReceiver", "addObserver", "addListener")),
    SourceRule("android.memory.bitmap_retention", ("Bitmap", "ImageDecoder", "LruCache")),
)
```

规则只负责候选排序，不自行声称根因。每个片段包含随机 source ref UUID、relative path、symbol、start/end line、content SHA-256、snapshot hash、rule IDs 和内容；异常/排除只返回稳定 code。

- [ ] **Step 6: 实现 `source_context` runner 与缓存清理**

`SourceTaskRunner.execute_source_context()` 校验 registry 中 workspace、创建快照、筛选片段、生成不超过 128 KiB 的 completion 并调用现有 authenticated completion API。缓存单快照上限 512 MiB、总上限 2 GiB、终态后 24 小时 TTL；按 oldest terminal snapshot 回收，绝不把新快照复用成旧 snapshot ID。

- [ ] **Step 7: 运行 GREEN、只读证明并提交**

```bash
uv run --offline --locked --package perfpilot-device-agent pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_source_snapshot.py \
  agents/device-agent/tests/unit/test_source_rules.py \
  agents/device-agent/tests/unit/test_source_runner.py \
  agents/device-agent/tests/integration/test_source_context_execution.py -q
uv run --offline --locked ruff check agents/device-agent
git diff --check
git add agents/device-agent
git commit -m "feat: extract bounded source context snapshots"
```

Expected: fixture 的真实工作树和 `.git` 前后 digest 完全相等；completion 不含绝对路径、remote、untracked 或 secret sentinel。

### Task 6: 服务端保存源码上下文、判定匹配并在超时后降级

**Files:**
- Create: `services/api/src/perfpilot_api/reports/source_context.py`
- Create: `services/api/src/perfpilot_api/services/source_artifacts.py`
- Create: `services/api/src/perfpilot_api/workers/source_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/workers/synthesis_runtime.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/api/src/perfpilot_api/services/trace_executions.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Create: `services/api/tests/unit/test_source_context.py`
- Create: `services/api/tests/unit/test_source_artifacts.py`
- Create: `services/api/tests/integration/test_source_orchestrator.py`
- Modify: `services/api/tests/integration/test_synthesis_orchestrator.py`
- Modify: `services/api/tests/integration/test_trace_ai_report_pipeline.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写 RED 上下文闭合与匹配测试**

服务端必须拒绝绝对/穿越路径、重复 ref、非法行区间、哈希漂移、未知规则、超过 12 文件/96 KiB/128 KiB、symlink/submodule 标记和 private transport 数据。匹配只能由确定性证据提升：

```python
@pytest.mark.parametrize(
    ("direct_identifiers", "candidate_symbols", "expected"),
    [
        (("demo.Startup.init",), ("demo.Startup.init",), "strong"),
        ((), ("Startup.init",), "weak"),
        ((), (), "none"),
    ],
)
def test_match_grade_is_server_determined(direct_identifiers, candidate_symbols, expected):
    assert grade_source_match(direct_identifiers, candidate_symbols) == expected
```

- [ ] **Step 2: 写 RED 编排降级测试**

覆盖：无绑定不创建 task；SmartPerfetto 完成前不创建 task；Agent 正常返回保存 context；Agent 离线、120 秒 deadline、非法 completion 或工作区失效只令 `source_code_analysis.context_state=unavailable`，随后照常排队 synthesis；SmartPerfetto 失败时不创建 source task 和 AI。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_source_context.py \
  services/api/tests/unit/test_source_artifacts.py \
  services/api/tests/integration/test_source_orchestrator.py -q
```

Expected: source context/artifact/orchestrator 模块不存在。

- [ ] **Step 4: 实现上下文校验与私有不可变制品**

`validate_source_context()` 先以 canonical JSON 计算大小，再验证引用和哈希，返回 defensive copy。`SourceArtifactService` 使用 tenant-private 前缀：

```text
raw/analyses/{analysis_id}/internal/source-context/{artifact_id}.json
raw/analyses/{analysis_id}/internal/source-patches/{artifact_id}.patch
raw/analyses/{analysis_id}/internal/source-validation/{artifact_id}.json
```

所有读写绑定 VersionId、MIME、size、SHA-256、team/analysis/artifact identity；control DB 只保存 artifact UUID 和 checksum。对象正文沿用分析 retention，删除/reset 时一起清理。

- [ ] **Step 5: 实现确定性匹配等级**

`strong` 只在 SmartPerfetto/Mapping/Native symbol/显式业务 Trace section 的直接应用标识与同一快照 symbol 精确匹配且哈希有效时产生；包名、文件名或规则启发式只能产生 `weak`；无候选或上下文不可用为 `none`。将每个 ref 的等级和整体最高等级固化进 context artifact，AI 无权覆盖。

- [ ] **Step 6: 接入 SmartPerfetto 后置编排**

`SourceOrchestrator.prepare_for_synthesis(analysis_id)`：

1. 读取已验证的最新 SmartPerfetto normalized report；
2. 无绑定时写 `requested=false` 并立即允许现有 synthesis；
3. 有绑定时幂等创建 context task，把状态设为 waiting/extracting；
4. 在部署 deadline 120 秒内消费 completion；
5. 成功时持久化 context 并设 available/match；
6. 所有可降级错误写稳定 failure code 并设 unavailable/none；
7. 无论成功或降级都只创建一次 synthesis generation。

本地 `local_app` 复用同一 pure validator；没有远程 Source Agent capability 时明确降级为 `source_agent_unavailable`，不阻塞本地 Trace-only 报告。

- [ ] **Step 7: 运行 GREEN、失败隔离测试并提交**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_source_context.py \
  services/api/tests/unit/test_source_artifacts.py \
  services/api/tests/integration/test_source_orchestrator.py \
  services/api/tests/integration/test_synthesis_orchestrator.py \
  services/api/tests/integration/test_trace_ai_report_pipeline.py \
  services/api/tests/integration/test_local_app.py -q
uv run --offline --locked ruff check services/api
git diff --check
git add services/api
git commit -m "feat: orchestrate bounded source context"
```

Expected: context 成功和所有降级状态均进入 AI；SmartPerfetto 报告在 source timeout/invalid/offline 测试中仍可读取。

### Task 7: 生成精简、源码感知的单轮 AI 2.0 报告

**Files:**
- Modify: `services/api/src/perfpilot_api/reports/projection.py`
- Modify: `services/api/src/perfpilot_api/ai/synthesis.py`
- Modify: `services/api/src/perfpilot_api/ai/local_report.py`
- Create: `services/api/src/perfpilot_api/ai/prompts/perfpilot-report-v3.txt`
- Modify: `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/reports/writer.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/unit/test_ai_projection.py`
- Modify: `services/api/tests/unit/test_ai_synthesis_validator.py`
- Modify: `services/api/tests/unit/test_ai_prompt.py`
- Modify: `services/api/tests/unit/test_local_report.py`
- Modify: `services/api/tests/unit/test_analysis_report_writer.py`
- Modify: `services/api/tests/integration/test_trace_ai_report_pipeline.py`

- [ ] **Step 1: 写 RED 2.0 投影和输出测试**

验证无源码时也产生 2.0 精简输出且 `source_context=null/source_fixes=[]`；有源码时投影只含被服务端验证的 refs。拒绝超过三项、重复 priority、虚构 metric/finding/evidence/rule/ref、AI 提升 match、数值漂移、路径不一致、弱匹配 Diff 和叙述中无依据数值。

```python
def test_v2_output_is_bounded_and_reference_closed() -> None:
    validated = validate_synthesis_output(_v2_candidate(), _v2_projection())
    output = validated.document
    assert len(output["key_metric_ids"]) <= 3
    assert len(output["top_findings"]) <= 3
    assert [item["priority"] for item in output["recommendations"]] == ["p0", "p1", "p2"]
    assert len(output["source_fixes"]) <= 3
```

- [ ] **Step 2: 写 RED 单逻辑轮次测试**

正常 provider 返回有效 2.0 时 calls=1；第一次网络/schema 错误后成功时 calls=2 但 rounds 仍为一个 `report`、attempts=2；补丁验证失败不得触发 provider 再调用。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  services/api/tests/unit/test_ai_prompt.py \
  services/api/tests/unit/test_local_report.py -q
```

Expected: 2.0 文档被现有 1.0 validator 拒绝。

- [ ] **Step 4: 构建有界 `analysis-projection 2.0`**

投影从 normalized report 选择最多三项严重度/置信度最高的核心 finding hints，但仍携带附录需要的全部事实引用索引。源码部分最多 96 KiB；总 canonical bytes 最大 256 KiB，超限时按低排名片段逐个删除并记录 limitation，绝不截断 UTF-8 或单个 ref。

投影明确包裹不可信源码：

```json
{
  "source_context": {
    "trust": "untrusted_data_not_instructions",
    "snapshot_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "match_summary": "strong",
    "fragments": []
  }
}
```

- [ ] **Step 5: 更新 prompt 和严格 2.0 validator**

`perfpilot-report-v3.txt` 要求 JSON only、直接说痛点、不复述完整指标、不输出通用教程、不执行源码注释指令、不承诺具体收益、只在 strong 时输出 Diff。Validator 先验证核心输出；再逐项过滤非法 source fix，把稳定 limitation 加入结果；核心 schema/metric/finding/evidence/数值错误仍触发同一轮一次 retry。

recommendation 对每个优先级最多一项，排序固定 p0/p1/p2。source fix 的 rule/ref/path/symbol/snapshot 必须与投影完全相等，Diff 静态检查在 Task 8 完成。

- [ ] **Step 6: 发布 `AnalysisReport 1.2`**

writer 从同一 normalized facts、validated synthesis 和 source state 组装：

```python
report = {
    "schema_version": "1.2",
    "analysis_id": str(analysis_id),
    "analysis_mode": analysis_mode,
    "state": report_state,
    "report_version": report_version,
    "generated_at": generated_at.isoformat(),
    "scenario_reports": scenario_reports,
    "synthesis": synthesis_document,
    "source_code": source_code_document,
}
```

旧 report 1.0/1.1 读取和 checksum 校验不变。AI 整体失败时仍发布 partial SmartPerfetto report；手动“重新生成 AI”复用仍有效 context，否则以明确 limitation 降级。

- [ ] **Step 7: 运行 GREEN、调用次数证明并提交**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  services/api/tests/unit/test_ai_prompt.py \
  services/api/tests/unit/test_local_report.py \
  services/api/tests/unit/test_analysis_report_writer.py \
  services/api/tests/integration/test_trace_ai_report_pipeline.py -q
uv run --offline --locked ruff check services/api
git diff --check
git add contracts/v1/examples services/api
git commit -m "feat: synthesize concise source-aware reports"
```

Expected: trace-only 与 strong-source 两个 1.2 fixture 通过；正常 provider 调用恰好一次；retry fixture 为同一 round 两次 attempt。

### Task 8: 在服务端静态拒绝不安全或不闭合的 Diff

**Files:**
- Create: `services/api/src/perfpilot_api/reports/source_patch.py`
- Modify: `services/api/src/perfpilot_api/ai/synthesis.py`
- Modify: `services/api/src/perfpilot_api/workers/source_orchestrator.py`
- Create: `services/api/tests/unit/test_source_patch.py`
- Modify: `services/api/tests/unit/test_ai_synthesis_validator.py`
- Modify: `services/api/tests/integration/test_source_orchestrator.py`

- [ ] **Step 1: 写 RED Diff 攻击和允许用例**

测试矩阵必须包含：单文件 Kotlin/Java/XML 正常补丁；绝对路径、`..`、`.git`、新文件、删除文件、rename/copy、mode change、binary patch、symlink/submodule、两个文件、未提供文件、hunk 超出片段、旧行不匹配、CRLF、超过 64 KiB、外部下载命令和敏感文件名。

```python
@pytest.mark.parametrize(
    "patch_name",
    [
        "absolute-path.patch",
        "traversal.patch",
        "rename.patch",
        "binary.patch",
        "outside-fragment.patch",
        "old-lines-mismatch.patch",
    ],
)
def test_rejects_unsafe_patch_fixture(patch_name: str) -> None:
    with pytest.raises(SourcePatchRejected, match="^source patch is invalid$"):
        validate_source_patch(_fixture(patch_name), _strong_context())
```

- [ ] **Step 2: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_source_patch.py \
  services/api/tests/unit/test_ai_synthesis_validator.py -q
```

Expected: `perfpilot_api.reports.source_patch` 导入失败。

- [ ] **Step 3: 实现最小 Unified Diff parser**

不引入 shell 或执行 Git。Parser 只接受 UTF-8 文本、一个 `diff --git a/path b/path`、相同路径、`--- a/path`、`+++ b/path` 和标准 hunk。将 `\` 统一拒绝，PurePosixPath 规范化后必须保持原值。只接受 `.kt/.java/.xml`；拒绝 `GIT binary patch`、`new file mode`、`deleted file mode`、`old/new mode`、`similarity index`、`rename/copy from/to`。

- [ ] **Step 4: 验证 hunk 与 source refs 完全闭合**

每个 source fix 必须引用同一 relative path 的一或两个 fragments。每个 hunk 的旧行范围必须落在引用片段并集内；context line 和 deleted line 按顺序与 fragment 内容完全相等；新增行扫描 NUL、secret patterns、外部下载/执行模式。返回 canonical LF patch、path、SHA-256、changed line counts；错误只暴露 `source patch is invalid` 和稳定内部 code，不回显补丁。

- [ ] **Step 5: 过滤单项失败并创建验证任务**

AI 输出核心通过后逐个调用 validator。失败 fix 从 public source fixes 移除并加入 `invalid_source_patch` limitation；其他 findings/recommendations 保留。只有至少一个有效 strong fix 且 profile 存在时创建 `patch_verification` task；否则状态为 `not_requested` 或 `not_configured`，不得假装 verified。

- [ ] **Step 6: 运行 GREEN、安全回归并提交**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_source_patch.py \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  services/api/tests/integration/test_source_orchestrator.py -q
uv run --offline --locked ruff check services/api/src/perfpilot_api/reports services/api/tests/unit/test_source_patch.py
git diff --check
git add services/api
git commit -m "feat: validate source patches before execution"
```

Expected: 所有攻击 fixture 被稳定拒绝；一个非法 fix 不影响同报告内另一个合法 fix，也不影响 SmartPerfetto 结论。

### Task 9: 在 Agent 私有 worktree 应用补丁并运行登记的 Gradle 验证

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/patch_validation.py`
- Modify: `agents/device-agent/src/perfpilot_agent/source_runner.py`
- Modify: `agents/device-agent/src/perfpilot_agent/service.py`
- Modify: `agents/device-agent/src/perfpilot_agent/state.py`
- Modify: `services/api/src/perfpilot_api/services/source_tasks.py`
- Modify: `services/api/src/perfpilot_api/workers/source_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/reports/writer.py`
- Create: `agents/device-agent/tests/unit/test_patch_validation.py`
- Create: `agents/device-agent/tests/integration/test_patch_validation_execution.py`
- Modify: `agents/device-agent/tests/integration/test_cancellation.py`
- Modify: `services/api/tests/unit/test_source_task_service.py`
- Modify: `services/api/tests/integration/test_source_orchestrator.py`

- [ ] **Step 1: 写 RED 私有 worktree 和真实仓库不变测试**

创建最小 Gradle Wrapper fixture 和私有 snapshot repo。验证成功、`git apply --check` 失败、Gradle 非零、timeout、cancel、lease lost、snapshot hash 漂移、profile 不存在全部清理临时目录，并保持真实 repo 与真实 `.git` digest 不变。

```python
@pytest.mark.asyncio
async def test_verified_patch_never_mutates_registered_workspace(tmp_path: Path) -> None:
    fixture = _registered_snapshot_fixture(tmp_path)
    before_tree = _tree_digest(fixture.real_repo)
    before_git = _tree_digest(fixture.real_repo / ".git")
    result = await PatchValidator(fixture.registry, fixture.cache).validate(_patch_task())
    assert result.state == "verified"
    assert _tree_digest(fixture.real_repo) == before_tree
    assert _tree_digest(fixture.real_repo / ".git") == before_git
    assert list(fixture.temp_root.iterdir()) == []
```

- [ ] **Step 2: 写 RED 固定命令和进程组测试**

断言 runner 只使用 registry 中 profile argv；任务 patch 中即便出现 `; curl`、`$()` 或 `&&` 也不能改变 argv。POSIX 使用新 session，Windows 使用 new process group/job object；取消/timeout 终止子进程树。环境只保留固定 allowlist：PATH、JAVA_HOME、ANDROID_HOME/ANDROID_SDK_ROOT、TMPDIR/TEMP、HOME 指向临时目录、Gradle user home 指向私有 cache。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
uv run --offline --locked --package perfpilot-device-agent pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_patch_validation.py \
  agents/device-agent/tests/integration/test_patch_validation_execution.py \
  agents/device-agent/tests/integration/test_cancellation.py -q
```

Expected: `PatchValidator` 不存在。

- [ ] **Step 4: 实现快照复原和临时 worktree**

在 Agent 私有 snapshot repo 上执行固定 argv：

```python
git_argv = (
    str(git_binary),
    "-c", "core.hooksPath=/dev/null",
    "--git-dir", str(private_repo),
    "worktree", "add", "--detach", str(temp_worktree), snapshot_commit,
)
```

worktree 建立后重新计算 snapshot hash；不匹配直接 `source_changed`。写入 patch 到私有 temp，以 `git -c core.hooksPath=/dev/null apply --check --whitespace=nowarn PATCH` 检查，再 apply。Git 命令使用 `shell=False`，路径来自已校验的私有 cache，不来自 AI 自由文本。

- [ ] **Step 5: 执行登记 profile 并生成有界结果**

从注册表读取当前平台已经登记的完整 argv；不做字符串拼接，也不把 POSIX wrapper 静默改写成 Windows wrapper。timeout 默认 600 秒且最大 1,200 秒。捕获 stdout/stderr 总计最多 64 KiB，按 SecretRedactor、绝对路径、token/credential pattern 脱敏后只上传末尾摘要、exit code、duration 和状态：

```python
PatchValidationResult(
    state="verified",
    exit_code=0,
    duration_ms=duration_ms,
    profile_id=UUID("94000000-0000-4000-8000-000000000001"),
    patch_sha256=patch_sha256,
    log_summary="BUILD SUCCESSFUL",
)
```

finally 中先 `git worktree remove --force`、再 `git worktree prune`、最后删除 temp。清理失败只记录稳定 code，不记录路径。

- [ ] **Step 6: 服务端接收终态并发布新报告版本**

`SourceTaskService.complete()` 校验 patch checksum/profile/snapshot 与 task request 相同。`SourceOrchestrator` 映射：成功=`verified`，apply 错=`apply_failed`，非零=`validation_failed`，hash 漂移=`source_changed`，无 profile=`not_configured`，deadline=`timeout`，cancel/lease/unavailable 分别落稳定状态。每次终态发布新的 immutable report version；AI 不再次调用。

- [ ] **Step 7: 运行 GREEN、取消/超时回归并提交**

```bash
uv run --offline --locked pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_patch_validation.py \
  agents/device-agent/tests/integration/test_patch_validation_execution.py \
  agents/device-agent/tests/integration/test_cancellation.py \
  services/api/tests/unit/test_source_task_service.py \
  services/api/tests/integration/test_source_orchestrator.py -q
uv run --offline --locked ruff check agents/device-agent services/api
git diff --check
git add agents/device-agent services/api
git commit -m "feat: verify patches in private Agent worktrees"
```

Expected: success/failure/cancel/timeout 后 temp 目录为空；真实工作树和 `.git` digest 在所有测试中不变；provider call count 不增加。

### Task 10: 分离 Trace 上传与在线采集，并增加可选源码工作区

**Files:**
- Create: `app/components/source-workspace-field.tsx`
- Modify: `app/components/trace-upload-form.tsx`
- Modify: `app/components/device-analysis-form.tsx`
- Modify: `app/components/new-analysis-dialog.tsx`
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `tests/perfpilot-api.test.ts`
- Modify: `tests/trace-upload-form.test.tsx`
- Modify: `tests/device-analysis-form.test.tsx`
- Modify: `tests/new-analysis-dialog.test.tsx`

- [ ] **Step 1: 写 RED 用户流程测试**

上传模式测试不得渲染“设备”“Pixel 8”或设备错误；Trace 文件仍为唯一必填。在线采集模式继续要求真实 remote device。两个模式都可以独立选择源码工作区，source Agent 可以不同于 capture device Agent。

```tsx
it("uploads an existing Trace without reading Android devices", async () => {
  const devices = vi.fn();
  render(<NewAnalysisDialog open client={{ ...client, devices }} />);
  await userEvent.click(screen.getByRole("tab", { name: "上传 Trace" }));
  expect(screen.queryByLabelText(/Android 设备/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Pixel 8/)).not.toBeInTheDocument();
  expect(devices).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: 写 RED 浏览器 API 严格校验测试**

新增 `sourceWorkspaces(teamId)` 响应验证，拒绝额外字段、路径、非法 SHA、duplicate ID。`createTrace`/`createDeviceAnalysis` 可选 `SourceBinding`，请求 schema 使用 1.1；无绑定时仍发送 1.0，维持旧服务兼容。Analysis response 1.0/1.1 都可读，并把 SmartPerfetto `source_analysis` 与新 `source_code_analysis` 分开。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
npm run test:unit -- \
  tests/perfpilot-api.test.ts \
  tests/trace-upload-form.test.tsx \
  tests/device-analysis-form.test.tsx \
  tests/new-analysis-dialog.test.tsx
```

Expected: `SourceWorkspaceField` 和 client method 不存在，source ZIP 断言仍为旧行为。

- [ ] **Step 4: 增加闭合前端类型和 client 方法**

```typescript
export interface SourceWorkspaceKey {
  readonly provider_kind: "agent_workspace";
  readonly agent_id: string;
  readonly workspace_id: string;
  readonly snapshot_policy: "tracked_worktree";
}

export interface SourceBinding extends SourceWorkspaceKey {
  readonly validation_profile_id: string | null;
}

export interface SourceWorkspaceView extends SourceWorkspaceKey {
  readonly agent_name: string;
  readonly name: string;
  readonly state: "ready" | "invalid";
  readonly git_branch: string | null;
  readonly git_head: string;
  readonly tracked_dirty_count: number;
  readonly validation_profiles: readonly {
    readonly profile_id: string;
    readonly name: string;
  }[];
}
```

选择 workspace 后再用用户选中的 profile UUID 或 null 构造 binding。所有 runtime validators 使用 exactKeys 并显式拒绝键名 `path/repo_url/remote/argv`。

- [ ] **Step 5: 实现共用工作区选择器**

`SourceWorkspaceField` 初始为“暂不关联源码”。展开后加载 team workspaces，按 Agent 名称分组，显示分支、短 SHA、dirty count 和 validation profile。离线/invalid 不进入列表；空态给出本机命令说明和 Agent 管理页链接。选择结果只回传 binding ID，不保留路径。

- [ ] **Step 6: 移除新建 UI 的 source ZIP 并保持 API 兼容**

从 `trace-upload-form.tsx` 删除 `source_archive` file input 和相关说明；不要从 `TraceInputKind`、上传排序或后端 slot 中删除该枚举。Trace submit payload 加可选 binding。选择 Trace tab 时不挂载 `PerfPilotSessionProvider` 的 device selector，也不调用 `devices()`。在线采集继续显示实时设备，并在 APK/场景字段之外显示独立 source selector。

- [ ] **Step 7: 确认后台创建行为**

创建请求成功后沿用当前 coordinator：立即关闭弹窗、主界面 active task card 显示当前阶段与取消；不在弹窗中等待 SmartPerfetto、source context 或 AI。状态文案新增“正在读取源码上下文”“正在验证源码修复”，但 Trace-only 分析不显示源码等待。

- [ ] **Step 8: 运行 GREEN、lint 并提交**

```bash
npm run test:unit -- \
  tests/perfpilot-api.test.ts \
  tests/trace-upload-form.test.tsx \
  tests/device-analysis-form.test.tsx \
  tests/new-analysis-dialog.test.tsx \
  tests/active-analysis-task-card.test.tsx
npm run lint
git diff --check
git add app tests
git commit -m "feat: select optional Agent source workspaces"
```

Expected: 上传 Trace 在 devices API 抛错时仍能提交；在线采集没有真实 ready device 时仍被阻止；界面没有 source ZIP 和固定 Pixel 8。

### Task 11: 渲染三层最终报告、验证状态、补丁下载和 PDF

**Files:**
- Create: `app/components/concise-report-summary.tsx`
- Create: `app/components/source-fixes-panel.tsx`
- Create: `app/components/technical-appendix.tsx`
- Modify: `app/components/analysis-report.tsx`
- Modify: `app/components/full-analysis-report.tsx`
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/lib/report-print.ts`
- Modify: `app/globals.css`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/services/source_artifacts.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `tests/analysis-report.test.tsx`
- Modify: `tests/full-analysis-report.test.tsx`
- Modify: `tests/perfpilot-api.test.ts`
- Modify: `tests/report-print.test.ts`
- Create: `services/api/tests/integration/test_source_patch_download.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写 RED 报告版本分派和三标签测试**

旧 1.0/1.1 fixture 继续使用现有渲染。1.2 默认打开“结论”，最多显示三个 key metrics、三个 findings 和三个 actions；完整第 4 项只出现在技术附录。标签必须为“结论 / 源码修复 / 技术附录”。

```tsx
expect(screen.getAllByTestId("key-metric")).toHaveLength(3);
expect(screen.getAllByTestId("top-finding")).toHaveLength(3);
expect(screen.getAllByTestId("priority-action")).toHaveLength(3);
expect(screen.queryByText("原始指标 4")).not.toBeInTheDocument();
await userEvent.click(screen.getByRole("tab", { name: "技术附录" }));
expect(screen.getByText("原始指标 4")).toBeInTheDocument();
```

- [ ] **Step 2: 写 RED 源码状态和下载测试**

无源码显示“本次分析未关联源码”；weak 显示只给建议且无文件/行号/Diff；pending/validating 显示验证中且无下载；apply/validation/source/timeout 错显示稳定说明；只有 verified 渲染 Diff 和“下载 .patch”。客户端必须验证响应 MIME `text/x-diff`、Content-Disposition 和最大 64 KiB。

- [ ] **Step 3: 写 RED 授权下载 API 测试**

`GET /v1/teams/{team_id}/analyses/{analysis_id}/source-fixes/{fix_id}/patch` 覆盖：verified same-team=200；非 verified=409；未知/跨团队=404；checksum/version mismatch=503；响应 `Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff`、安全文件名和 attachment。

- [ ] **Step 4: 运行测试并确认 RED**

```bash
npm run test:unit -- \
  tests/analysis-report.test.tsx \
  tests/full-analysis-report.test.tsx \
  tests/perfpilot-api.test.ts \
  tests/report-print.test.ts
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/integration/test_source_patch_download.py -q
```

Expected: 1.2 runtime validator、三个组件和 download endpoint 不存在。

- [ ] **Step 5: 实现严格 1.2 TypeScript 模型与版本分派**

增加 `ConciseSynthesisOutput`、`SourceCodeReport`、`SourceFix`、`PatchVerificationState`，不放宽旧类型为任意对象。`validAnalysisReport` 先读取 schema version，再分别调用 legacy 或 v1.2 exact validator。`AnalysisReportView` 只负责：

```tsx
if (report.schema_version === "1.2") {
  return <SourceAwareAnalysisReport report={report} onDownloadPatch={onDownloadPatch} />;
}
return <LegacyAnalysisReport report={report} onRetrySynthesis={onRetrySynthesis} retrying={retrying} />;
```

把当前渲染主体原样提取为 legacy component，避免历史报告视觉回归。

- [ ] **Step 6: 实现简洁结论和源码修复**

结论按 verdict、两段以内 summary、key metrics、findings/actions、最重要 limitation 排列。源码修复卡显示相对路径、symbol、规则机制、finding/evidence 引用、match、validation profile/duration/state、Diff `<pre>` 和 retest；只有 verified 调用 patch client。weak/none 不渲染虚构位置或占位 Diff。

- [ ] **Step 7: 实现独立技术附录和打印模式**

附录默认折叠 SmartPerfetto metrics/timeline、findings/evidence/SQL/interval、source refs/hash/exclusions、provider/prompt/model/version 和 validation summary。屏幕使用 tabs；`data-report-printing=true` 时三个 section 全部顺序展开并隐藏 tab controls。验证处于 pending/validating 时“下载 PDF”禁用并说明原因；终态失败仍可下载包含失败摘要的 PDF。

- [ ] **Step 8: 实现 verified-only 下载**

API 先授权 team/analysis，再从最新 report 找 fix，要求 `verification.state == verified`，用 VersionId 和 checksum 读取 source patch artifact。文件名固定 `perfpilot-{analysis_id}-{fix_id}.patch`。本地 API 使用相同状态检查和私有文件读取；绝不从 URL 接受对象路径。

- [ ] **Step 9: 运行 GREEN、SSR、构建并提交**

```bash
npm run test:unit -- \
  tests/analysis-report.test.tsx \
  tests/full-analysis-report.test.tsx \
  tests/perfpilot-api.test.ts \
  tests/report-print.test.ts \
  tests/final-report-navigation-contract.test.ts
npm run test:ssr
npm run lint
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/integration/test_source_patch_download.py \
  services/api/tests/integration/test_local_app.py -q
uv run --offline --locked ruff check services/api
git diff --check
git add app tests services/api
git commit -m "feat: render and download verified source fixes"
```

Expected: 1.2 主体严格不超过三项；旧报告快照通过；只有 verified fixture 产生 patch 请求；打印 HTML 同时包含三层内容。

### Task 12: 完成跨层回归、真实 Agent 验收和 Ubuntu 发布闸门

**Files:**
- Create: `services/api/tests/acceptance/test_source_aware_report_flow.py`
- Create: `agents/device-agent/tests/fixtures/source-project/README.md`
- Create: `agents/device-agent/tests/fixtures/source-project/settings.gradle.kts`
- Create: `agents/device-agent/tests/fixtures/source-project/build.gradle.kts`
- Create: `agents/device-agent/tests/fixtures/source-project/app/src/main/java/demo/Startup.kt`
- Create: `scripts/acceptance-source-report.sh`
- Modify: `README.md`
- Create: `docs/deployment/ubuntu-lan.md`
- Create: `docs/operations/device-agent.md`
- Create: `docs/security/source-analysis.md`

- [ ] **Step 1: 写端到端 acceptance 测试**

使用真实 Git CLI、真实 Agent task loop、fake SmartPerfetto canonical result 和 fake AI Provider；不能 mock SourceSnapshotter、Diff parser 或 PatchValidator。覆盖：

1. Trace-only 上传：设备 API 零调用，精简报告完成；
2. strong source：当前 tracked dirty 内容进入 context，合法 Diff 验证，patch 可下载；
3. weak source：source fixes 为空，只有建议；
4. Agent 在 context 前离线：主报告完成、源码降级；
5. malicious Diff：验证任务不创建；
6. Gradle 非零：不可下载但报告完成；
7. 取消：任务进程组和 temp worktree 被清理。

```python
assert result.provider_calls == 1
assert result.logical_rounds == 1
assert len(result.report["synthesis"]["output"]["key_metric_ids"]) <= 3
assert result.real_workspace_digest_before == result.real_workspace_digest_after
assert result.real_git_digest_before == result.real_git_digest_after
```

- [ ] **Step 2: 运行 acceptance 并修复仅由集成暴露的问题**

```bash
uv run --offline --locked pytest -p no:cacheprovider \
  services/api/tests/acceptance/test_source_aware_report_flow.py -q
```

Expected before final fixes: 若存在跨层命名、序列化或状态顺序错误，测试明确失败；不得用放宽 schema 或跳过验证使其通过。

- [ ] **Step 3: 增加一键真实环境验收脚本**

`scripts/acceptance-source-report.sh` 只编排已有 CLI/API：检查 API health、Agent status、source workspace doctor；打印用户需要执行的 Trace upload/online capture 步骤；轮询分析到终态；下载 report JSON/PDF 提示和 verified patch；最后比较测试前后的 `git status --porcelain=v1`、tracked tree digest 和 `.git` digest。脚本使用 `set -euo pipefail`，不删除用户仓库、不自动注册未知路径、不打印 token。

- [ ] **Step 4: 文档化 Ubuntu 与三平台边界**

README/部署文档明确：网页/API/Workers 部署在 Ubuntu；macOS、Windows、Linux 开发机各自运行 Agent；Android 设备只在“在线抓取”时连接任一 Agent；上传已有 Trace 不需要设备；源码绝对路径不离开 Agent；Gradle 验证会在私有临时源码副本运行用户预登记任务，但不是统一强制断网沙箱。给出精确注册例子：

```bash
perfpilot-agent source add \
  --name "Demo Android" \
  --path "/absolute/path/to/project"
perfpilot-agent source validation add \
  --workspace-id 92000000-0000-4000-8000-000000000001 \
  --name "Android check" --working-directory . --timeout-seconds 600 --allowed-exit-code 0 \
  -- ./gradlew :app:lintDebug --no-daemon --console=plain
perfpilot-agent source doctor \
  --workspace-id 92000000-0000-4000-8000-000000000001 --json
```

Windows 文档使用 PowerShell 绝对路径和同一 CLI 参数，不要求 WSL。

- [ ] **Step 5: 运行完整自动化回归**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked pytest -p no:cacheprovider \
  services/api/tests agents/device-agent/tests -q
uv run --offline --locked ruff check services/api agents/device-agent
npm test
npm run lint
npm run build
git diff --check
```

Expected: API、Agent、contract、migration、frontend unit、SSR 和 production build 全部通过；没有 skipped required integration test。若 `127.0.0.1:55439` 没有项目测试 PostgreSQL，先按现有 CI/README 的 PostgreSQL 17 测试服务配置启动它；不得删除 `PERFPILOT_REQUIRE_POSTGRES_TESTS=1` 或把 required tests 改为 skip。

- [ ] **Step 6: 执行隐私和遗留兼容审计**

```bash
rg -n "source_archive" app services/api contracts/v1 tests
rg -n "source_analysis|source_code_analysis|source_binding" app services/api contracts/v1
rg -n "Pixel 8|pixel 8" app tests
git grep -nE "shell=True|shell: *true" -- agents/device-agent services/api
```

Expected:

- `source_archive` 只出现在 API 兼容枚举/旧测试，不出现在新建表单；
- `source_analysis` 仍只对应 SmartPerfetto，源码字段全部为 `source_code_analysis`；
- 产品 UI 没有固定 Pixel 8；
- 源码/补丁路径没有 shell 执行。

另外在 acceptance log、API captured requests、数据库 dump 和对象 key 清单中搜索真实测试仓库绝对路径与 secret sentinel，结果必须为空。

- [ ] **Step 7: 在 Ubuntu LAN 做一次真实烟测**

先检查 Ubuntu API 进程是 `perfpilot_api.main:create_app` 而不是 `local_app:create_local_app`，并确认 PostgreSQL、对象存储、Agent control routes 与 `PERFPILOT_SOURCE_CODE_ANALYSIS_ENABLED=true` 已生效；未满足时先执行依赖计划 `docs/superpowers/plans/2026-08-05-perfpilot-ubuntu-lan-deployment.md`，不得在测试 API 上伪造通过。随后按 `docs/deployment/ubuntu-lan.md` 更新服务，从 Mac Agent 注册一个真实 Android 仓库，完成：Trace-only 上传、strong source 上传、在线抓取三条路径。记录 analysis ID、Agent ID、report version、provider calls、patch state 和真实工作树前后 digest；不提交真实源码片段或凭据到仓库。

- [ ] **Step 8: 最终提交**

```bash
git status --short
git diff --check
git add services/api/tests/acceptance agents/device-agent/tests/fixtures/source-project \
  scripts/acceptance-source-report.sh README.md docs
git commit -m "test: verify source-aware report workflow"
git log --oneline -12
```

Expected: 12 个任务各自形成清晰提交；工作区只剩用户原有的 `.superpowers/` 未跟踪资产或明确记录的非本功能文件。

## 实施完成后的规格核对

- [ ] 上传 Trace 路径没有设备字段、设备 API 调用或固定演示设备。
- [ ] 在线抓取只使用 Agent Heartbeat 中实时 ADB 设备。
- [ ] 服务器任何边界都没有源码绝对路径，workspace ID 不由路径派生。
- [ ] tracked staged/unstaged 内容进入快照，untracked/symlink/submodule/secret 不进入。
- [ ] `source_analysis` 与 `source_code_analysis` 在 JSON Schema、Pydantic、dataclass、TypeScript 和 UI 文案中语义一致。
- [ ] 无源码和旧报告仍可用；`source_archive` UI 删除但 API 可读。
- [ ] AI 正常调用一次；重试仍是一轮，补丁失败不再次请求 AI。
- [ ] 默认结论最多 3 个 metric/finding/action，完整事实只在技术附录。
- [ ] weak/none 没有 Diff、文件级根因或虚构行号。
- [ ] 所有 Diff 通过服务端静态校验，只有 verified 可下载。
- [ ] 真实工作树与真实 `.git` 字节级不变，临时 worktree 总能清理。
- [ ] PDF 包含结论、源码修复和独立技术附录。
- [ ] source Agent/AI/validation 失败不丢失 SmartPerfetto 主报告。
- [ ] macOS、Windows、Linux protocol fixtures 与 Ubuntu LAN 真机烟测通过。
- [ ] Gerrit Provider 没有混入首版实现。
