# Agent 审核与自动注册 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户一键申请 Agent，管理员审核后 Agent 自动领取正式凭据并连接；同时支持停用、恢复和删除，且所有状态可跨服务与 Agent 重启恢复。

**Architecture:** 在现有 `LocalAgentStore` 中增加闭合、持久化的 Agent 申请状态和待领取凭据摘要。浏览器一次点击只打开一个待认领槽位；下一台尚未注册、正通过出站 HTTPS 连接该服务器的 Agent 自动认领它并持有短期轮询凭据，不要求用户复制代码或再执行命令。管理员批准后，现有 `AgentService` 在原子事务内创建正式 Agent 身份，Agent 从轮询接口领取一次正式凭据。所有 heartbeat、task、artifact 和 source 边界继续以正式 Agent 凭据为唯一授权依据。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、Ed25519、Argon2/SHA-256 摘要、文件描述符固定 JSON 仓库、Agent HTTP 客户端、Next.js/React、TypeScript、pytest、Vitest。

**Prerequisite:** 先完成 `2026-08-20-admin-registration-implementation.md`；本计划复用其中创建的 `admin-console.tsx` 与 `admin-console.test.tsx`。

---

## 文件结构

- `services/api/src/perfpilot_api/local_agent_store.py`：Agent 申请、轮询凭据摘要、正式身份和状态事务。
- `services/api/src/perfpilot_api/local_agent_enrollment.py`：把一次性全局槽改成可恢复的申请认领协调器。
- `services/api/src/perfpilot_api/services/agents.py`：批准、停用、恢复、删除和正式凭据领取。
- `services/api/src/perfpilot_api/local_app.py`：普通用户申请、管理员审核和 Agent 轮询接口。
- `agents/device-agent/src/perfpilot_agent/control_client.py`：pending 响应、状态轮询和正式凭据领取。
- `agents/device-agent/src/perfpilot_agent/credentials.py`：待审核身份的 0600 持久化与恢复。
- `agents/device-agent/src/perfpilot_agent/service.py`：只有 approved 身份才能 heartbeat、poll 和执行任务。
- `app/lib/perfpilot-api.ts`：Agent 申请与管理后台闭合合同。
- `app/components/agent-management.tsx`：普通用户 Agent 状态和一键申请。
- `app/components/admin-console.tsx`：Agent 审核页签。
- 对应 `services/api/tests`、`agents/device-agent/tests` 与 `tests` 下的测试文件。

### Task 1: 持久化 Agent 申请和待审核认领

**Files:**
- Modify: `services/api/src/perfpilot_api/local_agent_store.py`
- Modify: `services/api/src/perfpilot_api/local_agent_enrollment.py`
- Test: `services/api/tests/unit/test_local_agent_store.py`
- Test: `services/api/tests/unit/test_local_agent_enrollment.py`

- [ ] **Step 1: 写申请状态、重启和并发 RED**

新增测试覆盖：

- 普通用户创建 `pending` 申请后只得到安全的申请 ID，不显示注册码或启动令牌；
- 同一时刻最多存在一个未认领槽位，下一台未注册 Agent 的 register 请求自动认领；认领完成后立即释放全局槽位，其他已认领 pending 申请可并存；
- Agent 认领后，服务器只保存轮询令牌摘要，不保存原始令牌；
- 重启 `LocalAgentStore` 后，Agent 能用原轮询令牌继续查询；
- 并发 register 只能由一台 Agent 绑定该申请的 public key；
- 管理员创建申请时走同一状态机并直接进入 `approved`；
- 拒绝、停用、删除均幂等；
- 删除后旧正式凭据与轮询凭据都无效；
- 不同团队不能读取或认领对方申请。

测试文档必须断言没有 `access_token`、`refresh_token`、`poll_token`、绝对路径或私钥。

- [ ] **Step 2: 运行仓库测试并确认 RED**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_agent_store.py \
  services/api/tests/unit/test_local_agent_enrollment.py \
  -k 'application or pending_claim or approval or deleted'
~~~

Expected: FAIL，首个失败为申请仓库方法或状态模型不存在。

- [ ] **Step 3: 实现闭合状态和原子事务**

在 `local_agent_store.py` 增加不可变公开视图 `AgentApplicationView`，只包含：

