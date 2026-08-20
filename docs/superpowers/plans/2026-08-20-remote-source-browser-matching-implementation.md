# 远程源码目录浏览与匹配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在网页中安全浏览 Agent 明确授权的目录，选择目录后立即开始分析；分析阶段必须真实读取 Android 工程文件并校验测试包名，只有匹配且读取到源码时才发布源码根因和 Diff。

**Architecture:** Agent 私有配置保存绝对授权根；服务器只接收根 ID、显示名称和匿名目录节点。目录浏览使用短期签名任务，通过现有 Agent 出站轮询交付，Agent 以 dir-fd/no-follow 逐级解析并返回一层目录。选中目录后创建持久化 `SourceWorkspaceBinding`；正式 source task 先做只读工程预检，再由服务端按测试包名和启动类判定 match，只有 match 成功才继续现有 bounded source snapshot。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、Ed25519 signed tasks、Git read-only snapshot、Android Manifest/Gradle 文本解析、Next.js/React、TypeScript、pytest、Vitest。

**Prerequisite:** 先按顺序完成 `2026-08-20-admin-registration-implementation.md` 和 `2026-08-20-agent-approval-implementation.md`；本计划以 approved Agent 与管理员权限为权威边界。

---

## 文件结构

- `agents/device-agent/src/perfpilot_agent/config.py`：私有授权根配置。
- `agents/device-agent/src/perfpilot_agent/source_browser.py`：安全目录解析、分页和匿名节点会话。
- `agents/device-agent/src/perfpilot_agent/source_registry.py`：从已验证匿名节点注册工作区。
- `agents/device-agent/src/perfpilot_agent/source_models.py`：browse/preflight 闭合模型。
- `agents/device-agent/src/perfpilot_agent/service.py`：browse 和 register task 分派。
- `agents/device-agent/src/perfpilot_agent/source_runner.py`：工程预检与现有源码 snapshot 组合。
- `contracts/v1/agents`、`contracts/v1/analyses`、`contracts/v1/reports` 与 `contracts/v1/examples`：签名任务、完成响应和 Analysis 状态合同。
- `services/api/src/perfpilot_api/services/source_workspaces.py`：browse session、绑定和团队授权。
- `services/api/src/perfpilot_api/services/source_tasks.py`：预检输入与 match authority。
- `services/api/src/perfpilot_api/local_app.py`：网页浏览 API、分析创建和报告状态。
- `app/lib/perfpilot-api.ts`：严格目录和匹配合同。
- `app/components/source-workspace-field.tsx`：Agent 根目录浏览器。
- `app/components/new-analysis-dialog.tsx`：包名必填和选择目录后创建分析。
- `app/components/source-fixes-panel.tsx`：不匹配/不可读提示。
- 对应 Agent、API、contract、frontend 与 acceptance 测试。

### Task 1: 声明私有授权根并安全列目录

**Files:**
- Modify: `agents/device-agent/src/perfpilot_agent/config.py`
- Create: `agents/device-agent/src/perfpilot_agent/source_browser.py`
- Modify: `agents/device-agent/src/perfpilot_agent/source_registry.py`
- Test: `agents/device-agent/tests/unit/test_config.py`
- Create: `agents/device-agent/tests/unit/test_source_browser.py`
- Modify: `agents/device-agent/tests/unit/test_source_registry.py`

- [ ] **Step 1: 写授权根和目录攻击 RED**

测试构造两个授权根，覆盖：

- 配置只在 Agent 本机保存 absolute path；
- 对外 root descriptor 只含 stable root ID 和用户定义名称；
- 每次只列一层、目录名排序、分页 200 项、响应最大 128 KiB；
- `..`、绝对路径、文件、symlink、FIFO、socket 和超深节点拒绝；
- 构造后替换 root 或父目录时拒绝且不触碰外部目录；
- 匿名 node ID 不能跨 root、browse session 或 Agent 复用；
- browse session 30 秒过期；
- 选择目录后注册工作区，外部结果仍不含绝对路径。

