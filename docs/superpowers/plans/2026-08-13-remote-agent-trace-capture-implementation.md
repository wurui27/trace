# 远程 Agent 真机 Trace 采集实施计划

> 设计依据：`docs/superpowers/specs/2026-08-13-remote-agent-trace-capture-design.md`

## 目标

让本地多用户网页把设备分析投递给所选远程 Agent，由 Agent 在真实 Android 设备上抓取冷启动和滑动 Perfetto Trace，再进入 SmartPerfetto、可选源码分析和一次中文 PerfPilot 总结。首版明确不执行 Android Memory。

## 执行原则

- 严格 TDD：每个任务先补失败测试，再写最小实现。
- 复用现有签名任务、租约、续租、取消、Agent capture runner 和 source task 协议。
- 浏览器/API/磁盘不得暴露绝对路径、设备 serial、token 或 lease。
- `memory_cycle` 仅在分析状态页显示 `not_requested`；最终 1.2 报告只包含实际执行的 startup 和 scroll，不制造内存失败结论。
- 任务签名只允许 `startup_trace`、`scroll_trace`、`agent_log`，不授权 `memory_evidence`。
- 一个 Trace 成功即可继续；两个均失败时不调用 AI。

---

## Task 1：关闭首版场景合同与前端状态

**修改文件**

- `contracts/v1/analyses/analysis-response.schema.json`
- `services/api/src/perfpilot_api/local_app.py`
- `services/api/tests/unit/test_analysis_contracts.py`
- `services/api/tests/integration/test_local_app.py`
- `app/lib/perfpilot-api.ts`
- `app/components/active-analysis-task-card.tsx`
- `tests/perfpilot-api.test.ts`
- `tests/active-analysis-task-card.test.tsx`

**RED**

1. 新建设备分析响应断言：`cold_start`、`scroll` 为排队/执行状态；`memory_cycle.state == "not_requested"`。
2. Trace 模式仍不得接受 `not_requested`。
3. TS 客户端严格接受新增状态并拒绝未知状态/额外私有字段。
4. 主任务卡显示“内存分析暂未执行”，不显示失败样式。

运行：

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/integration/test_local_app.py -k 'not_requested or memory_cycle'
npx vitest run tests/perfpilot-api.test.ts tests/active-analysis-task-card.test.tsx
```

**GREEN**

- 仅 device 1.1 响应允许 `not_requested`。
- 创建远程设备分析时固定投影三行；内存行没有 progress、failure 或伪指标。
- 保留旧 1.0/1.1 解析兼容。

**提交**

```bash
git add contracts/v1/analyses/analysis-response.schema.json services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_analysis_contracts.py services/api/tests/integration/test_local_app.py \
  app/lib/perfpilot-api.ts app/components/active-analysis-task-card.tsx \
  tests/perfpilot-api.test.ts tests/active-analysis-task-card.test.tsx
git commit -m "feat: mark remote memory capture not requested"
```

---

## Task 2：创建精确绑定的签名设备任务

**修改文件**

- `services/api/src/perfpilot_api/services/agent_tasks.py`
- `services/api/src/perfpilot_api/services/device_directory.py`
- `services/api/src/perfpilot_api/local_agent_store.py`
- `services/api/src/perfpilot_api/local_app.py`
- `services/api/tests/unit/test_agent_task_service.py`
- `services/api/tests/unit/test_local_agent_store.py`
- `services/api/tests/integration/test_local_app.py`

**RED**

1. user01 只能把任务投递给 user01 的 online/ready Agent 设备；跨团队、离线、busy 均拒绝且零任务。
2. `AgentTaskDefinition` 只含 startup、scroll，顺序固定。
3. 签名 claims 绑定 team、agent、device digest、analysis、APK artifact。
4. allowed uploads 根据场景派生为 startup_trace、scroll_trace、agent_log；提交 memory_evidence 必须拒绝。
5. 同一 Agent 只能持有一个 capture/source lease；旧 token、旧 version 和过期 lease 均不能复活。
6. Agent 领取任务后设备投影 busy；完成、取消或租约终结后按最新心跳恢复 ready/offline。

运行：

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_agent_task_service.py \
  services/api/tests/unit/test_local_agent_store.py \
  services/api/tests/integration/test_local_app.py -k 'remote_device or device_task or allowed_uploads'
```

**GREEN**

- 为内存仓库增加线程安全/异步安全的 enqueue 与 oldest queued 选择。
- DeviceDirectory 增加不公开 serial 的私有 task target 查询。
- 任务创建、poll 和 complete 重复校验团队与 device digest。
- 设备 busy/ready 状态通过本地 Agent store 持久化投影，不依赖服务器 ADB。