- `application_id`、`team_id`、`requested_by_user_id`；
- `display_name`；
- `status`：`pending|approved|rejected|suspended|deleted`；
- 安全环境摘要：`hostname`、`platform`、`architecture`；
- `created_at`、`claimed_at`、`decided_at`；
- approved 时的 `agent_id`。

持久化私有记录额外保存 public key、轮询令牌摘要和领取状态。所有令牌使用 SHA-256 摘要比较；随机令牌至少 32 bytes。未认领槽位的唯一权威是持久化申请状态，不依赖进程内全局变量。

实现这些事务边界：

- `create_application(team_id, requested_by_user_id, display_name, auto_approve)`；
- `claim_open_application(public_key, environment)`；
- `poll_application(application_id, poll_token)`；
- `approve_application(actor_admin_id, application_id)`；
- `reject_application(actor_admin_id, application_id)`；
- `suspend_agent(actor_admin_id, agent_id)`；
- `reactivate_agent(actor_admin_id, agent_id)`；
- `delete_agent(actor_user_id, agent_id, require_admin)`。

批准事务必须同时写 approved 状态、正式 Agent 记录和一组待领取凭据；重复批准返回同一 Agent，不重复生成。删除事务同时撤销 refresh credentials、active leases 和待领取凭据。

将 `local_agent_enrollment.py` 改为协调唯一未认领槽位，不再持有唯一权威状态；进程重启后从仓库恢复。若已有未认领申请，第二个用户的“添加 Agent”返回稳定冲突并提示等待当前 Agent 完成认领；槽位被认领后可立即创建下一申请。

- [ ] **Step 4: 运行完整仓库回归**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_agent_store.py \
  services/api/tests/unit/test_local_agent_enrollment.py \
  services/api/tests/unit/test_agent_service.py
~~~

Expected: 全部 PASS，现有 rename、refresh、unregister、device directory 和锁安全测试不回归。

- [ ] **Step 5: 提交持久化层**

~~~bash
git add services/api/src/perfpilot_api/local_agent_store.py \
  services/api/src/perfpilot_api/local_agent_enrollment.py \
  services/api/tests/unit/test_local_agent_store.py \
  services/api/tests/unit/test_local_agent_enrollment.py
git commit -m "feat: persist Agent approval applications"
~~~

### Task 2: 接入浏览器申请、管理员审核和 Agent 轮询 API

