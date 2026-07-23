# PerfPilot Phase 1 Device Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a hardened macOS/Linux Agent that leases one Android device, installs a task APK, restores a versioned fixture, captures valid startup/scroll/memory evidence, uploads immutable artifacts, and cleans the device before releasing it.

**Architecture:** The Agent is an outbound-only service authenticated by a versioned token. It verifies an Ed25519-signed task snapshot, performs all ADB operations through a serial-bound argument-array client, and writes each execution to `lease_id/scenario_job_id/execution_id`. Scenario adapters share fixture, environment, artifact, checkpoint, and cleanup services but keep their validity and output contracts separate.

**Tech Stack:** Python 3.12, uv workspace, HTTPX, Pydantic, cryptography Ed25519, ADB/platform-tools, Android UIAutomator and `android layout`, packaged `tracekit` capture resources, pytest.

---

## File map

- Create `agents/device-agent/pyproject.toml`: Agent package and workspace dependencies.
- Create `agents/device-agent/src/perfpilot_agent/config.py`: API, workspace, device, and timing settings.
- Create `agents/device-agent/src/perfpilot_agent/control_client.py`: registration, heartbeat, lease, checkpoint, sample, and artifact API.
- Create `agents/device-agent/src/perfpilot_agent/security.py`: Agent-token handling and task-snapshot verification.
- Create `agents/device-agent/src/perfpilot_agent/adb.py`: serial-bound subprocess boundary.
- Create `agents/device-agent/src/perfpilot_agent/devices.py`: discovery, capabilities, temperature, and quarantine evidence.
- Create `agents/device-agent/src/perfpilot_agent/fixtures/`: schema validation, actions, postconditions, dataset, and recorder.
- Create `agents/device-agent/src/perfpilot_agent/artifacts.py`: isolated workspaces, checksums, upload, and manifests.
- Create `agents/device-agent/src/perfpilot_agent/scenarios/{startup,scroll,memory}.py`: scenario-specific execution.
- Create `agents/device-agent/src/perfpilot_agent/executor.py`: lease workflow, checkpoints, sequencing, and stop handling.
- Create `agents/device-agent/src/perfpilot_agent/cleanup.py`: device and host cleanup.
- Create `agents/device-agent/src/perfpilot_agent/main.py`: long-running CLI.
- Create `agents/device-agent/fixtures/rkgallery/`: generated APK-bound fixture and dataset manifest.
- Create `agents/device-agent/tests/{conftest,factories}.py`: signing, ADB, control, scenario, clock, executor fixtures, and deterministic value builders.
- Create `agents/device-agent/tests/{unit,contract,integration}/`: fake-ADB, HTTP, fixture, scenario, and cleanup tests.
- Create `agents/device-agent/Dockerfile.linux`: optional Linux farm packaging; the first Mac runs the same Python entry point natively.

## Task 1: Scaffold the Agent and verify task snapshots

**Files:**
- Modify: `pyproject.toml`
- Create: `agents/device-agent/pyproject.toml`
- Create: `agents/device-agent/src/perfpilot_agent/{__init__,config,security,control_client}.py`
- Create: `agents/device-agent/tests/conftest.py`
- Create: `agents/device-agent/tests/factories.py`
- Create: `agents/device-agent/tests/unit/test_task_snapshot.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add the workspace package**

Append `agents/device-agent` to root workspace members and `agents/device-agent/tests` to the root pytest `testpaths` without removing tracekit or Worker entries from the parallel Packet 2 worktree. Its project depends on:

```toml
dependencies = [
  "cryptography>=46,<47",
  "httpx>=0.28,<0.29",
  "perfpilot-tracekit",
  "pydantic>=2.13.4,<2.14",
  "pydantic-settings>=2.12,<3",
]

[tool.uv.sources]
perfpilot-tracekit = { workspace = true }

