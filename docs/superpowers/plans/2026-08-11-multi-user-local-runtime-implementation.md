# Multi-user Local Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the trusted Ubuntu test runtime into a session-authenticated, one-user-one-team service whose users, Agent registrations, and public source workspace ownership survive restarts while every analysis artifact is permanently erased on each restart.

**Architecture:** Keep `create_local_app()` and the no-sudo Ubuntu deployment, but split state into a private persistent control store and a team-scoped ephemeral analysis store. Reuse the existing password hashing, Agent protocol models, source-workspace privacy validation, and browser team paths; add local adapters only where PostgreSQL is unavailable. Authorization derives the team from the authenticated session, never from client claims, and cross-team access returns `404`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, Argon2 password helpers, atomic JSON persistence, pytest, Next.js 16, React 19, TypeScript, Vitest, systemd user services, Bash.

---

### Task 1: Persistent local users, teams, and sessions

**Files:**
- Create: `services/api/src/perfpilot_api/local_control_store.py`
- Create: `services/api/tests/unit/test_local_control_store.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`

- [ ] **Step 1: Write the failing control-store tests**

```python
def test_bootstrap_is_idempotent_and_never_replaces_changed_password(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path / "state")
    first = store.ensure_user(username="user01", password="Temporary-1", admin=False)
    store.change_password(first.user_id, "Permanent-1")
    second = store.ensure_user(username="user01", password="Temporary-2", admin=False)
    assert second.user_id == first.user_id
    assert store.authenticate("user01", "Permanent-1") == first
    assert store.authenticate("user01", "Temporary-2") is None

def test_state_is_private_atomic_and_contains_no_plaintext_password(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path / "state")
    store.ensure_user(username="user01", password="Temporary-1", admin=False)
    document = (tmp_path / "state" / "control.json").read_text()
    assert "Temporary-1" not in document
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state" / "control.json").stat().st_mode) == 0o600
```

- [ ] **Step 2: Run the tests and capture RED**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_control_store.py -q`

Expected: collection fails with `ModuleNotFoundError: perfpilot_api.local_control_store`.

- [ ] **Step 3: Implement closed persistent records and atomic mutation**

```python
@dataclass(frozen=True, slots=True)
class LocalPrincipal:
    user_id: UUID
    username: str
    team_id: UUID
    team_name: str
    is_platform_admin: bool
    must_change_password: bool

class LocalControlStore:
    def require_team(self, token: str, team_id: UUID) -> LocalPrincipal:
        principal = self.resolve_session(token)
        if principal is None or principal.team_id != team_id:
            raise LocalControlNotFound
        return principal
```

Add exact public methods `ensure_user(username, password, admin) -> LocalPrincipal`, `authenticate(username, password) -> LocalPrincipal | None`, `issue_session(user_id) -> tuple[str, str]`, `resolve_session(token) -> LocalPrincipal | None`, `change_password(user_id, password) -> LocalPrincipal`, and the shown `require_team`. Normalize usernames with `normalize_username`, hash with `hash_password`, verify with `verify_password`, generate stable UUIDv4 IDs once, keep only SHA-256 session-token digests, and serialize with `schema_version`, `users`, `teams`, and `sessions` as closed dictionaries. Every mutation must lock, write a same-directory `0600` temporary file, `fsync`, `os.replace`, and retain a `0700` state directory. Reject symlinked state roots and malformed persisted documents with a redacted `LocalControlStoreError`.

- [ ] **Step 4: Prove persistence, session expiry, and corruption handling**

Add tests that reopen the store, validate stable `user_id`/`team_id`, reject expired sessions, reject unknown JSON keys, and verify that a failed atomic replace leaves the prior document readable.

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_control_store.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the control-store boundary**

```bash
git add services/api/src/perfpilot_api/local_control_store.py services/api/tests/unit/test_local_control_store.py
git commit -m "feat: persist local users and teams"
```

### Task 2: Login, forced password change, and browser session gate

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/perfpilot-session-provider.tsx`
- Create: `app/components/local-login.tsx`
- Modify: `app/layout.tsx`
- Modify: `tests/perfpilot-api.test.ts`
- Create: `tests/local-login.test.tsx`

- [ ] **Step 1: Write API RED tests for authenticated local sessions**