- [ ] **Step 2: 运行 Agent RED**

Run:

~~~bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider -q \
  agents/device-agent/tests/unit/test_config.py \
  agents/device-agent/tests/unit/test_source_browser.py \
  agents/device-agent/tests/unit/test_source_registry.py
~~~

Expected: FAIL，`perfpilot_agent.source_browser` 不存在。

- [ ] **Step 3: 实现 descriptor-only 浏览器**

在 `config.py` 增加 `source_browse_roots`，每项严格包含 `root_id`、`display_name`、`path`。路径仅进入 Agent 私有配置。

`source_browser.py`：

- 构造时以 `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` 打开并固定每个 root；
- 每个 session 保存随机 UUIDv4 和带 TTL 的 node 映射；
- 子目录用 `openat` 逐层打开，`fstat` 验证目录和权限；
- 枚举只返回 `node_id`、`name`、`has_android_markers`、`has_children`；
- 从不返回、记录或序列化 absolute path；
- 对 root inode/device 变化 fail closed；
- 页大小不超过 200，编码后响应不超过 128 KiB。

`source_registry.py` 增加只接受 `ResolvedSourceDirectory` 的注册入口，直接复用现有 registry 的 no-follow/Git/敏感文件保护。

- [ ] **Step 4: 运行完整 Agent source 单测**

Run:

~~~bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider -q \
  agents/device-agent/tests/unit/test_config.py \
  agents/device-agent/tests/unit/test_source_browser.py \
  agents/device-agent/tests/unit/test_source_registry.py \
  agents/device-agent/tests/unit/test_source_snapshot.py
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交 Agent 浏览器核心**

~~~bash
git add agents/device-agent/src/perfpilot_agent/config.py \
  agents/device-agent/src/perfpilot_agent/source_browser.py \
  agents/device-agent/src/perfpilot_agent/source_registry.py \
  agents/device-agent/tests
git commit -m "feat: browse authorized Agent source roots"
~~~

### Task 2: 增加签名 browse/register 任务合同

