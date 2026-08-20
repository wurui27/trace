# 账号注册与管理后台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加用户自助注册、管理员审核、用户停用/恢复和蓝白桌面管理后台，同时保持现有本地账号、团队与会话兼容。

**Architecture:** 扩展现有 `LocalControlStore` 闭合文档，在同一文件锁和原子写事务内保存注册申请、用户状态与团队绑定。`local_app.py` 提供注册和管理员接口；前端复用现有会话 Provider，并通过服务端 `is_platform_admin` 门禁保护 `/admin`。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、Argon2、文件描述符固定的 JSON 仓库、Next.js/React、TypeScript、Vitest、pytest。

---

## 文件结构

- `services/api/src/perfpilot_api/local_control_store.py`：注册申请、用户状态、审批事务和会话撤销的持久化权威。
- `services/api/src/perfpilot_api/local_app.py`：闭合请求模型、注册接口、管理员接口和服务端权限门禁。
- `services/api/tests/unit/test_local_control_store.py`：迁移、并发、权限和私密持久化测试。
- `services/api/tests/integration/test_local_app.py`：注册、登录状态、审核和跨用户访问集成测试。
- `app/lib/perfpilot-api.ts`：严格 TypeScript 合同、响应校验和客户端方法。
- `app/components/perfpilot-session-provider.tsx`：向应用公开当前用户和管理员标记。
- `app/components/local-login.tsx`：登录/注册切换和待审核文案。
- `app/components/app-shell.tsx`：头像菜单与管理员入口。
- `app/components/admin-console.tsx`：账号审核桌面端主体。
- `app/admin/page.tsx`：管理后台路由。
- `app/globals.css`：蓝白管理后台样式。
- `tests/perfpilot-api.test.ts`、`tests/local-login.test.tsx`、`tests/admin-console.test.tsx`、`tests/app-shell-device.test.tsx`：前端合同与交互测试。

### Task 1: 扩展本地账号状态与注册申请仓库

**Files:**
- Modify: `services/api/src/perfpilot_api/local_control_store.py`
- Test: `services/api/tests/unit/test_local_control_store.py`

- [ ] **Step 1: 写注册、审核、停用和迁移失败测试**

在 `test_local_control_store.py` 增加以下行为测试。测试使用确定 UUID，断言批准只创建一个用户和团队，原始密码不落盘，停用立即撤销会话。

~~~python
def test_registration_approval_is_atomic_private_and_idempotent(tmp_path: Path) -> None:
    ids = iter((
        UUID("82000000-0000-4000-8000-000000000001"),
        UUID("80000000-0000-4000-8000-000000000010"),
        UUID("81000000-0000-4000-8000-000000000010"),
    ))
    store = LocalControlStore(tmp_path, uuid_factory=lambda: next(ids))
    admin = store.ensure_user("admin", "admin safe password", True).principal
    application = store.submit_registration(
        username="new_user",
        display_name="新用户",
        password="new user safe password",
        application_reason="负责启动性能分析",
    )

    first = store.approve_registration(admin.user_id, application.application_id)
    second = store.approve_registration(admin.user_id, application.application_id)
    document = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))

    assert first == second
    assert first.username == "new_user"
    assert first.must_change_password is False
    assert sum(item["username"] == "new_user" for item in document["users"]) == 1
    assert sum(item["team_id"] == str(first.team_id) for item in document["teams"]) == 1
    assert "new user safe password" not in json.dumps(document)


def test_suspending_user_revokes_sessions_and_cannot_target_admin_self(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path)
    admin = store.ensure_user("admin", "admin safe password", True).principal
    user = store.ensure_user("ordinary", "ordinary safe password", False).principal
    token, _csrf = store.issue_session(user.user_id)

    suspended = store.suspend_user(admin.user_id, user.user_id)

    assert suspended.status == "suspended"
    assert store.resolve_session(token) is None
    assert store.authenticate_status("ordinary", "ordinary safe password").state == "suspended"
    with pytest.raises(LocalControlStoreError, match="local control request rejected"):
        store.suspend_user(admin.user_id, admin.user_id)
~~~

另加三项测试：v2 文档迁移到 v3；未知字段、重复用户名和重复申请拒绝；两个进程并发批准只产生一个用户和团队。

- [ ] **Step 2: 运行仓库测试并确认 RED**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_control_store.py \
  -k 'registration or suspend or v2_document'
~~~