**Files:**
- Modify: `services/api/src/perfpilot_api/services/agents.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写完整 API 生命周期 RED**

新增集成测试，顺序固定为：

1. 普通用户调用 `POST /v1/teams/{team_id}/agent-applications`；
2. Agent 调用 `POST /v1/agent/applications/claim`；
3. pending Agent 调 heartbeat/tasks/input/upload/source/browse 均为 401 或 403；
4. 普通用户不能批准；
5. 管理员批准；
6. Agent 状态轮询一次领取正式 credentials；
7. Agent 用正式 access token heartbeat 并出现在申请人团队；
8. 其他团队看不到该 Agent；
9. 管理员停用后旧 token 立即失败；
10. 恢复后必须用新 token；
11. 删除后 refresh、heartbeat、task poll 和待领取凭据全部失败。

另加管理员创建申请立即 approved 的测试，仍断言只创建一份 Agent 记录。

- [ ] **Step 2: 运行 API 测试并确认 RED**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  -k 'agent_application_approval or pending_agent_cannot'
~~~

Expected: FAIL，申请或 claim 路由返回 404。

- [ ] **Step 3: 实现闭合 API**

增加路由：

~~~text
POST /v1/teams/{team_id}/agent-applications
GET  /v1/teams/{team_id}/agent-applications
POST /v1/agent/applications/claim
POST /v1/agent/applications/status

GET  /v1/admin/agent-applications
POST /v1/admin/agent-applications/{application_id}/approve
POST /v1/admin/agent-applications/{application_id}/reject
POST /v1/admin/agents/{agent_id}/suspend
POST /v1/admin/agents/{agent_id}/reactivate
DELETE /v1/admin/agents/{agent_id}
~~~

浏览器写接口必须通过 CSRF 和 team/admin 权限。Agent claim/status 接口禁止浏览器 cookie，只接受闭合 JSON。pending status 返回 `pending` 和 bounded retry interval；approved status 只允许正式 credentials 被同一 poll token 领取，重复领取返回相同未过期身份或明确重新注册状态，不能创建第二身份。

把停用和删除接到现有 Agent credential、lease、task、source task、artifact access 的统一撤销路径。错误统一为稳定代码，不泄漏申请是否属于其他团队。

- [ ] **Step 4: 运行 Agent API 和任务门禁回归**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/unit/test_agent_task_service.py \
  services/api/tests/unit/test_source_task_service.py \
  -k 'agent or credential or lease or task or source'
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交服务端 API**

~~~bash
git add services/api/src/perfpilot_api/services/agents.py \
  services/api/src/perfpilot_api/local_app.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: approve local Agent registrations"
~~~

### Task 3: 让 Agent 自动等待并领取正式凭据

**Files:**
- Modify: `agents/device-agent/src/perfpilot_agent/control_client.py`
- Modify: `agents/device-agent/src/perfpilot_agent/credentials.py`
- Modify: `agents/device-agent/src/perfpilot_agent/service.py`
- Modify: `agents/device-agent/src/perfpilot_agent/cli.py`
- Create: `agents/device-agent/tests/unit/test_control_client.py`
- Modify: `agents/device-agent/tests/unit/test_credentials.py`
- Modify: `agents/device-agent/tests/unit/test_registration.py`
- Modify: `agents/device-agent/tests/integration/test_task_loop.py`
- Modify: `agents/device-agent/tests/unit/test_cli.py`

- [ ] **Step 1: 写 pending、批准、拒绝和重启 RED**

测试真实 Agent 注册循环：

- `register` 得到 pending 后，将 application ID、poll token 和 server origin 以 0600 保存；
- 等待期间只调用 status，不 heartbeat、不 poll task；
- 服务器返回 approved credentials 后原子替换 pending 文件；
- Agent 进程重启后继续等待，不要求用户再次执行命令；
- rejected/deleted 清理 pending 文件并输出稳定中文错误；
- 网络失败按现有 bounded backoff 重试；
- 日志中不出现 bootstrap/poll/access/refresh token。

- [ ] **Step 2: 运行 Agent RED**

Run:

~~~bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider -q \
  agents/device-agent/tests/unit/test_control_client.py \
  agents/device-agent/tests/unit/test_credentials.py \
  agents/device-agent/tests/unit/test_registration.py \
  agents/device-agent/tests/integration/test_task_loop.py \
  agents/device-agent/tests/unit/test_cli.py \
  -k 'pending or approval or registration_restart'
~~~

Expected: FAIL，客户端模型拒绝 pending 响应或缺少 pending credential store。

- [ ] **Step 3: 实现闭合响应与恢复循环**

在 `control_client.py` 增加严格判别联合：

- `AgentApplicationPendingResponse`；
- `AgentApplicationApprovedResponse`；
- `AgentApplicationRejectedResponse`。

`credentials.py` 增加独立 pending 文档，exact keys、最大 16 KiB、0600、no-follow、原子 replace。`service.py` 启动时优先恢复 pending enrollment，只有获得正式 `AgentCredentials` 后才启动 heartbeat 和 task loop。`cli.py` 输出一句可执行状态，不要求复制第二条命令。

- [ ] **Step 4: 运行完整 Agent 测试**

Run:

~~~bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider -q \
  agents/device-agent/tests
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交 Agent 客户端**

~~~bash
git add agents/device-agent/src/perfpilot_agent/control_client.py \
  agents/device-agent/src/perfpilot_agent/credentials.py \
  agents/device-agent/src/perfpilot_agent/service.py \
  agents/device-agent/src/perfpilot_agent/cli.py \
  agents/device-agent/tests
git commit -m "feat: await Agent registration approval"
~~~

### Task 4: 增加普通用户和管理员 Agent 界面

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/agent-management.tsx`
- Modify: `app/components/admin-console.tsx`
- Modify: `app/globals.css`
- Test: `tests/perfpilot-api.test.ts`
- Test: `tests/agent-management.test.tsx`
- Test: `tests/admin-console.test.tsx`

- [ ] **Step 1: 写前端合同和交互 RED**

覆盖：

- 普通用户点击“添加 Agent”只需输入名称并确认；
- pending 显示“等待管理员审核”，claimed 显示“已连接，等待审核”；
- approved 显示在线状态；
- 用户可删除自己的 Agent；
- 管理员页可批准、拒绝、停用、恢复、删除；
- 普通用户看不到管理操作；
- 所有响应附加 token、absolute_path、private_key 或未知字段时拒绝。