[project.scripts]
perfpilot-agent = "perfpilot_agent.main:main"
```

- [ ] **Step 2: Define Agent fixtures before the tests use them**

Create `agents/device-agent/tests/factories.py` with fixed UUID constants `AGENT_ID`, `ANALYSIS_ID`, `SCENARIO_JOB_ID`, `LEASE_ID`, and `ATTEMPT_ID`; deterministic helpers `valid_fixture_dict`, `LAYOUT_JSON`, `valid_startup_manifest`, `hot`, `cool`, `segment`, and `overheat_on_round`; and explicit imports in every test module that uses those names.

Create `agents/device-agent/tests/conftest.py` and provide:

- `signing_key`, `PUBLIC_KEYS`, and `valid_snapshot_token` using a deterministic test-only Ed25519 key and fixed clock;
- `fake_subprocess` and `fake_adb`, both recording argument arrays and refusing `shell=True`;
- a checksum/version-aware `fake_object_store` built from the factory values;
- `fake_control` and `control`, which implement lease, input, checkpoint, sample-verdict, upload, completion, cancellation, and quarantine calls;
- `fake_clock`, `environment`, `startup_runner`, and `memory_runner` with explicit thermal readings and PID sequences;
- `executor`, `executor_factory`, and `SimulatedCrash` with injectable checkpoints.

All fixture bytes, IDs, timestamps, device readings, and server verdicts are deterministic; no unit or integration test invokes a physical device.

Keep imports of not-yet-created Agent modules inside the fixture that needs them. Task 1 signature tests must collect before ADB, scenario, executor, and cleanup modules exist.

- [ ] **Step 3: Write failing signature tests**

```python
def test_snapshot_requires_expected_audience_and_agent(signing_key) -> None:
    token = sign_snapshot(
        signing_key,
        audience="another-service",
        agent_id=AGENT_ID,
        scenario_job_id=SCENARIO_JOB_ID,
    )
    with pytest.raises(SnapshotRejected, match="audience"):
        verify_snapshot(token, expected_agent_id=AGENT_ID, public_keys=PUBLIC_KEYS)


def test_snapshot_exposes_only_versioned_artifact_ids(valid_snapshot_token) -> None:
    snapshot = verify_snapshot(
        valid_snapshot_token, expected_agent_id=AGENT_ID, public_keys=PUBLIC_KEYS
    )
    assert snapshot.analysis_id == ANALYSIS_ID
    assert snapshot.scenario_job_id == SCENARIO_JOB_ID
    assert snapshot.input_artifacts[0].version_id
    assert not hasattr(snapshot.input_artifacts[0], "bucket")
```

- [ ] **Step 4: Run RED**

```bash
uv lock
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_task_snapshot.py -q
```

Expected: Agent package and verifier are absent.

- [ ] **Step 5: Implement verification**

Parse only supported `kid` values, verify Ed25519, require `audience=perfpilot-agent`, match `agent_id`, reject expired or not-yet-valid snapshots, and validate the payload with the checked-in task-snapshot schema. Keep the Agent token in memory or an OS-protected file with mode `0600`; never log it.

- [ ] **Step 6: Run GREEN and commit**

```bash
uv lock
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_task_snapshot.py -q
git add pyproject.toml uv.lock agents/device-agent
git commit -m "feat: scaffold signed device Agent"
```

## Task 2: Implement the serial-bound ADB boundary and device inventory

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/{adb,devices}.py`
- Create: `agents/device-agent/tests/unit/{test_adb,test_devices}.py`
- Create: `agents/device-agent/tests/fixtures/adb/`

- [ ] **Step 1: Write failing ADB tests**

```python
async def test_every_device_command_includes_serial(fake_subprocess) -> None:
    client = AdbClient(serial="R3CN30TEST")
    await client.run("shell", "getprop", "ro.build.fingerprint")
    assert fake_subprocess.argv == [
        "adb", "-s", "R3CN30TEST", "shell", "getprop", "ro.build.fingerprint"
    ]


async def test_shell_fragments_are_rejected() -> None:
    client = AdbClient(serial="R3CN30TEST")
    with pytest.raises(UnsafeArgument):
        await client.run("shell", "am start; rm -rf /data/local/tmp")
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_adb.py \
  agents/device-agent/tests/unit/test_devices.py -q
```

Expected: `AdbClient` and capability parser are missing.

- [ ] **Step 3: Implement command execution**

Use `asyncio.create_subprocess_exec`, never `shell=True`. Validate package, component, file name, key event, integer, ratio, and action values before execution. Apply explicit timeouts, cap captured output, and raise typed errors containing exit code and redacted stderr.

