# PerfPilot Cross-Platform Device Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development before implementation and superpowers:verification-before-completion before each commit.

**Goal:** Ship one Python Agent core that registers securely, discovers Android devices, executes signed capture tasks, resumes multipart uploads, and runs as a native background service on macOS, Windows, and Linux.

**Architecture:** All business state lives in `perfpilot_agent`; OS adapters implement only credential storage, service lifecycle, and ADB location. The Agent makes outbound HTTPS requests using the deployment CA. It never opens a listening port. ADB commands are argument arrays bound to an explicit serial. A lease supervisor renews every 20 seconds and cancels the entire execution when the server requests cancellation or the lease is lost.

**Tech Stack:** Python 3.12, HTTPX, Pydantic, cryptography Ed25519, asyncio subprocesses, PyInstaller, macOS launchd/pkgbuild, Windows Service/pywin32/WiX, Linux systemd/dpkg-deb, pytest.

---

## Package layout

```text
agents/device-agent/
├── pyproject.toml
├── src/perfpilot_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── adb.py
│   ├── capture.py
│   ├── cli.py
│   ├── config.py
│   ├── control_client.py
│   ├── credentials.py
│   ├── devices.py
│   ├── executor.py
│   ├── logging.py
│   ├── registration.py
│   ├── security.py
│   ├── service.py
│   ├── state.py
│   ├── uploads.py
│   ├── platform/
│   │   ├── base.py
│   │   ├── linux.py
│   │   ├── macos.py
│   │   └── windows.py
│   └── resources/perfetto/
│       ├── startup.pbtxt
│       └── scroll.pbtxt
├── packaging/
│   ├── linux/
│   ├── macos/
│   └── windows/
└── tests/
    ├── contract/
    ├── integration/
    └── unit/
```

## Task 1: Scaffold configuration, credentials, registration, and signed-task verification

**Files:**
- Modify: `pyproject.toml`
- Create: `agents/device-agent/pyproject.toml`
- Create: `agents/device-agent/src/perfpilot_agent/__init__.py`
- Create: `agents/device-agent/src/perfpilot_agent/__main__.py`
- Create: `agents/device-agent/src/perfpilot_agent/config.py`
- Create: `agents/device-agent/src/perfpilot_agent/credentials.py`
- Create: `agents/device-agent/src/perfpilot_agent/security.py`
- Create: `agents/device-agent/src/perfpilot_agent/registration.py`
- Create: `agents/device-agent/src/perfpilot_agent/control_client.py`
- Create: `agents/device-agent/src/perfpilot_agent/platform/base.py`
- Create: `agents/device-agent/src/perfpilot_agent/platform/macos.py`
- Create: `agents/device-agent/src/perfpilot_agent/platform/windows.py`
- Create: `agents/device-agent/src/perfpilot_agent/platform/linux.py`
- Create: `agents/device-agent/tests/conftest.py`
- Create: `agents/device-agent/tests/unit/test_config.py`
- Create: `agents/device-agent/tests/unit/test_credentials.py`
- Create: `agents/device-agent/tests/unit/test_registration.py`
- Create: `agents/device-agent/tests/unit/test_security.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add the workspace package**

Add `agents/device-agent` to root workspace members and `agents/device-agent/tests` to pytest test paths. Use this package metadata:

```toml
[project]
name = "perfpilot-device-agent"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "cryptography>=48.0.1,<49",
  "httpx>=0.28.1,<0.29",
  "pydantic>=2.13.4,<2.14",
  "pydantic-settings>=2.12,<3",
]

[project.optional-dependencies]
windows = ["pywin32>=311; sys_platform == 'win32'"]
build = ["pyinstaller>=6.15,<7"]

