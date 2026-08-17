# PerfPilot Live Agent Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Ubuntu deployment source-aware and HTTPS-only, keep the Agent alive across device failures and cancellations, and verify the repaired flow on the connected Android device with an APK whose versionCode is at least 202.

**Architecture:** Keep the approved remote-capture and LAN designs. The zero-argument FastAPI factory reads one strict source-analysis feature flag. A pinned user-local Caddy process terminates TLS and proxies API and web traffic to loopback-only services. Device preparation failures become closed failed manifests, while expired canceled leases become terminal instead of being leased again.

**Tech Stack:** FastAPI, asyncio, Pydantic, pytest, Vitest, Bash, systemd user services, Caddy 2.11.4, ADB, Perfetto.

**Design basis:** `docs/superpowers/specs/2026-08-13-remote-agent-trace-capture-design.md` and `docs/superpowers/specs/2026-08-05-perfpilot-lan-deployment-device-agent-design.md`.

---

### Task 1: Enable source analysis in the deployed app factory

**Files:**
- Modify: `services/api/src/perfpilot_api/local_app.py`
- Modify: `services/api/tests/integration/test_local_app.py`
- Modify: `scripts/bootstrap-ubuntu-user.sh`
- Modify: `tests/ubuntu-user-deployment.test.ts`

- [ ] **Step 1: Write failing environment-boundary tests**

Add tests that clear `PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED`, prove the zero-argument factory stays disabled, set it to `true` and prove an authenticated Agent workspace appears, and reject values other than `true` or `false`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/integration/test_local_app.py \
  -k 'source_code_analysis_environment' -q