- [ ] **Step 4: Implement inventory and environment readings**

Parse `adb devices -l`, then query API level, ABI, build fingerprint, display modes, battery level and temperature, thermal status, storage, root/profileable capabilities, and Perfetto data sources. A temperature source reports `value`, `source`, and `available`; unavailable never becomes zero or pass.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_adb.py \
  agents/device-agent/tests/unit/test_devices.py -q
git add agents/device-agent/src/perfpilot_agent agents/device-agent/tests
git commit -m "feat: inventory Android farm devices"
```

## Task 3: Implement versioned fixtures, actions, and postconditions

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/fixtures/{models,actions,postconditions,dataset}.py`
- Create: `agents/device-agent/tests/unit/{test_fixture_schema,test_fixture_actions}.py`
- Create: `contracts/v1/agent/scenario-fixture.schema.json`

- [ ] **Step 1: Write failing fixture tests**

```python
def test_fixture_requires_postcondition_after_navigation() -> None:
    payload = valid_fixture_dict()
    payload["scroll"]["prepare_actions"][0].pop("postcondition")
    with pytest.raises(ValidationError):
        ScenarioFixture.model_validate(payload)


def test_action_is_translated_to_argument_array() -> None:
    action = TapResourceId(value="com.demo:id/gallery")
    assert build_action(action, layout=LAYOUT_JSON) == [
        "shell", "input", "tap", "160", "420"
    ]
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_fixture_schema.py \
  agents/device-agent/tests/unit/test_fixture_actions.py -q
```

Expected: fixture models and action executor are absent.

- [ ] **Step 3: Implement the fixture schema**

The immutable fixture contains APK SHA-256, package, launch component, Android-version-aware permission grants, onboarding actions, locale, orientation, display mode, animation policy, compilation policy, app-data policy, cache policy, dataset manifest, scroll preparation/motion checks, and memory enter/operate/exit actions. Each state-changing action has an explicit postcondition.

Supported actions are `launch`, `tap_id`, `tap_text`, `tap_ratio`, `wait_text`, `keyevent`, `swipe`, and bounded `sleep`. Automated production acceptance forbids `manual_confirm`.

- [ ] **Step 4: Implement UI inspection**

Invoke `android layout --device "$PERFPILOT_DEVICE_SERIAL" --pretty` as the primary representation and `android layout --device "$PERFPILOT_DEVICE_SERIAL" --diff` after actions. The validated Agent setting supplies that exact serial. Fall back to `adb -s "$PERFPILOT_DEVICE_SERIAL" exec-out uiautomator dump /dev/tty` only when the Android CLI is unavailable. Save a screenshot plus layout JSON when a postcondition fails; do not continue the journey.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_fixture_schema.py \
  agents/device-agent/tests/unit/test_fixture_actions.py -q
git add agents/device-agent/src/perfpilot_agent/fixtures agents/device-agent/tests contracts/v1/agent
git commit -m "feat: add versioned Android fixtures"
```

## Task 4: Implement isolated workspaces and immutable artifact upload

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/artifacts.py`
- Create: `agents/device-agent/tests/unit/test_artifacts.py`
- Create: `agents/device-agent/tests/integration/test_artifact_upload.py`

- [ ] **Step 1: Write failing workspace tests**

```python
def test_execution_path_is_fully_scoped(tmp_path: Path) -> None:
    workspace = ArtifactWorkspace(tmp_path, LEASE_ID, SCENARIO_JOB_ID, ATTEMPT_ID)
    assert workspace.root == tmp_path / str(LEASE_ID) / str(SCENARIO_JOB_ID) / str(ATTEMPT_ID)


async def test_upload_finalizes_every_required_slot(fake_control, fake_object_store) -> None:
    result = await uploader.upload_manifest(valid_startup_manifest())
    assert [item.kind for item in result.artifacts] == [
        "trace", "perfetto_config", "capture_manifest", "capture_info", "agent_log"
    ]
    assert all(item.version_id for item in result.artifacts)
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_artifacts.py \
  agents/device-agent/tests/integration/test_artifact_upload.py -q
```

Expected: artifact workspace and uploader are missing.

- [ ] **Step 3: Implement local immutability and checksums**