[project.scripts]
perfpilot-agent = "perfpilot_agent.cli:main"
```

- [ ] **Step 2: Write failing configuration and security tests**

```python
def test_config_requires_https_and_absolute_ca(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AgentConfig(server_url="http://10.166.0.125", ca_bundle=tmp_path / "ca.crt")


def test_task_rejects_wrong_agent_and_expired_signature(task_verifier) -> None:
    token = signed_task(agent_id=OTHER_AGENT_ID, expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(TaskRejected):
        task_verifier.verify(token, expected_agent_id=AGENT_ID)
```

- [ ] **Step 3: Run RED**

```bash
PYTHONPATH=agents/device-agent/src .venv/bin/pytest -p no:cacheprovider agents/device-agent/tests/unit/test_config.py agents/device-agent/tests/unit/test_credentials.py agents/device-agent/tests/unit/test_registration.py agents/device-agent/tests/unit/test_security.py -q
```

Expected: FAIL because the package is absent.

- [ ] **Step 4: Implement immutable bootstrap configuration**

`AgentConfig` loads `/Library/Application Support/PerfPilot Agent/config.json` on macOS, `%ProgramData%\PerfPilot\Agent\config.json` on Windows, and `/etc/perfpilot-agent/config.json` on Linux. The file contains only:

```json
{
  "schema_version":"1.0",
  "server_url":"https://10.166.0.125",
  "ca_bundle":"/platform-specific/absolute/path/perfpilot-ca.crt",
  "adb_path":null,
  "workspace_root":"/platform-specific/absolute/path/work"
}
```

Require HTTPS, no userinfo/query/fragment, an absolute readable CA file, an absolute workspace path, and an optional absolute ADB path. The registration response may update credentials and the server task-signing public key; it may not replace `server_url` or CA trust.

- [ ] **Step 5: Implement OS credential stores**

Define `CredentialStore.load/save/delete`. Store `agent_id`, Ed25519 private key, refresh token, access token/expiry, and task-signing public key as one versioned value.

- macOS uses `/usr/bin/security` with argument arrays against `/Library/Keychains/System.keychain`, service `com.perfpilot.agent`, account `credentials`.
- Windows encrypts with `win32crypt.CryptProtectData(payload, "PerfPilot Agent", None, None, None, win32crypt.CRYPTPROTECT_LOCAL_MACHINE)` and writes the ciphertext to a SYSTEM-only file under `%ProgramData%\PerfPilot\Agent`.
- Linux uses `/var/lib/perfpilot-agent/credentials.json`, owner `root:root`, mode `0600`, atomic write plus `fsync` and rename.

Never print or include the stored value in exceptions. Unit tests inject an in-memory adapter; no test touches a real keychain.

- [ ] **Step 6: Implement registration and JWS verification**

`perfpilot-agent register` reads the registration code using `getpass.getpass`, generates an Ed25519 key in memory, posts the canonical public key and platform metadata, verifies the closed response contract, saves credentials, and overwrites the in-memory code variable. It must refuse a second registration unless `--replace` is supplied and confirmation is typed locally.

`TaskVerifier` accepts only `alg=EdDSA`, the installed `kid`, `aud=perfpilot-agent`, the local Agent ID, current lease version, known device digest, and an expiration no more than 90 seconds ahead. Unknown claims are rejected by Pydantic `extra="forbid"`.

- [ ] **Step 7: Run GREEN, update the lock, and commit**

```bash
.venv/bin/python -m pip install uv==0.11.32
.venv/bin/uv lock
.venv/bin/uv sync --locked --all-packages
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_config.py agents/device-agent/tests/unit/test_credentials.py agents/device-agent/tests/unit/test_registration.py agents/device-agent/tests/unit/test_security.py -q
.venv/bin/uv run --locked ruff check agents/device-agent/src agents/device-agent/tests
git add pyproject.toml uv.lock agents/device-agent
git commit -m "feat: scaffold cross-platform Device Agent"
```

## Task 2: Discover ADB and report independent device states

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/adb.py`
- Create: `agents/device-agent/src/perfpilot_agent/devices.py`
- Create: `agents/device-agent/src/perfpilot_agent/state.py`
- Create: `agents/device-agent/src/perfpilot_agent/logging.py`
- Create: `agents/device-agent/tests/fixtures/adb/devices-l.txt`
- Create: `agents/device-agent/tests/unit/test_adb.py`
- Create: `agents/device-agent/tests/unit/test_devices.py`
- Create: `agents/device-agent/tests/integration/test_heartbeat.py`

- [ ] **Step 1: Write failing ADB safety tests**

```python
async def test_every_device_command_is_serial_bound(fake_process) -> None:
    adb = AdbClient(binary=ADB, serial="R3CN30SECRET", runner=fake_process)
    await adb.run("shell", "getprop", "ro.product.model")
    assert fake_process.argv == [str(ADB), "-s", "R3CN30SECRET", "shell", "getprop", "ro.product.model"]


async def test_one_bad_device_does_not_hide_other_devices(inventory) -> None:
    inventory.responses["broken"] = TimeoutError()
    snapshot = await inventory.read_all()
    assert {item.adb_state for item in snapshot} == {"device", "offline"}
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_adb.py agents/device-agent/tests/unit/test_devices.py agents/device-agent/tests/integration/test_heartbeat.py -q
```

- [ ] **Step 3: Implement ADB discovery and process safety**

Resolve ADB in this order: configured absolute path, `ANDROID_HOME`/`ANDROID_SDK_ROOT` platform-tools, `PATH`, installer-managed platform-tools. Validate the binary is a regular executable outside the Agent workspace and call `adb version` with a five-second timeout.

Use `asyncio.create_subprocess_exec`; never use a shell. Discovery uses `adb devices -l`. Every device query includes `-s serial`, a five-second timeout, a 256 KiB output cap, and typed redacted errors. Allow only validated package/component names, integers, fixed ADB verbs, and paths created beneath the execution workspace.

- [ ] **Step 4: Implement inventory and heartbeat replacement**

For each device read manufacturer, model, Android release, API level, transport id, battery percentage/temperature, `/data` free bytes, ABI, fingerprint, and Perfetto availability. Classify `device`, `unauthorized`, `offline`, and `booting` independently. Generate an ephemeral `client_ref` UUID per observed serial and send the full snapshot every ten seconds or immediately after `adb devices -l` changes.

Consume the heartbeat response mapping:

```json
{"client_ref":"74000000-0000-4000-8000-000000000001",
 "device_id":"72000000-0000-4000-8000-000000000001",
 "device_digest":"64 lowercase hexadecimal characters"}
```

Keep `device_digest -> serial` only in process memory. The logging filter replaces every currently known serial, access token, refresh token, registration code prefix payload, and signed URL query with `[redacted]` before emission.

- [ ] **Step 5: Run GREEN and commit**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_adb.py agents/device-agent/tests/unit/test_devices.py agents/device-agent/tests/integration/test_heartbeat.py -q
.venv/bin/uv run --locked ruff check agents/device-agent/src agents/device-agent/tests
git add agents/device-agent
git commit -m "feat: discover and report Android devices"
```

## Task 3: Implement the lease supervisor and five-second cancellation

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/executor.py`
- Create: `agents/device-agent/src/perfpilot_agent/service.py`
- Create: `agents/device-agent/src/perfpilot_agent/cli.py`
- Modify: `agents/device-agent/src/perfpilot_agent/control_client.py`
- Create: `agents/device-agent/tests/unit/test_executor.py`
- Create: `agents/device-agent/tests/integration/test_task_loop.py`
- Create: `agents/device-agent/tests/integration/test_cancellation.py`

- [ ] **Step 1: Write failing lease-loss and cancellation tests**

```python
async def test_lease_loss_terminates_execution_and_blocks_completion(executor) -> None:
    executor.control.renew_result = LeaseLost()
    await executor.run(valid_task())
    assert executor.process_group.terminated
    assert executor.control.complete_calls == []


async def test_server_cancel_stops_capture_within_five_seconds(executor, fake_clock) -> None:
    executor.control.cancel_after = timedelta(seconds=1)
    await executor.run(valid_task())
    assert fake_clock.elapsed <= timedelta(seconds=5)
    assert executor.control.cancel_ack_calls == [EXECUTION_ID]
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_executor.py agents/device-agent/tests/integration/test_task_loop.py agents/device-agent/tests/integration/test_cancellation.py -q
```

- [ ] **Step 3: Implement control request semantics**

`ControlClient` owns one HTTPX client with the installed CA, connect timeout 5 seconds, read timeout 25 seconds, no redirects, no environment proxy, bounded JSON, and automatic access-token refresh. It retries only connection failures, `408`, `425`, `429`, and `5xx`, with capped exponential backoff plus jitter. Every mutation uses `execution_id` and lease version as idempotency/fencing data.

- [ ] **Step 4: Implement task loop and supervisor**

The service runs three structured-concurrency loops: heartbeat, task poll, and credential refresh. One Agent executes at most one task in v1 even when multiple devices are present. For an active task, renew every 20 seconds and poll control state at most every two seconds so cancellation reaches the process within five seconds under a healthy network.

Spawn local capture subprocesses in a new process group. On cancellation: stop the detached Perfetto session, terminate the process group, wait two seconds, kill survivors, preserve the bounded Agent log, remove incomplete artifacts, send `cancel-ack`, and resume heartbeats. On lease loss perform the same cleanup but never send completion or finalize a new upload.

- [ ] **Step 5: Implement service CLI**

Commands are exact and noninteractive except registration:

```text
perfpilot-agent register
perfpilot-agent run
perfpilot-agent status --json
perfpilot-agent doctor --json
perfpilot-agent unregister
```

`status` and `doctor` redact serials and credentials. `unregister` deletes local credentials only after server revocation succeeds, unless the operator passes `--local-only` and confirms locally.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_executor.py agents/device-agent/tests/integration/test_task_loop.py agents/device-agent/tests/integration/test_cancellation.py -q
.venv/bin/uv run --locked ruff check agents/device-agent/src agents/device-agent/tests
git add agents/device-agent
git commit -m "feat: supervise leased Agent tasks"
```

## Task 4: Capture startup, scroll, and memory evidence and resume uploads

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/capture.py`
- Create: `agents/device-agent/src/perfpilot_agent/uploads.py`
- Create: `agents/device-agent/src/perfpilot_agent/resources/perfetto/startup.pbtxt`
- Create: `agents/device-agent/src/perfpilot_agent/resources/perfetto/scroll.pbtxt`
- Create: `agents/device-agent/tests/unit/test_capture.py`
- Create: `agents/device-agent/tests/unit/test_uploads.py`
- Create: `agents/device-agent/tests/integration/test_execution.py`
- Create: `agents/device-agent/tests/integration/test_upload_resume.py`

- [ ] **Step 1: Write failing capture and resume tests**

```python
async def test_capture_installs_only_verified_task_apk(capture, object_store) -> None:
    await capture.prepare(valid_task())
    assert object_store.downloaded_sha256 == TASK_APK_SHA256
    assert ["install", "-r", "-t", str(capture.apk_path)] in capture.adb.calls


async def test_upload_resumes_from_confirmed_parts(uploader) -> None:
    uploader.server_parts = {1: "etag-1", 2: "etag-2"}
    await uploader.upload(TRACE_FILE)
    assert uploader.sent_part_numbers == [3, 4, 5, 6, 7, 8]
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_capture.py agents/device-agent/tests/unit/test_uploads.py agents/device-agent/tests/integration/test_execution.py agents/device-agent/tests/integration/test_upload_resume.py -q
```

- [ ] **Step 3: Download and verify immutable task input**

Request a five-minute input authorization from `/v1/agent/tasks/{execution_id}/inputs/{artifact_id}`. Download with redirects disabled into a `0700` execution directory, cap bytes to the signed size, compute SHA-256 while streaming, `fsync`, then rename. Reject mismatch before any ADB install.

- [ ] **Step 4: Capture deterministic scenario evidence**

For every task, record device properties and thermal readings before and after each scenario. Refuse measurement when battery exceeds 42°C, thermal status exceeds `LIGHT`, or the required source is unavailable. Recovery requires three passing readings ten seconds apart.

Use this sequence:

1. Install verified APK using `adb -s SERIAL install -r -t APK`.
2. Resolve the signed package/activity from the task and force-stop before startup capture.
3. Push the checked-in Perfetto config to `/data/local/tmp/perfpilot-EXECUTION.pbtxt`.
4. Start a detached session named `perfpilot-EXECUTION-startup`, launch the activity, stop/attach the session, and pull the trace.
5. For scroll, launch the activity, start `perfpilot-EXECUTION-scroll`, run bounded recipe swipes for 30 seconds, stop and pull.
6. For memory, collect baseline plus ten `dumpsys meminfo PACKAGE` rounds with signed recipe actions between rounds; store raw text, `metadata.json`, `summary.json`, and `memory_cycles.csv` in a tar archive.
7. Always remove the device config and trace files; uninstall only when the task snapshot says `cleanup_policy="uninstall"`.

No command is built through a shell string. The task may select only checked-in scenario operations; it cannot send arbitrary ADB arguments.

- [ ] **Step 5: Upload artifacts and submit the closed manifest**

Hash each file, reserve the allowed slot, split it into the server's 64 MiB parts, upload at most two parts concurrently, persist only part number/ETag in `upload-state.json`, and resume confirmed parts after restart. Finalize only while the lease is active. Submit one manifest containing artifact IDs, hashes, sizes, scenario timings, temperature gates, Agent version, ADB version, and stable diagnostic codes.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/unit/test_capture.py agents/device-agent/tests/unit/test_uploads.py agents/device-agent/tests/integration/test_execution.py agents/device-agent/tests/integration/test_upload_resume.py -q
.venv/bin/uv run --locked ruff check agents/device-agent/src agents/device-agent/tests
git add agents/device-agent
git commit -m "feat: execute leased Trace captures"
```

## Task 5: Build and smoke-test macOS, Windows, and Linux packages

**Files:**
- Create: `agents/device-agent/packaging/common/perfpilot-agent.spec`
- Create: `agents/device-agent/packaging/macos/build.sh`
- Create: `agents/device-agent/packaging/macos/com.perfpilot.agent.plist`
- Create: `agents/device-agent/packaging/macos/scripts/postinstall`
- Create: `agents/device-agent/packaging/macos/scripts/preinstall`
- Create: `agents/device-agent/packaging/windows/build.ps1`
- Create: `agents/device-agent/packaging/windows/PerfPilotAgent.wxs`
- Create: `agents/device-agent/packaging/windows/service.py`
- Create: `agents/device-agent/packaging/linux/build.sh`
- Create: `agents/device-agent/packaging/linux/perfpilot-agent.service`
- Create: `agents/device-agent/packaging/linux/postinst`
- Create: `agents/device-agent/packaging/linux/prerm`
- Create: `agents/device-agent/tests/packaging/test_package_metadata.py`
- Create: `.github/workflows/device-agent-packages.yml`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing package metadata tests**

```python
def test_every_package_runs_the_same_entrypoint(package_manifests) -> None:
    assert {manifest.entrypoint for manifest in package_manifests} == {"perfpilot-agent run"}


def test_packages_install_bootstrap_ca_and_no_credentials(package_manifests) -> None:
    assert all(manifest.includes_ca for manifest in package_manifests)
    assert all(not manifest.includes_credentials for manifest in package_manifests)
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests/packaging/test_package_metadata.py -q
```

- [ ] **Step 3: Build the frozen executable**

PyInstaller includes the two Perfetto configs and contract files, excludes tests and development credentials, and emits `perfpilot-agent`/`perfpilot-agent.exe`. Build on the target OS; never cross-compile Python binaries.

- [ ] **Step 4: Package native services**

- macOS installs under `/Library/PerfPilot Agent`, imports the deployment CA into the System keychain, and loads `com.perfpilot.agent` through `launchctl bootstrap system`.
- Windows installs under `%ProgramFiles%\PerfPilot Agent`, stores the CA file for HTTPX, and registers an automatic delayed-start LocalSystem service through WiX.
- Linux installs under `/opt/perfpilot-agent`, configuration in `/etc/perfpilot-agent`, state in `/var/lib/perfpilot-agent`, and a hardened systemd unit with `NoNewPrivileges=true`, `PrivateTmp=true`, and device/USB access retained.

Installers contain the deployment URL and CA but no registration code or Agent credentials. The first version is unsigned; package documentation must state the manual trust warning.

- [ ] **Step 5: Add native CI smoke tests**

The workflow matrix uses `macos-15`, `windows-2025`, and `ubuntu-24.04`. Each job builds the package, installs it, verifies service registration, runs `doctor --json` against fake ADB/API fixtures, stops it, upgrades the same version idempotently, uninstalls it, and proves credentials/workspace removal follows the selected uninstall option. Upload `.pkg`, `.msi`, `.deb`, and SHA-256 manifest artifacts.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests -q
.venv/bin/uv run --locked ruff check agents/device-agent/src agents/device-agent/tests
git add agents/device-agent .github/workflows
git commit -m "build: package Device Agent for three platforms"
```

## Plan 2 closure gate

Run locally:

```bash
.venv/bin/uv run --locked --package perfpilot-device-agent pytest -p no:cacheprovider agents/device-agent/tests -q
.venv/bin/uv run --locked ruff check agents/device-agent/src agents/device-agent/tests
```

Then trigger `device-agent-packages.yml` and require all three native jobs to pass. Download each artifact and compare it with the workflow SHA-256 manifest before using it in the LAN bootstrap bundle.