**Files:**
- Modify: `agents/device-agent/src/perfpilot_agent/source_models.py`
- Modify: `agents/device-agent/src/perfpilot_agent/service.py`
- Modify: `agents/device-agent/src/perfpilot_agent/control_client.py`
- Modify: `contracts/v1/agents/task-poll-response.schema.json`
- Modify: `contracts/v1/agents/task-snapshot.schema.json`
- Modify: `contracts/v1/examples/agent-task-poll.valid.json`
- Modify: `contracts/v1/examples/agent-task-snapshot.valid.json`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/security/task_snapshots.py`
- Test: `agents/device-agent/tests/integration/test_task_loop.py`
- Test: `agents/device-agent/tests/unit/test_security.py`
- Test: `services/api/tests/unit/test_agent_task_service.py`
- Test: `services/api/tests/contract/test_agent_contracts.py`

- [ ] **Step 1: 写签名、团队和过期 RED**

增加 `source_browse` 和 `source_register` 两种 task：

- v1.0 旧 device/source task 保持精确兼容；
- 新任务必须含 team、agent、browse session、root/node、expiry、nonce；
- Agent verifier 必须外部绑定 credentials.team_id 和 agent_id；
- 结果签名覆盖 exact raw JSON；
- 重放、过期、跨团队、跨 Agent、错误 root/node、额外字段均拒绝；
- browse response 最多 200 entries/128 KiB；
- register response 只返回公开 workspace descriptor。

- [ ] **Step 2: 运行合同 RED**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/contract/test_agent_contracts.py \
  services/api/tests/unit/test_agent_task_service.py \
  agents/device-agent/tests/unit/test_security.py \
  agents/device-agent/tests/integration/test_task_loop.py \
  -k 'source_browse or source_register'
~~~

Expected: FAIL，schema 和 TaskLoop 不认识新任务类型。

- [ ] **Step 3: 实现判别联合和 TaskLoop 分派**

合同使用 `task_type` 判别，不放宽旧分支。`TaskVerifier` 先验证 detached signature，再按类型构造严格模型。`TaskLoop` 将 browse/register 交给独立 runner；它们不访问 ADB、capture runner 或 source snapshot runner。

服务端 repository 按 `created_at, task_id` 确定性排队；同一 browse request 幂等；终态移出队列但保留短期 replay 记录。签名 completion 经 team/agent/task/lease 检查后才投影。

- [ ] **Step 4: 运行合同和 Agent 回归**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/contract/test_agent_contracts.py \
  services/api/tests/unit/test_agent_task_service.py \
  agents/device-agent/tests/unit/test_security.py \
  agents/device-agent/tests/integration/test_task_loop.py
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交任务协议**

~~~bash
git add agents/device-agent/src/perfpilot_agent/source_models.py \
  agents/device-agent/src/perfpilot_agent/service.py \
  agents/device-agent/src/perfpilot_agent/control_client.py \
  contracts/v1/agents/task-poll-response.schema.json \
  contracts/v1/agents/task-snapshot.schema.json \
  contracts/v1/examples/agent-task-poll.valid.json \
  contracts/v1/examples/agent-task-snapshot.valid.json \
  services/api/src/perfpilot_api/api/agent_control.py \
  services/api/src/perfpilot_api/security/task_snapshots.py \
  services/api/tests agents/device-agent/tests
git commit -m "feat: sign remote source browse tasks"
~~~

### Task 3: 持久化浏览会话和工作区绑定

**Files:**
- Modify: `services/api/src/perfpilot_api/services/source_workspaces.py`
- Modify: `services/api/src/perfpilot_api/local_agent_store.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/unit/test_source_workspaces.py`
- Test: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写网页浏览生命周期 RED**

使用两个团队和两个 Agent，断言：

- 只有 approved/online Agent 的授权根可列出；
- 创建 browse session 后可列 root 和一层 child；
- 其他团队、其他 Agent、过期 session 和旧 node ID 都返回统一 404/409；
- register selected node 后创建持久工作区绑定；
- 重启服务后绑定仍存在且可用于 analysis；
- API 响应和错误不含 root path、home path、token、argv；
- suspended/deleted/offline Agent 立即终止 browse，已有绑定保留但标 unavailable。

- [ ] **Step 2: 运行 API RED**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_source_workspaces.py \
  services/api/tests/integration/test_local_app.py \
  -k 'source_browse or selected_source_directory'
~~~

Expected: FAIL，browse session API 返回 404。

- [ ] **Step 3: 实现团队私有浏览服务**

增加闭合路由：

~~~text
GET  /v1/teams/{team_id}/agents/{agent_id}/source-roots
POST /v1/teams/{team_id}/agents/{agent_id}/source-browse-sessions
POST /v1/teams/{team_id}/source-browse-sessions/{session_id}/list
POST /v1/teams/{team_id}/source-browse-sessions/{session_id}/register
~~~

`SourceWorkspaceService` 只持有 root/node opaque ID，不接受 path 字符串。register completion 持久化 team、agent、workspace、root ID、node ID、display name、version；绝对路径永远只留在 Agent registry。所有接口使用当前 team authority 与 CSRF。

- [ ] **Step 4: 运行 source workspace 回归**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_source_workspaces.py \
  services/api/tests/integration/test_local_app.py \
  -k 'source_workspace or source_browse or team_private'
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交服务端浏览 API**

~~~bash
git add services/api/src/perfpilot_api/services/source_workspaces.py \
  services/api/src/perfpilot_api/local_agent_store.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_source_workspaces.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: register remote source directories"
~~~

### Task 4: 增加 Android 工程预检与包名匹配

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/source_preflight.py`
- Modify: `agents/device-agent/src/perfpilot_agent/source_models.py`
- Modify: `agents/device-agent/src/perfpilot_agent/source_runner.py`
- Modify: `services/api/src/perfpilot_api/reports/source_context.py`
- Modify: `services/api/src/perfpilot_api/services/source_tasks.py`
- Create: `agents/device-agent/tests/unit/test_source_preflight.py`
- Modify: `agents/device-agent/tests/unit/test_source_runner.py`
- Modify: `services/api/tests/unit/test_source_context.py`
- Modify: `services/api/tests/unit/test_source_task_service.py`