```

Expected: the enabled case returns no workspace because `create_local_app()` hard-codes `False`.

- [ ] **Step 3: Resolve a strict deployment feature flag**

Add a closed parser and preserve explicit test overrides:

```python
def _environment_boolean(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def create_local_app(*, source_code_analysis_enabled: bool | None = None, ...) -> FastAPI:
    resolved_source_enabled = (
        _environment_boolean(
            "PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED",
            default=False,
        )
        if source_code_analysis_enabled is None
        else source_code_analysis_enabled
    )
```

Pass `resolved_source_enabled` to `SourceWorkspaceService`. Write `PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED=true` into the Ubuntu environment.

- [ ] **Step 4: Run backend and deployment tests**

Run the focused pytest command above and:

```bash
npx vitest run tests/ubuntu-user-deployment.test.ts
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/local_app.py \
  services/api/tests/integration/test_local_app.py \
  scripts/bootstrap-ubuntu-user.sh tests/ubuntu-user-deployment.test.ts
git commit -m "fix: enable deployed source analysis"
```

### Task 2: Convert device preparation failures into terminal manifests

**Files:**
- Modify: `agents/device-agent/src/perfpilot_agent/capture.py`
- Modify: `agents/device-agent/tests/unit/test_capture.py`

- [ ] **Step 1: Write failing install and service-survival tests**

Make a fake device raise `CaptureError("apk_install_failed")` from `install()`. Assert that `CaptureExecution.wait()` returns a failed manifest with both requested scenarios failed, uploads a bounded `agent_log`, runs cleanup, and never raises. Run that execution through `TaskExecutor` and assert the completion is sent once and the next task poll remains possible.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider \
  agents/device-agent/tests/unit/test_capture.py \
  agents/device-agent/tests/unit/test_executor.py \
  -k 'install_failure or failed_manifest' -q
```

Expected: install failure escapes `CaptureExecution`, so no failed completion is sent.

- [ ] **Step 3: Build the failed manifest before scenario execution**

In `CaptureExecution._execute`, call `adb_version()` before install, catch only expected local device preparation errors, append one failed `_ScenarioResult` per signed scenario with the same stable diagnostic code, then continue through the existing agent-log upload and closed-manifest builder. Do not catch `ControlClientError`, upload errors, cancellation, or lease loss.

Do not add a broad exception guard to `AgentService`: programming errors must still reach systemd supervision. The closed failed manifest handles expected device failures through the normal completion protocol.

- [ ] **Step 4: Run the Agent suite**

```bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider \
  agents/device-agent/tests/unit agents/device-agent/tests/integration -q
```

Expected: all Agent tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/device-agent/src/perfpilot_agent/capture.py \
  agents/device-agent/tests/unit/test_capture.py \
  agents/device-agent/tests/unit/test_executor.py
git commit -m "fix: contain remote capture failures"
```

### Task 3: Stop canceled expired tasks from being leased again

**Files:**
- Modify: `services/api/src/perfpilot_api/services/agent_tasks.py`
- Modify: `services/api/tests/unit/test_agent_task_service.py`
- Modify: `services/api/tests/integration/test_local_app.py`

- [ ] **Step 1: Write the expired-lease cancellation regression**

Schedule a task, move the clock past `expires_at`, request cancellation, and assert that the response is terminal `canceled`, `oldest_queued()` returns `None`, and `schedule()` returns `None`. Add a local API test that reproduces Agent failure, cancels the analysis, polls again, and receives `wait` rather than a new execution.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHONPATH=services/api/src .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_agent_task_service.py \
  services/api/tests/integration/test_local_app.py \
  -k 'expired_lease_cancel or failed_agent_cancel' -q
```

Expected: the canceled definition receives a new lease.

- [ ] **Step 3: Terminalize expired cancellations**

In `InMemoryAgentTaskRepository.request_cancel`, select only live active or cancel-requested leases. If none exists, remove `_definitions` and `_queued_at` and return `analysis_state="canceled"`. Leave released lease records available for idempotent completion and cancellation replay.

- [ ] **Step 4: Run task, API, and acceptance suites**

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_agent_task_service.py \
  services/api/tests/integration/test_local_app.py \
  services/api/tests/acceptance/test_remote_agent_capture.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api/services/agent_tasks.py \
  services/api/tests/unit/test_agent_task_service.py \
  services/api/tests/integration/test_local_app.py
git commit -m "fix: terminalize expired capture cancellations"
```

### Task 4: Add the Ubuntu HTTPS gateway

**Files:**
- Create: `infra/ubuntu-user/Caddyfile`
- Create: `infra/ubuntu-user/systemd/perfpilot-gateway.service`
- Modify: `infra/ubuntu-user/systemd/perfpilot-api.service`
- Modify: `infra/ubuntu-user/systemd/perfpilot-web.service`
- Modify: `infra/ubuntu-user/systemd/perfpilot-reset-analysis-data.service`
- Modify: `infra/ubuntu-user/systemd/perfpilot.target`
- Modify: `scripts/bootstrap-ubuntu-user.sh`
- Modify: `scripts/restart-ubuntu-perfpilot.sh`
- Modify: `tests/ubuntu-user-deployment.test.ts`

- [ ] **Step 1: Write failing deployment-contract tests**

Require a supervised gateway, loopback-only API/Web listeners, HTTPS health checks, persistent private CA/server keys, a public CA certificate, pinned Caddy `2.11.4` with Linux amd64 SHA-256 `527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9`, and source enablement.

- [ ] **Step 2: Run the deployment test and confirm RED**

```bash
npx vitest run tests/ubuntu-user-deployment.test.ts
```

Expected: gateway files, HTTPS checks, and loopback bindings are absent.

- [ ] **Step 3: Install and configure the gateway**

Download the pinned Caddy archive from the official release, verify the pinned SHA-256, and install only the `caddy` executable under `%h/.local/bin`. Generate a persistent private CA and an IP-SAN server certificate under `%h/perfpilot/state/tls` with exact `0700`/`0600` modes. Copy only the CA certificate to `%h/perfpilot/config/perfpilot-agent-ca.crt` with mode `0644`.

Use this routing boundary:

```caddyfile
https://{$PERFPILOT_SERVER_IP}:8443 {
    tls {$PERFPILOT_TLS_CERT_FILE} {$PERFPILOT_TLS_KEY_FILE}
    @api path /v1/* /local/v1/*
    handle @api {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        reverse_proxy 127.0.0.1:3000
    }
}
```

Bind Uvicorn and Vinext to `127.0.0.1`. Set both public origins to `https://10.166.0.125:8443`. Add the gateway to the target and reset ordering.

- [ ] **Step 4: Run deployment, frontend, and shell checks**

```bash
npx vitest run tests/ubuntu-user-deployment.test.ts
bash -n scripts/bootstrap-ubuntu-user.sh scripts/restart-ubuntu-perfpilot.sh
npm run lint
```

Expected: all checks pass.

- [ ] **Step 5: Commit**

```bash
git add infra/ubuntu-user scripts/bootstrap-ubuntu-user.sh \
  scripts/restart-ubuntu-perfpilot.sh tests/ubuntu-user-deployment.test.ts
git commit -m "feat: terminate local Agent traffic with HTTPS"
```

### Task 5: Push, deploy, and run the connected-device acceptance

**Files:**
- Modify only if the live run reproduces a contract failure with a focused RED test.

- [ ] **Step 1: Run final local gates**

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/acceptance/test_remote_agent_capture.py \
  services/api/tests/integration/test_local_app.py \
  agents/device-agent/tests/unit agents/device-agent/tests/integration -q
npx vitest run tests/ubuntu-user-deployment.test.ts tests/perfpilot-api.test.ts
npm run lint
npm run build
git diff --check
```

- [ ] **Step 2: Push and deploy**

Push `main`, pull it on `rivotek@10.166.0.125`, run `scripts/bootstrap-ubuntu-user.sh`, then restart through `scripts/restart-ubuntu-perfpilot.sh`. Verify that the reset leaves zero analysis files and no backup/archive.

- [ ] **Step 3: Establish trusted Agent HTTPS**

Copy the public CA certificate from the Ubuntu config directory, verify its SHA-256 fingerprint over SSH and locally, and configure the Agent with `https://10.166.0.125:8443`. Register the Agent and add the selected source workspace.

- [ ] **Step 4: Obtain and validate an APK with versionCode at least 202**

Pull `/system/app/gallery_n66/gallery_n66.apk` from device `0123456789ABCDEF` into a private temporary directory. Verify package `com.rivotek.gallery`, `versionCode >= 202`, SHA-256, and `adb install -r -t` success before submitting it. If the package path changed, resolve it with `adb shell pm path com.rivotek.gallery` and reject split APK sets.

- [ ] **Step 5: Run the full browser/API/Agent flow**

Verify HTTPS health, source workspace visibility and selection, signed task delivery, install, startup and scroll captures, multipart uploads, completion, SmartPerfetto scenario outputs, one Chinese PerfPilot report, original reports, and zero server-host ADB calls. Explicitly test cancel on one controlled run and confirm the Agent remains alive and receives no replacement execution.

- [ ] **Step 6: Restore and clean**

Confirm the Agent restores the system package, unregister the temporary Agent, delete only temporary local credentials/APK/CA copies, restart the Ubuntu stack, and verify zero analysis files and no backup/archive. Preserve accounts, Agent history, and source workspace registry.

## Self-review

- Every approved live-readiness requirement maps to one task.
- Each behavior change starts with an executable failing test.
- HTTPS uses one process and one stateful API backend; it does not create a second local runtime.
- CA private keys remain on Ubuntu. Agent machines receive only the public CA certificate.
- Device failures produce closed manifests; transport and lease failures remain retryable.
- The final acceptance uses an exact single APK with `versionCode >= 202` and restores the system app afterward.