```python
def test_local_login_requires_password_change_before_team_access(local_client) -> None:
    csrf = local_client.get("/v1/auth/csrf").json()["csrf_token"]
    login = local_client.post(
        "/v1/auth/login",
        headers={"x-csrf-token": csrf, "origin": "http://10.166.0.125:3000"},
        json={"username": "user01", "password": "Temporary-1"},
    )
    assert login.status_code == 200
    assert local_client.get(f"/v1/teams/{USER01_TEAM}/analyses").status_code == 403
    changed = local_client.post(
        "/v1/auth/change-password",
        headers={"x-csrf-token": login.json()["csrf_token"]},
        json={"current_password": "Temporary-1", "new_password": "Permanent-1"},
    )
    assert changed.status_code == 204
```

Also assert invalid user and invalid password return the same `invalid_credentials`, logout invalidates the cookie, and an unauthenticated `/v1/me` returns `401`.

- [ ] **Step 2: Run the API RED tests**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -k 'login or password or unauthenticated' -q`

Expected: tests fail because the local runtime currently returns a fixed `ray_wu` identity and has no login endpoint.

- [ ] **Step 3: Replace fixed identity with cookie-authenticated dependencies**

```python
def current_principal(request: Request) -> LocalPrincipal:
    principal = control_store.resolve_session(request.cookies.get(COOKIE_NAME, ""))
    if principal is None:
        raise ApiError("unauthenticated", "需要重新登录", 401, False)
    return principal

def authorize_team(request: Request, team_id: UUID) -> LocalPrincipal:
    principal = current_principal(request)
    if principal.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "team not found")
    if principal.must_change_password:
        raise ApiError("password_change_required", "请先修改初始密码", 403, False)
    return principal
```

Add `/v1/auth/login`, `/v1/auth/logout`, `/v1/auth/change-password`, and authenticated `/v1/me`. Rotate the session and CSRF token after login and password change, use `HttpOnly; SameSite=Strict` cookies, enforce the configured LAN origin on state-changing requests, and remove `LOCAL_USER_ID`, `LOCAL_TEAM_ID`, and the global static CSRF token from request authorization.

- [ ] **Step 4: Run local authentication tests GREEN**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -k 'login or password or unauthenticated or me' -q`

Expected: all selected tests pass.

- [ ] **Step 5: Write browser RED tests for login and password change**

```tsx
it("shows login before loading a team and forces initial password change", async () => {
  render(<LocalLogin client={client}><span>dashboard</span></LocalLogin>);
  await user.type(screen.getByLabelText("账号"), "user01");
  await user.type(screen.getByLabelText("密码"), "Temporary-1");
  await user.click(screen.getByRole("button", { name: "登录" }));
  expect(await screen.findByRole("heading", { name: "修改初始密码" })).toBeVisible();
  expect(screen.queryByText("dashboard")).toBeNull();
});
```

- [ ] **Step 6: Add strict client methods and the session UI gate**

Extend `PerfPilotClient` with `login`, `logout`, and `changePassword`; validate every response with `exactKeys`. Make `PerfPilotSessionProvider` expose `principal`, `login`, `logout`, and `changePassword`, treat `401` as signed out, and render `LocalLogin` from `app/layout.tsx` until `/v1/me` succeeds. The change-password form must block all app children while `must_change_password` is true.

- [ ] **Step 7: Run frontend tests and commit**

Run: `npm test -- --run tests/perfpilot-api.test.ts tests/local-login.test.tsx`

Expected: all tests pass.

```bash
git add services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py app/lib/perfpilot-api.ts app/components/perfpilot-session-provider.tsx app/components/local-login.tsx app/layout.tsx tests/perfpilot-api.test.ts tests/local-login.test.tsx
git commit -m "feat: authenticate local test users"
```

### Task 3: Team-scoped analysis storage and cross-user denial

**Files:**
- Modify: `services/api/src/perfpilot_api/local_analysis_store.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `services/api/tests/unit/test_local_analysis_store.py`
- Modify: `tests/perfpilot-api.test.ts`

- [ ] **Step 1: Write a two-user isolation RED matrix**

```python
@pytest.mark.parametrize("operation", ["list", "read", "cancel", "report", "upload"])
def test_user_cannot_access_another_users_analysis(two_logged_in_clients, operation) -> None:
    owner, stranger = two_logged_in_clients
    analysis = create_trace(owner, team_id=USER01_TEAM)
    response = perform(operation, stranger, path_team=USER02_TEAM, analysis_id=analysis["analysis_id"])
    assert response.status_code == 404
    assert str(analysis["analysis_id"]) not in response.text
```

Add a filesystem assertion that user01 data is under `teams/<user01_team>/analyses/<analysis_id>` and no analysis file is written at the legacy root.

- [ ] **Step 2: Run the isolation matrix RED**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_local_app.py -k 'another_users or team_scoped' -q`