Expected: FAIL，首个失败为 `LocalControlStore` 缺少 `submit_registration`、`approve_registration` 或 `suspend_user`。

- [ ] **Step 3: 实现 v3 闭合文档和状态方法**

在 `local_control_store.py` 增加不可变视图：

~~~python
@dataclass(frozen=True, slots=True)
class RegistrationApplication:
    application_id: UUID
    username: str
    display_name: str
    application_reason: str
    status: Literal["pending", "approved", "rejected"]
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class LocalUserView:
    user_id: UUID
    username: str
    display_name: str
    team_id: UUID
    is_platform_admin: bool
    must_change_password: bool
    status: Literal["active", "suspended"]


@dataclass(frozen=True, slots=True)
class LocalAuthentication:
    state: Literal["active", "pending", "rejected", "suspended", "invalid"]
    principal: LocalPrincipal | None
~~~

把 `_SCHEMA_VERSION` 提升到 `3`。`_empty_document()` 返回：

~~~python
return {
    "schema_version": 3,
    "users": [],
    "teams": [],
    "sessions": [],
    "registration_applications": [],
}
~~~

v2 迁移为每个用户补 `display_name=username`、`status="active"`，增加空 `registration_applications`，再写回 v3。新增用户记录严格包含：

~~~python
{
    "user_id": user_id,
    "username": normalized,
    "display_name": display_name,
    "team_id": team_id,
    "password_hash": password_hash,
    "is_platform_admin": admin,
    "must_change_password": must_change_password,
    "status": "active",
}
~~~

实现以下公开方法，每个方法只持有一次 `_exclusive_lock()`：

- `submit_registration(*, username, display_name, password, application_reason) -> RegistrationApplication`
- `authenticate_status(username, password) -> LocalAuthentication`
- `list_registrations(actor_user_id) -> tuple[RegistrationApplication, ...]`
- `approve_registration(actor_user_id, application_id) -> LocalPrincipal`
- `reject_registration(actor_user_id, application_id) -> RegistrationApplication`
- `list_users(actor_user_id) -> tuple[LocalUserView, ...]`
- `suspend_user(actor_user_id, user_id) -> LocalUserView`
- `reactivate_user(actor_user_id, user_id) -> LocalUserView`

`approve_registration` 在一份内存文档上完成申请状态、用户和团队更新，再调用一次 `_write_document`。`authenticate_status` 对未知用户名使用 `_DUMMY_PASSWORD_HASH`，对待审核申请验证申请内的哈希。`resolve_session` 和 `issue_session` 拒绝 suspended 用户。

- [ ] **Step 4: 运行完整仓库测试并确认 GREEN**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_control_store.py
~~~

Expected: 全部 PASS，无 FIFO、symlink、并发锁、大小上限或迁移回归。

- [ ] **Step 5: 提交仓库变更**

~~~bash
git add services/api/src/perfpilot_api/local_control_store.py \
  services/api/tests/unit/test_local_control_store.py
git commit -m "feat: persist local registration approvals"
~~~

### Task 2: 增加注册和管理员 API

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Test: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: 写注册、登录门禁和管理员越权 RED**

增加完整集成测试：匿名用户取得 preauth CSRF 后注册；注册后不能进入 `/v1/me`；普通用户不能访问 `/v1/admin/*`；管理员批准后新用户能登录并只得到自己的团队；停用后旧 cookie 失效。

~~~python
def test_local_registration_requires_admin_approval_and_is_team_private(tmp_path: Path) -> None:
    app = create_local_app(data_root=tmp_path)
    with TestClient(app) as browser:
        csrf = browser.get("/v1/auth/csrf").json()["csrf_token"]
        submitted = browser.post(
            "/v1/auth/register",
            headers={"origin": "http://localhost:3000", "x-csrf-token": csrf},
            json={
                "schema_version": "1.0",
                "username": "new_user",
                "display_name": "新用户",
                "password": "new user safe password",
                "application_reason": "负责启动性能分析",
            },
        )
        assert submitted.status_code == 201
        assert submitted.json()["application"]["status"] == "pending"

        pending_login = browser.post(
            "/v1/auth/login",
            headers={"origin": "http://localhost:3000", "x-csrf-token": csrf},
            json={"username": "new_user", "password": "new user safe password"},
        )
        assert pending_login.status_code == 403
        assert pending_login.json()["error"]["code"] == "account_pending_approval"
~~~