**提交**

```bash
git add services/api/src/perfpilot_api/services/agent_tasks.py \
  services/api/src/perfpilot_api/services/device_directory.py \
  services/api/src/perfpilot_api/local_agent_store.py services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_agent_task_service.py services/api/tests/unit/test_local_agent_store.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: queue tenant-bound remote capture tasks"
```

---

## Task 3：实现本地 APK 输入与 Trace multipart 制品服务

**新增文件**

- `services/api/src/perfpilot_api/local_agent_artifacts.py`
- `services/api/tests/unit/test_local_agent_artifacts.py`

**修改文件**

- `services/api/src/perfpilot_api/local_app.py`
- `services/api/tests/integration/test_local_app.py`

**RED**

覆盖：

- APK 只能由匹配 execution/agent/team/lease 下载；
- multipart 创建、分片 PUT、断点查询和 complete；
- 分片与最终文件 512 MiB 上限、SHA-256、MIME、artifact type 校验；
- 错团队、错 lease、过期 lease、错 part、错 checksum 拒绝；
- 临时文件/完成文件 `0600`，目录 `0700`，符号链接/FIFO/根替换拒绝；
- 取消删除未完成分片，已完成制品不可覆盖；
- 错误和响应不包含绝对路径、serial、token；
- 重复同一 complete 幂等，不同 manifest 冲突。

运行：

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q \
  services/api/tests/unit/test_local_agent_artifacts.py \
  services/api/tests/integration/test_local_app.py -k 'agent_input or multipart or remote_artifact'
```

**GREEN**

- 实现现有 agent control router 需要的 `authorize_input/create_upload/authorize_part/complete_upload/validate_completion/project_completion/abort_execution/project_cancellation` 边界。
- 增加受 lease 约束的本地 GET/PUT 路由，PUT 返回 ETag。
- 所有路径操作使用固定租户分析根、no-follow 打开、临时文件加原子 replace。

**提交**

```bash
git add services/api/src/perfpilot_api/local_agent_artifacts.py services/api/src/perfpilot_api/local_app.py \
  services/api/tests/unit/test_local_agent_artifacts.py services/api/tests/integration/test_local_app.py
git commit -m "feat: transfer private remote capture artifacts"
```

---

## Task 4：APK 解析、finalize 投递与取消闭环

**修改文件**

- `services/api/src/perfpilot_api/local_device_capture.py`
- `services/api/src/perfpilot_api/local_app.py`
- `services/api/tests/unit/test_local_device_capture.py`
- `services/api/tests/integration/test_local_app.py`
- `agents/device-agent/src/perfpilot_agent/capture.py`
- `agents/device-agent/tests/unit/test_capture.py`

如实际代码仍位于 `local_app.py`，不为了拆文件而做无关重构。

**RED**

1. APK finalize 前零 aapt2、零任务。
2. finalize 后从私有 APK 调用 `aapt2 dump badging`，解析 package、launch activity、版本；失败得到 `apk_metadata_invalid` 且零任务。
3. 服务端只解析 aapt2，不解析/连接服务器 ADB。
4. Agent poll 得到签名任务，真实 `CaptureTaskRunner` 下载 APK 并只运行 startup、scroll；ADB resolver 在任务执行前零调用。
5. 用户取消 queued/leased/running 分别得到稳定状态；运行中 Agent 停止 Perfetto、清理工作目录并 ack。
6. 分析状态按 queued → scheduled → running → analyzing 推进，dialog 仍在提交成功后立即关闭。

运行：

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest -q \
  services/api/tests/unit/test_local_device_capture.py \
  services/api/tests/integration/test_local_app.py \
  agents/device-agent/tests/unit/test_capture.py -k 'aapt2 or remote_capture or cancel'
```

**GREEN**

- 把 aapt2 独立解析为不要求 ADB 的 resolver，Ubuntu 继续发现 Android SDK build-tools。
- finalize 的持久化成功后才发布任务；发布失败回滚/稳定失败，不留下 ghost task。
- 任务完成观察器验证 manifest 后唤醒分析流水线。

**提交**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/src/perfpilot_api/local_device_capture.py \
  services/api/tests/unit/test_local_device_capture.py services/api/tests/integration/test_local_app.py \
  agents/device-agent/src/perfpilot_agent/capture.py agents/device-agent/tests/unit/test_capture.py