Create directories with mode `0700`; reject symlinks and paths outside the execution root. Compute bytes and Base64 SHA-256 before requesting each signed URL, send exactly the signed MIME/checksum headers, then finalize by `upload_id`. Build sample and scenario manifests only from finalized version IDs.

- [ ] **Step 4: Implement required slot checks**

Enforce:

```text
startup attempt: trace.pb, config.txtpb, manifest.json, info.txt, agent.log
scroll run:      trace.pb, config.txtpb, scroll_manifest.json,
                 scroll_summary.json, agent.log
memory bundle:   metadata.json, summary.json, memory_cycles.csv,
                 baseline raw meminfo, ten round raw meminfo, agent.log
```

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_artifacts.py \
  agents/device-agent/tests/integration/test_artifact_upload.py -q
git add agents/device-agent/src/perfpilot_agent/artifacts.py agents/device-agent/tests
git commit -m "feat: upload immutable device artifacts"
```

## Task 5: Implement thermal recovery and process-cold startup collection

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/environment.py`
- Create: `agents/device-agent/src/perfpilot_agent/scenarios/startup.py`
- Create: `agents/device-agent/tests/unit/{test_environment,test_startup_scenario}.py`

- [ ] **Step 1: Write failing thermal and startup tests**

```python
async def test_recovery_requires_three_good_readings(fake_clock, environment) -> None:
    environment.readings = [hot(), cool(), cool(), hot(), cool(), cool(), cool()]
    await environment.wait_until_recovered()
    assert fake_clock.sleeps == [10, 10, 10, 10, 10, 10]


async def test_aot_speed_maps_to_android_speed(fake_adb, startup_runner) -> None:
    await startup_runner.prepare_compilation("aot_speed")
    assert [
        "shell", "cmd", "package", "compile", "-f", "-m", "speed", "com.demo"
    ] in fake_adb.calls
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_environment.py \
  agents/device-agent/tests/unit/test_startup_scenario.py -q
```

Expected: environment gate and startup runner are absent.

- [ ] **Step 3: Implement per-attempt gates**

Reject an RKGallery attempt when battery exceeds 42°C, SoC exceeds 65°C, thermal status exceeds `LIGHT(1)`, or required sources are unavailable. Recovery requires three passing readings ten seconds apart. Record start/end values in every manifest.

- [ ] **Step 4: Implement process-cold collection**

For each attempt: restore fixture state, apply and verify `compiler_filter=speed`, perform the declared non-measurement warm-up outside the capture, `force-stop`, confirm the process is absent, run packaged `trace_app.sh` with explicit output root, serial, activity, and `TRACE_AUTOFILL_EXCEL=0`, record observed PID, upload, and submit the sample manifest. Disable the legacy implicit preflight launch.

Continue until the server reports five valid samples, ten attempts, or a deterministic error. Never trust local `valid_capture` as the server verdict.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_environment.py \
  agents/device-agent/tests/unit/test_startup_scenario.py -q
git add agents/device-agent/src/perfpilot_agent agents/device-agent/tests
git commit -m "feat: collect valid process-cold starts"
```

## Task 6: Implement 30-second segmented scroll collection

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/scenarios/scroll.py`
- Create: `agents/device-agent/tests/unit/test_scroll_scenario.py`

- [ ] **Step 1: Write failing validity tests**

```python
def test_round_trip_motion_uses_segments_not_endpoints() -> None:
    evidence = verify_motion(
        [segment(changed=True), segment(changed=True), segment(changed=True)],
        start_signature="same",
        end_signature="same",
    )
    assert evidence.confirmed_segments == 3


def test_crash_with_healthy_trace_is_submitted_not_retried() -> None:
    result = classify_scroll_run(healthy_trace=True, frames=0, crash=True)
    assert result.local_outcome == "crash"
    assert result.submit_for_server_validation
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_scroll_scenario.py -q
```

Expected: scroll scenario module is absent.

- [ ] **Step 3: Implement navigation and measurement**

Restore the scroll fixture, execute each preparation action and postcondition, wait for stability, start a 30-second Perfetto capture, inject the declared swipe sequence, and sample foreground component, PID, layout diff, and motion evidence at least every five seconds. Keep navigation and reset outside the measurement window.