测试后半段使用现有登录 helper 登录管理员，调用批准接口，再登录新用户并断言唯一团队。另加 malformed body 闭合 422、普通用户 403、管理员不能停用自己、重复批准幂等和跨用户 ID 不泄漏测试。

- [ ] **Step 2: 运行集成测试并确认 RED**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/integration/test_local_app.py \
  -k 'registration_requires_admin or admin_user_gate'
~~~

Expected: FAIL，`POST /v1/auth/register` 返回 404。

- [ ] **Step 3: 实现闭合请求模型和路由**

在 `_LoginRequest` 附近增加：

~~~python
class _RegisterRequest(_StrictModel):
    schema_version: Literal["1.0"]
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=1024)
    application_reason: str = Field(min_length=1, max_length=500)


class _AdminDecisionRequest(_StrictModel):
    schema_version: Literal["1.0"]
~~~

增加服务端门禁：

~~~python
def require_admin(request: Request) -> LocalPrincipal:
    principal = _require_principal(request)
    if not principal.is_platform_admin:
        raise ApiError("admin_access_required", "无权访问管理后台", 403, False)
    return principal
~~~

新增路由：

~~~text
POST /v1/auth/register
GET  /v1/admin/registration-applications
POST /v1/admin/registration-applications/{application_id}/approve
POST /v1/admin/registration-applications/{application_id}/reject
GET  /v1/admin/users
POST /v1/admin/users/{user_id}/suspend
POST /v1/admin/users/{user_id}/reactivate
~~~

所有写路由调用 `_require_csrf`。`login` 改用 `authenticate_status`：

~~~python
authentication = resolved_control_store.authenticate_status(body.username, body.password)
if authentication.state == "pending":
    raise ApiError("account_pending_approval", "账号正在等待管理员审核", 403, False)
if authentication.state == "rejected":
    raise ApiError("account_registration_rejected", "账号申请未通过", 403, False)
if authentication.state == "suspended":
    raise ApiError("account_suspended", "账号已停用", 403, False)
if authentication.principal is None:
    raise ApiError("invalid_credentials", "账号或密码错误", 401, False)
principal = authentication.principal
~~~

响应仅包含公开 ID、名称、状态和时间。

- [ ] **Step 4: 运行认证与管理 API 回归**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_control_store.py \
  services/api/tests/integration/test_local_app.py \
  -k 'auth or registration or admin or session or team'
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交 API 变更**

~~~bash
git add services/api/src/perfpilot_api/local_app.py \
  services/api/tests/integration/test_local_app.py
git commit -m "feat: review local user registrations"
~~~

### Task 3: 增加严格前端客户端合同

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Test: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: 写客户端 RED**

测试 `register`、`registrationApplications`、`adminUsers` 和全部 mutation。合法响应通过；附加 `password_hash`、`private_path`、`session` 或未知状态的响应必须抛 `invalid_api_response`。

~~~typescript
it("parses closed registration responses", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(jsonResponse({ schema_version: "1.0", csrf_token: "csrf" }))
    .mockResolvedValueOnce(jsonResponse({
      schema_version: "1.0",
      application: {
        application_id: "82000000-0000-4000-8000-000000000001",
        username: "new_user",
        display_name: "新用户",
        application_reason: "负责启动性能分析",
        status: "pending",
        created_at: "2026-08-20T00:00:00Z",
        decided_at: null,
      },
    }, 201));
  const client = createPerfPilotClient({ fetcher });
  await client.csrf();
  await expect(client.register(
    "new_user", "新用户", "safe password 123", "负责启动性能分析",
  )).resolves.toMatchObject({ status: "pending" });
});
~~~

- [ ] **Step 2: 运行客户端测试并确认 RED**

Run:

~~~bash
npx vitest run tests/perfpilot-api.test.ts -t 'registration|admin user'
~~~

Expected: FAIL，`client.register is not a function`。

- [ ] **Step 3: 添加类型、validator 和客户端方法**

给 `PerfPilotClient` 增加：

~~~typescript
register(
  username: string,
  displayName: string,
  password: string,
  applicationReason: string,
  signal?: AbortSignal,
): Promise<RegistrationApplication>;
registrationApplications(signal?: AbortSignal): Promise<RegistrationApplicationListResponse>;
adminUsers(signal?: AbortSignal): Promise<AdminUserListResponse>;
approveRegistration(applicationId: string, signal?: AbortSignal): Promise<AdminUserView>;
rejectRegistration(applicationId: string, signal?: AbortSignal): Promise<RegistrationApplication>;
suspendUser(userId: string, signal?: AbortSignal): Promise<AdminUserView>;
reactivateUser(userId: string, signal?: AbortSignal): Promise<AdminUserView>;
~~~