Expected: failures show the current global `analyses` dictionary and analysis-id-only filesystem path can cross the team boundary.

- [ ] **Step 3: Make team identity part of every analysis key and file path**

```python
@dataclass(slots=True)
class _LocalAnalysis:
    team_id: UUID
    analysis_id: UUID
    # existing fields remain unchanged

class LocalAnalysisStore:
    def analysis_root(self, team_id: UUID, analysis_id: UUID) -> Path:
        root = self.root / "teams" / str(team_id) / "analyses" / str(analysis_id)
        return _require_descendant(root, self.root / "teams" / str(team_id))
```

Key runtime dictionaries by `(team_id, analysis_id)`, pass `team_id` to `create`, `analysis`, `reserve`, `finalize`, `cancel`, `report_analyses`, `active_analyses`, `_persist`, `save_document`, and `load_document`, and verify the persisted document's `team_id` before loading. Do not grant an admin bypass.

- [ ] **Step 4: Run API/store GREEN tests**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_analysis_store.py services/api/tests/integration/test_local_app.py -q`

Expected: all tests pass, including all cross-user operations returning `404`.

- [ ] **Step 5: Prove browser queries use only the signed-in team**

Add a client test that switches from a user01 session to user02 and asserts `analyses`, `activeAnalyses`, `report`, and `sourceWorkspaces` are called only with user02's team ID and that cached user01 cards disappear.

Run: `npm test -- --run tests/perfpilot-api.test.ts tests/dashboard.test.tsx tests/perfpilot-session-provider.test.tsx`

Expected: all tests pass.

- [ ] **Step 6: Commit tenant isolation**

```bash
git add services/api/src/perfpilot_api/local_analysis_store.py services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py services/api/tests/unit/test_local_analysis_store.py tests/perfpilot-api.test.ts tests/dashboard.test.tsx tests/perfpilot-session-provider.test.tsx
git commit -m "feat: isolate local analyses by user team"
```

### Task 4: Persistent Agent registration and per-user source workspaces

**Files:**
- Create: `services/api/src/perfpilot_api/local_agent_store.py`
- Create: `services/api/tests/unit/test_local_agent_store.py`
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `app/components/agent-management.tsx`
- Modify: `app/components/source-workspace-field.tsx`
- Modify: `tests/source-workspace-field.test.tsx`
- Modify: `tests/agent-management.test.tsx`

- [ ] **Step 1: Write RED tests for Agent ownership and restart persistence**

```python
def test_agent_and_public_workspace_survive_restart_but_remain_team_private(tmp_path: Path) -> None:
    first = create_local_app(data_root=tmp_path / "data", state_root=tmp_path / "state")
    code = issue_registration_code(first, user="user01", name="Ray Mac")
    credentials = register_agent(first, code)
    heartbeat(first, credentials, workspace_name="RivotekMedia", workspace_id=WORKSPACE_ID)
    second = create_local_app(data_root=tmp_path / "data", state_root=tmp_path / "state")
    assert list_workspaces(second, user="user01") == [public_workspace(WORKSPACE_ID)]
    assert list_workspaces(second, user="user02") == []
    assert "/Users/" not in (tmp_path / "state" / "agents.json").read_text()
```

Also cover registration-code one-time use, token-digest-only persistence, revoke, rename, stale/offline state, and rejection of absolute-path-shaped agent/workspace/profile/branch names.

- [ ] **Step 2: Run the Agent RED tests**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_agent_store.py services/api/tests/integration/test_local_app.py -k 'agent or source_workspace' -q`

Expected: collection fails for `perfpilot_api.local_agent_store`, and local Agent endpoints return `404`.

- [ ] **Step 3: Implement the persistent repository adapters**

```python
class LocalAgentRepository(AgentRepository, DeviceDirectoryRepository):
    async def replace_snapshot(self, *, agent_id: UUID, heartbeat: AgentHeartbeat,
                               devices: Sequence[SanitizedDeviceObservation],
                               now: datetime) -> Sequence[DeviceRecord]:
        async with self._lock:
            record = self._require_active_agent(agent_id)
            sanitized = self._replace_sanitized_devices(record, devices, now)
            self._replace_public_capabilities(record, heartbeat, now)
            self._save_locked()
            return sanitized
```