- [ ] **Step 4: Implement stop behavior**

Upload and submit each run independently. PID switch, wrong page, automation loss, incomplete window, or no motion/crash evidence is locally invalid. Zero frames, low frames, motion stall, crash, or ANR in a healthy trace is submitted as a real result. Stop at five server-valid runs or ten attempts.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_scroll_scenario.py -q
git add agents/device-agent/src/perfpilot_agent/scenarios/scroll.py agents/device-agent/tests
git commit -m "feat: collect segmented scroll traces"
```

## Task 7: Implement the ten-round memory bundle

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/scenarios/memory.py`
- Create: `agents/device-agent/tests/unit/test_memory_scenario.py`
- Copy: `/Users/ray/Desktop/trace/tools/capture/memory_cycle_test.py` to `agents/device-agent/src/perfpilot_agent/legacy_memory_cycle.py`

- [ ] **Step 1: Write failing loop tests**

```python
async def test_memory_keeps_one_pid_for_baseline_and_ten_rounds(memory_runner) -> None:
    result = await memory_runner.run()
    assert len(result.rounds) == 11
    assert {row.pid for row in result.rounds} == {3210}


async def test_mid_run_thermal_failure_retries_whole_bundle_once(memory_runner) -> None:
    memory_runner.environment = overheat_on_round(four=1, then_cool=True)
    await memory_runner.run()
    assert memory_runner.bundle_attempts == 2
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_memory_scenario.py -q
```

Expected: memory runner is absent.

- [ ] **Step 3: Refactor the existing collector**

Move meminfo parsing, action execution, summary building, and CSV output behind injected `AdbClient`, clock, environment gate, and fixture services. Run one cold start, record baseline, execute ten enter/operate/exit journeys with postconditions, wait the fixture delay, and capture `dumpsys meminfo -d` after every exit.

- [ ] **Step 4: Enforce bundle validity**

Abort on PID change, missing Activity/page postcondition, missing required meminfo field, ADB loss, or unhandled dialog. If thermal limits are crossed, invalidate the entire bundle, cool down, and retry exactly once. Memory collection does not create a fake Trace.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/unit/test_memory_scenario.py -q
git add agents/device-agent/src/perfpilot_agent agents/device-agent/tests
git commit -m "feat: collect memory-cycle evidence"
```

## Task 8: Orchestrate leases, checkpoints, cancellation, and cleanup

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/{executor,cleanup,main}.py`
- Create: `agents/device-agent/tests/integration/{test_executor,test_cleanup}.py`

- [ ] **Step 1: Write failing recovery tests**

```python
async def test_restart_resumes_only_with_same_active_lease(executor_factory, control) -> None:
    first = executor_factory(crash_after="apk_installed")
    with pytest.raises(SimulatedCrash):
        await first.run_once()
    second = executor_factory()
    await second.run_once()
    assert control.install_count == 1


async def test_cleanup_failure_quarantines_device(executor, fake_adb, control) -> None:
    fake_adb.fail_on("uninstall")
    await executor.finish_parent()
    assert control.device_state == "quarantined"
    assert control.lease_state != "released"
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/integration/test_executor.py \
  agents/device-agent/tests/integration/test_cleanup.py -q
```

Expected: executor and cleanup services are absent.

- [ ] **Step 3: Implement execution and checkpoints**

Long-poll tasks, verify the snapshot, confirm the lease, download and hash the APK, uninstall same-package residue, install the exact version, and execute `cold_start → scroll → memory_cycle`. Persist server-confirmed checkpoints for APK download, install, execution, and artifact finalize. Resume only on the same Agent, device, and active lease.

- [ ] **Step 4: Implement cancellation and cleanup**

Heartbeat every ten seconds. On cancellation or revoked lease, stop after the current atomic ADB operation and do not finalize new results. Before changing display mode, orientation, locale, or animation settings, checkpoint their original values. When the control service confirms no more device execution, force-stop, clear data, uninstall, restore every checkpointed system setting, delete only manifest-listed task dataset files and explicit device-side traces/screenshots/downloads, clear logcat, remove the exact lease workspace, and revoke task network/account state. Never recursively delete a shared media, home, temp, or workspace root. Release an active lease only after all cleanup succeeds; otherwise quarantine.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/integration/test_executor.py \
  agents/device-agent/tests/integration/test_cleanup.py -q