validator 全部使用 `exactKeys`，限制数组 256 项，要求规范 UUID、ISO 时间、闭合状态枚举和唯一 ID。`register` 复用 preauth CSRF；管理员 mutation 复用 authenticated CSRF。

- [ ] **Step 4: 运行完整客户端测试**

Run:

~~~bash
npx vitest run tests/perfpilot-api.test.ts
~~~

Expected: 全部 PASS。

- [ ] **Step 5: 提交客户端合同**

~~~bash
git add app/lib/perfpilot-api.ts tests/perfpilot-api.test.ts
git commit -m "feat: add admin account client contracts"
~~~

### Task 4: 在登录页增加自助注册

**Files:**
- Modify: `app/components/local-login.tsx`
- Modify: `app/globals.css`
- Test: `tests/local-login.test.tsx`

- [ ] **Step 1: 写注册表单 RED**

测试登录页切换注册、提交四个字段、成功后只显示等待审核、不挂载工作台，页面不出现“找回密码”。

~~~typescript
it("submits registration and shows pending approval", async () => {
  const register = vi.fn().mockResolvedValue({ status: "pending" });
  const client = {
    csrf: vi.fn().mockResolvedValue("csrf"),
    me: vi.fn().mockRejectedValue({ code: "authentication_required" }),
    register,
  } as unknown as PerfPilotClient;
  render(
    <PerfPilotSessionProvider client={client}>
      <LocalLogin><div>workspace</div></LocalLogin>
    </PerfPilotSessionProvider>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "注册账号" }));
  await user.type(screen.getByLabelText("用户名"), "new_user");
  await user.type(screen.getByLabelText("显示名称"), "新用户");
  await user.type(screen.getByLabelText("密码"), "safe password 123");
  await user.type(screen.getByLabelText("申请说明"), "负责启动性能分析");
  await user.click(screen.getByRole("button", { name: "提交申请" }));
  expect(await screen.findByText("申请已提交，等待管理员审核。")).toBeInTheDocument();
  expect(screen.queryByText("workspace")).toBeNull();
});
~~~

- [ ] **Step 2: 运行 UI 测试并确认 RED**

Run:

~~~bash
npx vitest run tests/local-login.test.tsx -t 'registration|pending approval'
~~~

Expected: FAIL，找不到“注册账号”。

- [ ] **Step 3: 实现登录/注册双视图**

增加 `mode: "login" | "register"`、注册字段和 `submitRegistration`。成功后清空密码并进入 pending 视图。错误按 `PerfPilotApiError.code` 映射：

~~~typescript
const message =
  error instanceof PerfPilotApiError && error.code === "account_pending_approval"
    ? "账号正在等待管理员审核。"
    : error instanceof PerfPilotApiError && error.code === "account_registration_rejected"
      ? "账号申请未通过。"
      : "账号或密码错误，请重试。";
~~~

保留现有强制修改初始密码流程。注册页不渲染找回密码链接。

- [ ] **Step 4: 运行登录和会话门禁测试**

Run:

~~~bash
npx vitest run tests/local-login.test.tsx tests/perfpilot-session-provider.test.tsx
~~~

Expected: 全部 PASS；signed out、pending 和 password-change 状态均不挂载工作台轮询。

- [ ] **Step 5: 提交注册 UI**

~~~bash
git add app/components/local-login.tsx app/globals.css tests/local-login.test.tsx
git commit -m "feat: add reviewed user registration"
~~~

### Task 5: 实现蓝白管理员桌面端和头像入口

**Files:**
- Create: `app/components/admin-console.tsx`
- Create: `app/admin/page.tsx`
- Create: `tests/admin-console.test.tsx`
- Modify: `app/components/perfpilot-session-provider.tsx`
- Modify: `app/components/app-shell.tsx`
- Modify: `app/globals.css`
- Test: `tests/app-shell-device.test.tsx`

- [ ] **Step 1: 写管理员入口与后台 RED**

覆盖：管理员头像菜单显示“管理后台”；普通用户不显示；后台读取账号申请和用户列表；审批按钮更新当前行；页面不出现“操作记录”。