Implement every method required by the existing `AgentRepository` and `DeviceDirectoryRepository` protocols, including pending registration creation/consumption, credential rotation, access lookup, list/rename/revoke, heartbeat replacement, stale expiry, team-device listing, and source-agent lookup. Persist only hashed credentials, team ownership, sanitized device fields, and public source-workspace capability records in `state/agents.json`; never persist registration codes, access tokens, refresh tokens, serials, source content, or absolute paths. Use the same private atomic writer and cross-process lock discipline as `LocalControlStore`.

- [ ] **Step 4: Wire existing Agent control and source services into `create_local_app`**

Instantiate `AgentService(repository=local_agent_repository)`, `DeviceDirectory(repository=local_agent_repository)`, and `SourceWorkspaceService(repository=device_directory, enabled=True)`. Include the existing `/v1/agent/register`, refresh, unregister, heartbeat, and task routes; expose browser `/agents`, `/registration-codes`, rename, revoke, `/devices`, and `/source-workspaces` using `authorize_team`. On analysis creation, call `source_workspace_service.require_binding` before accepting a source binding.

- [ ] **Step 5: Make source execution use the existing signed source-task pipeline**

Connect accepted local `source_binding` to `SourceTaskService.create_context_task`, let the existing Agent poll/renew/complete endpoints carry the signed task, persist the validated result under the analysis's team root, and feed it into the already implemented `source_context` synthesis path. Add an integration test that drives registration → heartbeat → trace upload → signed source completion → AnalysisReport 1.2 with `source_code.context_state == "available"`; assert the server response and persistent control JSON contain no absolute source path.

- [ ] **Step 6: Improve the empty state without choosing a path for the user**

Keep `SourceWorkspaceField` defaulted to “暂不关联源码”. When empty, show the three commands using the server origin and user-generated registration code:

```text
perfpilot-agent register
perfpilot-agent source add --name "RivotekMedia" --path "$PWD"
perfpilot-agent run
```

Do not prefill a path, Agent, or workspace. Add frontend tests for user01 seeing only user01 workspaces and user02 seeing only user02 workspaces.