- [ ] **Step 2: 运行 Vitest 并确认 RED**

Run:

~~~bash
npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/agent-management.test.tsx \
  tests/admin-console.test.tsx
~~~

Expected: FAIL，客户端缺少 Agent application 方法或界面缺少审核状态。

- [ ] **Step 3: 实现蓝白桌面 UI**

`agent-management.tsx` 删除多步注册说明，改成名称输入、一次确认、状态卡和删除操作。`admin-console.tsx` 的 Agent 页签显示申请人、Agent 名、安全平台摘要和状态操作。所有 mutation 成功后重新拉取权威列表；失败保留稳定中文错误。

- [ ] **Step 4: 运行前端回归**

Run:

~~~bash
npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/agent-management.test.tsx \
  tests/admin-console.test.tsx \
  tests/perfpilot-session-provider.test.tsx
npm run lint
npm run build
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交前端**

~~~bash
git add app/lib/perfpilot-api.ts \
  app/components/agent-management.tsx \
  app/components/admin-console.tsx \
  app/globals.css \
  tests/perfpilot-api.test.ts \
  tests/agent-management.test.tsx \
  tests/admin-console.test.tsx
git commit -m "feat: manage approved Agents"
~~~

### Task 5: 增加端到端验收和故障恢复门禁

**Files:**
- Create: `services/api/tests/acceptance/test_agent_approval.py`
- Modify: `scripts/bootstrap-local-users.py`
- Modify: `tests/ubuntu-user-deployment.test.ts`

- [ ] **Step 1: 写验收 RED**

验收必须走真实 API 和 Agent 客户端：

- 普通用户申请、Agent claim、管理员批准、Agent 自动 heartbeat；
- pending 期间所有设备/任务/源码能力均不可用；
- 服务重启发生在批准前和批准后未领取凭据两种位置，流程均继续；
- suspended/deleted 立即断开且不能 refresh；
- 管理员创建 Agent 自动批准；
- team A/B 申请、凭据、设备和任务严格隔离；
- 数据重置脚本只清分析数据，不清用户、Agent 申请和正式 Agent。

- [ ] **Step 2: 运行验收并确认 RED**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/acceptance/test_agent_approval.py
~~~

Expected: 首次运行在缺少验收文件或完整自动领取路径处 FAIL。

- [ ] **Step 3: 补齐 bootstrap 与 reset 兼容**

确认 bootstrap 管理员保持 active，普通预置用户保持 active。reset 脚本继续只替换 `teams/<team>/analyses`，不得删除 `control.json`、Agent store、Agent application 或 credential state。增加 exact byte digest 断言。

- [ ] **Step 4: 运行最终门禁**

Run:

~~~bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest \
  -p no:cacheprovider -q \
  services/api/tests/unit/test_local_agent_store.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_agent_approval.py \
  agents/device-agent/tests

npx vitest run \
  tests/perfpilot-api.test.ts \
  tests/agent-management.test.tsx \
  tests/admin-console.test.tsx \
  tests/ubuntu-user-deployment.test.ts

.venv/bin/ruff check services/api/src services/api/tests agents/device-agent/src agents/device-agent/tests
npm run lint
npm run build
git diff --check
~~~

Expected: 全部 PASS；没有 token、绝对路径或跨团队数据进入响应。

- [ ] **Step 5: 提交验收**

~~~bash
git add services/api/tests/acceptance/test_agent_approval.py \
  scripts/bootstrap-local-users.py \
  tests/ubuntu-user-deployment.test.ts
git commit -m "test: verify approved Agent lifecycle"
~~~

## 完成定义

- 普通用户只点击一次“添加 Agent”，不复制注册码、不执行第二条命令。
- 待审核 Agent 只能轮询申请状态，不能上报设备或领取任何任务。
- 管理员批准后 Agent 自动获得唯一正式身份并开始 heartbeat。
- 管理员创建的 Agent 走同一状态机但自动批准。
- 普通用户可删除自己的 Agent；管理员可审核、停用、恢复和删除任何普通用户 Agent。
- 停用和删除立即使旧 credentials、lease、task 与 artifact 权限失效。
- 服务端和 Agent 重启不会丢失 pending/approved 状态，也不会创建重复 Agent。
- 管理 API 与页面不泄漏 token、私钥、绝对路径或跨团队数据。