- [ ] **Step 1: 写 Manifest/Gradle/源码读取 RED**

创建最小 Android fixture，覆盖：

- Manifest package 和 launcher activity；
- Groovy/Kotlin DSL 的 literal `applicationId` 和 `namespace`；
- product flavor 多个 literal application ID；
- 变量、函数、插件和脚本不执行；
- 读取 `.kt/.java/.xml/.gradle/.gradle.kts` 白名单文本；
- 二进制、secret、symlink、submodule、超大文件排除；
- 返回工程指纹、候选 package IDs、activities、readable_source_count；
- readable_source_count 为 0 时不能 match；
- applicationId 精确匹配，大小写或相似字符串不匹配；
- activity 不属于工程时为 `activity_mismatch`；
- 所有被声明已读的文件都带 SHA-256，篡改后 completion 被拒绝。

- [ ] **Step 2: 运行预检 RED**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  agents/device-agent/tests/unit/test_source_preflight.py \
  agents/device-agent/tests/unit/test_source_runner.py \
  services/api/tests/unit/test_source_context.py \
  services/api/tests/unit/test_source_task_service.py \
  -k 'preflight or package_match or readable_source'
~~~

Expected: FAIL，`source_preflight` 模块或 completion 字段不存在。

- [ ] **Step 3: 实现只读工程预检**

`source_preflight.py` 使用现有 safe open/no-follow 基础设施，限定：

- 最多扫描 12,000 个目录项；
- 最多读取 2,000 个候选文本文件；
- 每个配置文件最多 1 MiB；
- 预检总读取最多 32 MiB；
- 最多返回 32 个 package/activity 候选；
- 不调用 shell、Gradle、Java、Kotlin 编译器或项目脚本。

工程指纹由 workspace snapshot hash、归一化 Manifest/Gradle 证据哈希和 source count 组成。结果不包含绝对路径，只含 relative evidence path 和 hash。

服务端 match authority 接收分析中的必填 package、可选 activity、test type：

- 至少一个 literal applicationId 精确匹配；
- activity 自动启动时必须属于 manifest/source candidate；
- readable_source_count 至少为 1；
- 验证每个 preflight evidence hash 和 snapshot binding；
- 输出 `available|package_mismatch|activity_mismatch|not_android_project|unreadable|agent_unavailable`。

只有 `available` 才继续执行现有 snapshot/rule/fragment 流程。

- [ ] **Step 4: 运行 source task 与安全回归**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  agents/device-agent/tests/unit/test_source_preflight.py \
  agents/device-agent/tests/unit/test_source_runner.py \
  agents/device-agent/tests/unit/test_source_snapshot.py \
  services/api/tests/unit/test_source_context.py \
  services/api/tests/unit/test_source_task_service.py
~~~

Expected: 全部 PASS，Agent 不调用 ADB 或任何构建命令。

- [ ] **Step 5: 提交预检与匹配**

~~~bash
git add agents/device-agent/src/perfpilot_agent/source_preflight.py \
  agents/device-agent/src/perfpilot_agent/source_models.py \
  agents/device-agent/src/perfpilot_agent/source_runner.py \
  services/api/src/perfpilot_api/reports/source_context.py \
  services/api/src/perfpilot_api/services/source_tasks.py \
  agents/device-agent/tests services/api/tests/unit
git commit -m "feat: verify Android source workspace identity"
~~~