- [ ] **Step 7: Run Agent/source gates and commit**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_agent_store.py services/api/tests/integration/test_local_app.py agents/device-agent/tests -q`

Run: `npm test -- --run tests/source-workspace-field.test.tsx tests/agent-management.test.tsx tests/perfpilot-api.test.ts`

Expected: all selected tests pass.

```bash
git add services/api/src/perfpilot_api/local_agent_store.py services/api/tests/unit/test_local_agent_store.py services/api/src/perfpilot_api/local_app.py services/api/tests/integration/test_local_app.py app/components/agent-management.tsx app/components/source-workspace-field.tsx tests/source-workspace-field.test.tsx tests/agent-management.test.tsx
git commit -m "feat: connect user-owned source agents"
```

### Task 5: Permanent restart reset and five-user bootstrap

**Files:**
- Create: `scripts/reset-ubuntu-analysis-data.sh`
- Create: `scripts/bootstrap-local-users.py`
- Create: `infra/ubuntu-user/systemd/perfpilot-reset-analysis-data.service`
- Create: `infra/ubuntu-user/systemd/perfpilot.target`
- Modify: `infra/ubuntu-user/systemd/perfpilot-api.service`
- Modify: `infra/ubuntu-user/systemd/perfpilot-smartperfetto.service`
- Modify: `infra/ubuntu-user/systemd/perfpilot-web.service`
- Modify: `scripts/bootstrap-ubuntu-user.sh`
- Modify: `infra/ubuntu-user/README.md`
- Modify: `tests/ubuntu-user-deployment.test.ts`

- [ ] **Step 1: Write reset-script RED tests**

Add tests that create `data/local-runtime/teams/*` and `state/control.json`, run the reset script, and assert analysis data is gone, the analysis root is recreated `0700`, state is byte-identical, and no path containing `backup`, `archive`, `.tar`, or `.zip` exists. Add negative tests for `/`, `$HOME`, the state root, a symlink root, and `PERFPILOT_RESET_ANALYSIS_ON_RESTART=false`.

Run: `npm test -- --run tests/ubuntu-user-deployment.test.ts`

Expected: failures show the reset unit and safe script do not exist and current systemd services can bypass reset.

- [ ] **Step 2: Implement a fail-closed permanent reset**

```bash
DATA_ROOT="${PERFPILOT_LOCAL_DATA_DIR:?missing PERFPILOT_LOCAL_DATA_DIR}"
EXPECTED_ROOT="${PERFPILOT_EXPECTED_ANALYSIS_ROOT:?missing expected root}"
[[ "$PERFPILOT_RESET_ANALYSIS_ON_RESTART" == "true" ]]
[[ "$DATA_ROOT" == "$EXPECTED_ROOT" ]]
[[ ! -L "$DATA_ROOT" ]]
rm -rf -- "$DATA_ROOT"
install -d -m 0700 "$DATA_ROOT"
```

Before deletion, resolve and compare the parent directory, reject root/home/config/state/project paths, reject symlink components, and refuse unset or mismatched roots. The script must never move, compress, copy, or timestamp old data.

- [ ] **Step 3: Make systemd restart go through one target**

Configure `perfpilot-reset-analysis-data.service` as `Type=oneshot`, `RemainAfterExit=no`, `Before=perfpilot-smartperfetto.service perfpilot-api.service perfpilot-web.service`, and condition it on `PERFPILOT_RESET_ANALYSIS_ON_RESTART=true`. Make `perfpilot.target` start reset first and then the three services. Update bootstrap and README so the only supported command is:

```bash
systemctl --user restart perfpilot.target
```

Make bootstrap fail if the reset unit fails and create `%h/perfpilot/state` as `0700` separately from `%h/perfpilot/data/local-runtime`.

- [ ] **Step 4: Add an idempotent secure five-user bootstrap**

```python
USERNAMES = ("user01", "user02", "user03", "user04", "user05")
for username in USERNAMES:
    password = secrets.token_urlsafe(18)
    created = store.ensure_user(username=username, password=password, admin=False)
    if created.created:
        credentials_file.write(f"{username}\t{password}\n")
```

Read the `ray_wu` admin password only from a `0600` file descriptor or environment removed immediately from `os.environ`; never accept passwords as command-line arguments. Write newly generated temporary credentials once to `%h/perfpilot/state/bootstrap-users.txt` with mode `0600`, omit existing users, set `must_change_password=true`, and print only the credential-file path.

- [ ] **Step 5: Run deployment tests and commit**

Run: `npm test -- --run tests/ubuntu-user-deployment.test.ts`

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_control_store.py services/api/tests/unit/test_local_agent_store.py services/api/tests/integration/test_local_app.py -q`

Expected: all selected tests pass.

```bash
git add scripts/reset-ubuntu-analysis-data.sh scripts/bootstrap-local-users.py infra/ubuntu-user/systemd/perfpilot-reset-analysis-data.service infra/ubuntu-user/systemd/perfpilot.target infra/ubuntu-user/systemd/perfpilot-api.service infra/ubuntu-user/systemd/perfpilot-smartperfetto.service infra/ubuntu-user/systemd/perfpilot-web.service scripts/bootstrap-ubuntu-user.sh infra/ubuntu-user/README.md tests/ubuntu-user-deployment.test.ts
git commit -m "feat: reset isolated test data on restart"
```

### Task 6: Focused security verification

**Files:**
- Modify only files implicated by failing tests from Tasks 1–5.

- [ ] **Step 1: Run backend and Agent focused suites**

Run: `.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_local_control_store.py services/api/tests/unit/test_local_agent_store.py services/api/tests/unit/test_local_analysis_store.py services/api/tests/integration/test_local_app.py agents/device-agent/tests -q`

Expected: all tests pass.

- [ ] **Step 2: Run frontend focused suites and static gates**

Run: `npm test -- --run tests/perfpilot-api.test.ts tests/local-login.test.tsx tests/perfpilot-session-provider.test.tsx tests/dashboard.test.tsx tests/source-workspace-field.test.tsx tests/agent-management.test.tsx tests/ubuntu-user-deployment.test.ts`

Run: `npm run lint && npm run test:ssr && git diff --check`

Expected: all tests and checks pass.

- [ ] **Step 3: Audit the privacy boundary**

Run: `rg -n '/Users/|[A-Za-z]:\\\\|file://|source.*path|password_hash|registration_code' /tmp/perfpilot-test-responses /tmp/perfpilot-test-logs`

Expected: no absolute source path, password/hash, token, or registration code appears. Expected command exit is `1` because no match is found.

- [ ] **Step 4: Commit only if verification required a scoped fix**

```bash
git add services/api/src/perfpilot_api/local_control_store.py services/api/src/perfpilot_api/local_agent_store.py services/api/src/perfpilot_api/local_analysis_store.py services/api/src/perfpilot_api/local_app.py app/lib/perfpilot-api.ts app/components/perfpilot-session-provider.tsx
git commit -m "fix: close local tenant isolation gaps"
```

If no file changed, do not create an empty commit.