~~~typescript
it("shows the admin entry only to platform admins", async () => {
  renderWithSession({ is_platform_admin: true });
  await userEvent.click(await screen.findByRole("button", { name: /当前用户/ }));
  expect(screen.getByRole("link", { name: "管理后台" }))
    .toHaveAttribute("href", "/admin");
  cleanup();
  renderWithSession({ is_platform_admin: false });
  expect(screen.queryByRole("link", { name: "管理后台" })).toBeNull();
});
~~~

- [ ] **Step 2: 运行前端测试并确认 RED**

Run:

~~~bash
npx vitest run tests/admin-console.test.tsx tests/app-shell-device.test.tsx
~~~

Expected: FAIL，缺少 `admin-console` 或“管理后台”入口。

- [ ] **Step 3: 向 Session Provider 公开当前用户**

给 `PerfPilotSessionValue` 增加：

~~~typescript
readonly user: MeResponse["user"] | null;
~~~

`applyMe` 设置用户，`clearSession` 清空用户。`AppShell` 使用真实 username 和管理员标记替换固定文案。

- [ ] **Step 4: 实现管理员页面**

`app/admin/page.tsx`：

~~~tsx
import { AdminConsole } from "../components/admin-console";

export default function AdminPage() {
  return <AdminConsole />;
}
~~~

`AdminConsole` 仅在 `status === "ready" && user?.is_platform_admin` 时请求管理数据；普通用户渲染 403 页面且不调用管理 API。实现确认的蓝白侧栏、总览、账号审核和 Agent 审核 tab；Agent tab 的真实数据由下一份计划接入。mutation 使用 `AbortController`，提交期间 disabled，成功后替换对应行。

- [ ] **Step 5: 运行 UI、lint 和 build**

Run:

~~~bash
npx vitest run tests/admin-console.test.tsx tests/app-shell-device.test.tsx \
  tests/local-login.test.tsx tests/perfpilot-session-provider.test.tsx
npm run lint
npm run build
~~~

Expected: 全部 PASS，build exit 0。

- [ ] **Step 6: 提交管理员 UI**

~~~bash
git add app/components/admin-console.tsx app/admin/page.tsx \
  app/components/perfpilot-session-provider.tsx app/components/app-shell.tsx \
  app/globals.css tests/admin-console.test.tsx tests/app-shell-device.test.tsx
git commit -m "feat: add local account administration"
~~~

### Task 6: 完成账号审核验收与全量回归

**Files:**
- Modify: `services/api/tests/acceptance/test_source_aware_report_flow.py`
- Modify: `tests/local-login.test.tsx`

- [ ] **Step 1: 增加跨重启和分析清理验收**

新增验收：管理员批准 user06，user06 登录并创建私有团队状态；关闭并重开 app 后用户仍可登录；运行现有分析数据 reset 后用户、团队和管理员身份仍存在；user06 不能读取 user01 分析。

- [ ] **Step 2: 运行账号完整门禁**

Run:

~~~bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider -q \
  services/api/tests/unit/test_local_control_store.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_source_aware_report_flow.py \
  -k 'auth or user or registration or team or reset'
npx vitest run tests/perfpilot-api.test.ts tests/local-login.test.tsx \
  tests/perfpilot-session-provider.test.tsx tests/admin-console.test.tsx \
  tests/app-shell-device.test.tsx
~~~

Expected: 全部 PASS。

- [ ] **Step 3: 运行静态门禁**

Run:

~~~bash
.venv/bin/ruff check services/api/src/perfpilot_api \
  services/api/tests/unit/test_local_control_store.py \
  services/api/tests/integration/test_local_app.py
npm run lint
npm run build
git diff --check
~~~

Expected: 全部 exit 0。

- [ ] **Step 4: 提交验收**

~~~bash
git add services/api/tests/acceptance/test_source_aware_report_flow.py \
  tests/local-login.test.tsx
git commit -m "test: verify reviewed local accounts"
~~~

## 完成定义

- 注册用户在管理员批准前无法进入工作台。
- 批准原子创建一个用户和一个团队。
- 普通用户无法访问后台 UI 或 API。
- 停用立即撤销会话，恢复后必须重新登录。
- 管理员无法停用自己或提升其他管理员。
- 登录页没有找回密码，后台没有操作记录。
- 现有用户、管理员、团队、会话安全和分析隔离回归通过。