### Task 5: 把匹配结果接入分析、报告和重启恢复

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/src/perfpilot_api/reports/writer.py`
- Modify: `services/api/src/perfpilot_api/ai/synthesis.py`
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Modify: `contracts/v1/reports/analysis-report.schema.json`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/tests/unit/test_analysis_contracts.py`
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`

- [ ] **Step 1: 写 matched/mismatch/offline RED**

三条真实流程：

1. package/activity 匹配且实际返回源码片段：报告可发布 path、symbol、多 Diff；
2. package mismatch：Trace、SmartPerfetto、中文 AI 各完成一次，最终分析绿色 completed，source 状态为 package_mismatch，refs/fixes 为空；
3. Agent offline/unreadable：其他分析继续，最终绿色 completed，对应中文提示。

另加服务重启发生在 preflight 前、completion 后、报告发布前的恢复测试，确保 source task 不重复、AI 只运行一轮。

- [ ] **Step 2: 运行分析 RED**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  -k 'package_mismatch or activity_mismatch or matched_source'
~~~

Expected: FAIL，合同缺少公开 match status，或 mismatch 仍被投影成 unavailable/partial。

- [ ] **Step 3: 扩展闭合合同和报告语义**

Analysis 公开 `source_code_analysis` 增加安全字段：

- `status`；
- `expected_package`；
- `matched_package`，非 match 时为空；
- `readable_source_count`；
- `message`；
- `match_summary`。

不公开 fingerprint 原始输入、绝对路径、Gradle 内容或 node ID。Report 1.2 的 source section 使用同一 status。

`local_app.py` 在 SmartPerfetto 之后等待 preflight/source completion。mismatch/unreadable/offline 只关闭 source 阶段，不把 analysis/report 标成 partial。writer 对这些状态固定生成中文提示，并强制 `source_refs=[]`、`source_fixes=[]`。synthesis validator 拒绝模型在非 available 状态输出路径、symbol、Diff 或声称读取源码。

- [ ] **Step 4: 运行报告和重启回归**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/unit/test_local_report.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交分析接线**

~~~bash
git add services/api/src/perfpilot_api/local_app.py \
  services/api/src/perfpilot_api/reports/writer.py \
  services/api/src/perfpilot_api/ai/synthesis.py \
  contracts/v1/analyses/analysis-response.schema.json \
  contracts/v1/reports/analysis-report.schema.json \
  services/api/tests
git commit -m "feat: report verified source workspace matches"
~~~

### Task 6: 实现网页目录选择器和状态提示

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/source-workspace-field.tsx`
- Modify: `app/components/new-analysis-dialog.tsx`
- Modify: `app/components/source-fixes-panel.tsx`
- Modify: `app/globals.css`
- Test: `tests/perfpilot-api.test.ts`
- Create: `tests/source-workspace-field.test.tsx`
- Modify: `tests/new-analysis-dialog.test.tsx`
- Modify: `tests/analysis-report.test.tsx`

- [ ] **Step 1: 写浏览和报告 UI RED**

覆盖：

- 仅列当前团队 approved/online Agent；
- 展开授权 root，只显示目录名称，不显示路径；
- 单击子目录只加载一层，支持返回上级但不能越过 root；
- 选择目录后立即创建绑定并提交分析；
- package name 仍为必填；
- pending/offline/expired 显示稳定中文错误；
- package mismatch 最终显示绿色“分析完成”和源码跳过提示；
- mismatch 响应即使恶意携带 relative_path/symbol/Diff，客户端也拒绝；
- 页面不出现 `source add --path` 命令或绝对路径输入框。

- [ ] **Step 2: 运行前端 RED**

Run:

~~~bash
npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/source-workspace-field.test.tsx \
  tests/new-analysis-dialog.test.tsx \
  tests/analysis-report.test.tsx
~~~

Expected: FAIL，客户端缺 browse API 或旧组件仍只显示固定 workspace 下拉框。

- [ ] **Step 3: 实现蓝白桌面目录浏览器**

`source-workspace-field.tsx` 显示 Agent、root 和目录三列或面包屑列表。状态由服务端轮询驱动；目录项只渲染 display name。选中目录调用 register，得到 workspace ID 后回填 `new-analysis-dialog` 并立即提交现有分析请求。