git add agents/device-agent/src/perfpilot_agent agents/device-agent/tests
git commit -m "feat: orchestrate and clean device jobs"
```

## Task 9: Generate and validate the RKGallery fixture

**Files:**
- Create: `agents/device-agent/src/perfpilot_agent/fixtures/recorder.py`
- Create: `agents/device-agent/tests/integration/test_fixture_recorder.py`
- Create: `agents/device-agent/fixtures/rkgallery/fixture.json`
- Create: `agents/device-agent/fixtures/rkgallery/dataset-manifest.json`
- Create: `agents/device-agent/fixtures/rkgallery/README.md`

- [ ] **Step 1: Build the recorder test**

The fake-device test records package/activity from `apkanalyzer manifest`, captures layout-backed selectors, hashes every dataset file, writes an APK-bound fixture, and rejects a final fixture containing `manual_confirm`, an unverified postcondition, or an empty dataset.

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-device-agent pytest \
  agents/device-agent/tests/integration/test_fixture_recorder.py -q
```

Expected: fixture recorder is absent.

- [ ] **Step 3: Record against the supplied APK and device**

Run:

```bash
uv run perfpilot-agent fixture record \
  --apk "$RKGALLERY_APK_PATH" \
  --serial "$RKGALLERY_DEVICE_SERIAL" \
  --dataset "$RKGALLERY_DATASET_DIR" \
  --output agents/device-agent/fixtures/rkgallery/fixture.json
```

The command derives package, launch Activity, APK SHA-256, permissions, display facts, and dataset fingerprint. The operator demonstrates onboarding, target gallery page, segmented scroll, and memory enter/operate/exit journeys; the recorder stores stable resource IDs or text/content descriptions plus postconditions. It captures screenshots only as review evidence, not as selectors when stable UI metadata exists.

- [ ] **Step 4: Replay from a clean install**

```bash
uv run perfpilot-agent fixture verify \
  --fixture agents/device-agent/fixtures/rkgallery/fixture.json \
  --serial "$RKGALLERY_DEVICE_SERIAL" \
  --clean-install
```

Expected: permission setup, onboarding, dataset restoration, scroll journey, memory journey, compilation policy, cache policy, and every postcondition pass without manual input.

- [ ] **Step 5: Commit the fixture, not the APK or media**

```bash
git add agents/device-agent/fixtures/rkgallery agents/device-agent/src/perfpilot_agent/fixtures/recorder.py agents/device-agent/tests
git commit -m "test: add RKGallery device fixture"
```

The dataset manifest contains names, bytes, hashes, and fingerprint; the customer APK and media files remain outside Git.

## Task 10: Package and verify the Agent

**Files:**
- Create: `agents/device-agent/Dockerfile.linux`
- Create: `agents/device-agent/launchd/com.perfpilot.agent.plist.example`
- Modify: `.github/workflows/platform-ci.yml`
- Modify: `README.md`

- [ ] **Step 1: Add contract compatibility tests**

Validate every Agent request/response and manifest against `contracts/v1`. Run fake-ADB end-to-end execution from signed snapshot through cleanup and assert no bucket, DSN, token, or signed URL enters logs.

- [ ] **Step 2: Run packet verification**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv sync --locked --all-packages --dev
uv run ruff check agents/device-agent
uv run --package perfpilot-device-agent pytest agents/device-agent/tests -q
uv run --package perfpilot-tracekit pytest tracekit/tests -q
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 3: Run the Mac smoke test**

```bash
adb devices -l
uv run perfpilot-agent doctor --serial "$RKGALLERY_DEVICE_SERIAL"
```

Expected: exactly the selected device reports ready capabilities, temperature sources, storage, Perfetto support, and no active lease. This command does not install or mutate an app.

- [ ] **Step 4: Commit and push**

```bash
git add agents/device-agent/Dockerfile.linux agents/device-agent/launchd README.md .github/workflows/platform-ci.yml
git commit -m "build: package PerfPilot device Agent"
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
git status --short
```

Expected: remote and local SHAs match and status is empty.