git commit -m "feat: execute remote Android trace captures"
```

---

## Task 5：接入 SmartPerfetto、源码分析和双报告

**修改文件**

- `services/api/src/perfpilot_api/local_app.py`
- `services/api/src/perfpilot_api/reports/writer.py`
- `services/api/tests/integration/test_local_app.py`
- `services/api/tests/acceptance/test_source_aware_report_flow.py`
- `app/components/analysis-report.tsx`
- `tests/analysis-report.test.tsx`

**RED**

1. 两个 Trace 成功：SmartPerfetto 收到两份真实 bytes，PerfPilot provider 只调用一次。
2. 一个场景成功：只分析成功 Trace，最终 report 为 partially_completed 并明确缺失场景。
3. 两个场景失败：不调用 SmartPerfetto/AI，保留脱敏 Agent 诊断。
4. 最终 AnalysisReport 1.2 只含 startup、scroll 两个真实 scenario，不加入虚假的 memory failure。
5. 有 strong 源码：报告给相对路径、symbol、Unified Diff、复测方案；weak/none 不显示路径/行号/Diff。
6. 自然语言为简体中文；标准术语、代码和 Diff 保持原文。
7. PerfPilot 报告突出源码根因/改法；SmartPerfetto 原始报告独立打开、查看和下载，不与 PerfPilot 内容混写。
8. 用户/团队隔离覆盖报告、Trace、原始报告和源码上下文。

运行：

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest -q \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py -k 'remote or device or original'
npx vitest run tests/analysis-report.test.tsx tests/full-analysis-report.test.tsx
```

**GREEN**

- 复用现有 `_prepare_local_report`/source task/writer，但让远程 device 模式使用实际成功场景集合。
- 固定一次逻辑 AI round；仅契约失败允许一次 bounded retry。
- 原始报告继续使用租户绑定、checksum 绑定和安全下载；不返回内部路径。

**提交**

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/src/perfpilot_api/reports/writer.py \
  services/api/tests/integration/test_local_app.py services/api/tests/acceptance/test_source_aware_report_flow.py \
  app/components/analysis-report.tsx tests/analysis-report.test.tsx
git commit -m "feat: report remote trace capture results"
```

---

## Task 6：全链路验收、推送与 Ubuntu 部署

**新增或修改**

- `services/api/tests/acceptance/test_remote_agent_capture.py`
- `tests/ubuntu-user-deployment.test.ts`
- 必要的部署 README/systemd 环境配置

**验收场景**

1. 两个本地团队、两个 Agent、两台设备；互相不可见。
2. 浏览器创建 device 分析、上传 APK、finalize。
3. Agent 验证签名任务并以真实 capture runner 完成两份 Trace 上传。
4. SmartPerfetto 双 Trace、源码 strong、一次中文 AI、PerfPilot 1.2 与原始报告均可打开/下载。
5. 一个场景失败仍产出部分报告。
6. 取消清理未完成上传。
7. 统一重启清空所有分析/APK/Trace/报告，无 backup；账户、Agent、workspace 保留。

**完整门禁**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -q services/api/tests
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -q agents/device-agent/tests
npm run lint
npm run build
npx vitest run
git diff --check
```

记录任何已证实与本改动无关的基线失败，不为通过门禁修改无关功能。

**提交与发布**

```bash
git add services/api/tests/acceptance/test_remote_agent_capture.py tests/ubuntu-user-deployment.test.ts
git commit -m "test: verify remote Agent trace capture"
git push origin main
```

Ubuntu：

```bash
cd /home/rivotek/perfpilot/platform-web
git pull --ff-only origin main
systemctl --user restart perfpilot.target
systemctl --user --no-pager --full status perfpilot-reset.service perfpilot-api.service perfpilot-web.service
```

部署后用 user01 登录，确认 Agent online/ready、设备可选；提交真实 APK 后确认 startup/scroll、PerfPilot 报告和 SmartPerfetto 原始报告。重启一次并确认历史分析被永久清空、无备份目录。

## 完成定义

- 网页不再返回 `remote_device_capture_unavailable` 作为正常远程设备路径。
- 远程 Agent 真机 startup/scroll Trace 闭环可用。
- Android Memory 明确为未执行，不伪造失败或数据。
- 一个 Trace 成功即可生成部分报告，两个失败不调用 AI。
- PerfPilot 报告以源码优化为核心，SmartPerfetto 原始报告独立存在。
- 多用户、设备、源码、APK、Trace、报告严格隔离。
- 代码已提交、推送 main，并在 Ubuntu 重启部署通过健康检查。