`perfpilot-api.ts` 对 root、entry、workspace、match status 使用 exact keys 和固定 enum；所有 unknown/private 字段拒绝。`source-fixes-panel.tsx` 只有 available/strong 渲染源码定位和 Diff，其余只显示 server-owned 中文状态。

- [ ] **Step 4: 运行前端完整门禁**

Run:

~~~bash
npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/source-workspace-field.test.tsx \
  tests/new-analysis-dialog.test.tsx \
  tests/analysis-report.test.tsx \
  tests/full-analysis-report.test.tsx
npm run lint
npm run build
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交前端**

~~~bash
git add app/lib/perfpilot-api.ts \
  app/components/source-workspace-field.tsx \
  app/components/new-analysis-dialog.tsx \
  app/components/source-fixes-panel.tsx \
  app/globals.css \
  tests
git commit -m "feat: select verified remote source directories"
~~~

### Task 7: 增加跨层隐私与成功/降级验收

**Files:**
- Create: `services/api/tests/acceptance/test_remote_source_browser.py`
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`
- Modify: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: 写跨层验收 RED**

使用两个团队、两个真实 Agent TaskLoop 和临时 Android 工程：

- team A 只能浏览 A 的授权 root；
- team B 不能看到 root、node、workspace、analysis 或 report；
- matched 工程真实读取 Manifest、Gradle 和 Kotlin/Java 源码，hash 对齐，报告发布源码根因和 Diff；
- mismatched 工程仍生成 Trace/SmartPerfetto/中文总结，最终绿色 completed；
- unreadable/symlink/root swap 被拒绝且其他分析继续；
- Agent 离线后 browse timeout bounded；
- 全部浏览、分析和错误响应扫描不到临时 absolute path；
- server/browser 侧 host ADB probe 为 0；
- 服务与 Agent 各重启一次后，绑定和最终状态一致且任务不重复。

- [ ] **Step 2: 运行验收并确认 RED**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/acceptance/test_remote_source_browser.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py
~~~

Expected: 首次运行在缺少验收文件或浏览全链路处 FAIL。

- [ ] **Step 3: 修正验收发现的最小生产缺口**

只修验收实际暴露的问题；每个修复先保留精确 RED，再做最小 GREEN。禁止在本步骤增加新产品范围。

- [ ] **Step 4: 运行最终总门禁**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests \
  agents/device-agent/tests

npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/source-workspace-field.test.tsx \
  tests/new-analysis-dialog.test.tsx \
  tests/analysis-report.test.tsx \
  tests/full-analysis-report.test.tsx

.venv/bin/ruff check services/api/src services/api/tests agents/device-agent/src agents/device-agent/tests
npm run lint
npm run build
git diff --check
~~~

Expected: 全部 PASS；只有 unavailable 的外部 Postgres 测试可以按现有标记 skip。

- [ ] **Step 5: 提交验收**

~~~bash
git add services/api/tests/acceptance/test_remote_source_browser.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  tests/perfpilot-api.test.ts
git commit -m "test: verify remote source browsing and matching"
~~~

## 完成定义

- 网页只能浏览 Agent 明确授权 root 的一层子目录，服务器从未得到远程绝对路径。
- 目录选择、工作区注册、分析创建和重启恢复均保持 team/Agent 绑定。
- 分析一定实际读取 Manifest、Gradle 配置和至少一个源码文件后才可能标记 source available。
- 测试包名必须与 literal applicationId 精确匹配；自动启动时 Activity 也必须属于工程。
- Agent 不执行 Gradle、shell、构建脚本或用户源码。
- 匹配成功才发布相对路径、symbol、根因和 Diff。
- mismatch、unreadable 和 offline 只跳过源码阶段，Trace、SmartPerfetto 和中文总结继续，最终状态绿色“分析完成”。
- 跨团队、symlink、root swap、过期 node 和篡改 hash 全部 fail closed。
