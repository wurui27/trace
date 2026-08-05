# PerfPilot LAN Operations and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development before implementation and superpowers:verification-before-completion before each commit.

**Goal:** Close the LAN release with resource protection, restart reconciliation, encrypted backup/restore, host startup, and a measured real-device acceptance run from Agent registration through the final SmartPerfetto/AI report.

**Architecture:** PostgreSQL remains authoritative during recovery. A maintenance worker expires stale leases/uploads and enforces retention. Both SmartPerfetto and Android Memory acquire the same PostgreSQL advisory permit so only one heavy process runs. Host systemd starts Compose; timers call bounded maintenance and backup commands. Backups are encrypted before they leave `/data` and are accepted only after an isolated restore verifies database and object hashes.

**Tech Stack:** Python 3.12, PostgreSQL advisory locks, Docker Compose, Bash, systemd, pg_dump/pg_restore, MinIO client, age encryption, pytest, Node test runner, Playwright or browser acceptance, real Android/ADB.

---

## Task 1: Enforce capacity, stale-state reconciliation, and retention

**Files:**
- Create: `services/api/src/perfpilot_api/services/capacity.py`
- Create: `services/api/src/perfpilot_api/services/maintenance.py`
- Create: `services/api/src/perfpilot_api/workers/maintenance.py`
- Modify: `services/api/src/perfpilot_api/workers/trace_orchestrator.py`
- Modify: `services/api/src/perfpilot_api/workers/reconciler.py`
- Modify: `services/api/src/perfpilot_api/api/health.py`
- Modify: `services/api/src/perfpilot_api/api/uploads.py`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/config.py`
- Modify: `services/api/pyproject.toml`
- Modify: `infra/lan/compose.yaml`
- Create: `services/api/tests/unit/test_capacity.py`
- Create: `services/api/tests/unit/test_maintenance.py`
- Create: `services/api/tests/integration/test_heavy_worker_permit.py`
- Create: `services/api/tests/integration/test_reconciler_recovery.py`

- [ ] **Step 1: Write failing capacity tests**

```python
async def test_low_data_disk_rejects_new_upload_but_keeps_reports_readable(capacity) -> None:
    capacity.free_bytes = 49 * 1024**3
    with pytest.raises(CapacityUnavailable):
        await capacity.require_new_artifact_capacity()
    assert await capacity.allow_report_read()


async def test_only_one_heavy_engine_holds_the_global_permit(permit_factory) -> None:
    first = await permit_factory.try_acquire("smartperfetto", EXECUTION_A)
    second = await permit_factory.try_acquire("android-memory", EXECUTION_B)
    assert first is not None
    assert second is None
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_capacity.py services/api/tests/unit/test_maintenance.py services/api/tests/integration/test_heavy_worker_permit.py services/api/tests/integration/test_reconciler_recovery.py -q
```

- [ ] **Step 3: Implement readiness and admission gates**

Production settings are fixed to:

```text
data_root=/data/perfpilot
minimum_free_bytes=53687091200
raw_trace_retention_days=30
orphan_upload_retention_hours=24
expired_lease_grace_seconds=30
```

Before reserving any browser or Agent upload, require at least 50 GiB free plus the declared object size. Before scheduling heavy work, require PostgreSQL, Redis, object storage, and the selected engine health. `GET /v1/health` remains liveness-only; add `GET /v1/health/ready` returning only component names and `ok/degraded`, never addresses or credentials.

- [ ] **Step 4: Implement one shared heavy-worker permit**

Use one dedicated PostgreSQL connection and `pg_try_advisory_lock(0x50455246, 0x50494C4F)` for both SmartPerfetto and Android Memory. Hold the connection for the whole external-engine run and release in `finally`. If unavailable, leave the job queued; do not count it as a retry. Synthesis-only work does not acquire this permit.

- [ ] **Step 5: Reconcile and retain idempotently**

`MaintenanceService.run_once()` performs bounded batches:

1. Mark Agents/devices offline after 30 seconds.
2. Expire leases and fence their execution versions.
3. Abort multipart uploads expired for 24 hours.
4. Requeue only idempotent interrupted analysis steps.
5. Delete raw Trace objects older than 30 days only after the current report and provenance are durable.
6. Retain report bundles, canonical results, audit events, and object hashes.

Every row claim uses `FOR UPDATE SKIP LOCKED`; every delete is team-routed and emits a redacted audit event. Add `perfpilot-maintenance` and run one instance in Compose every five minutes.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_capacity.py services/api/tests/unit/test_maintenance.py services/api/tests/integration/test_heavy_worker_permit.py services/api/tests/integration/test_reconciler_recovery.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add services/api infra/lan
git commit -m "ops: enforce LAN capacity and recovery"
```

## Task 2: Add encrypted backups and isolated restore verification

**Files:**
- Create: `scripts/lib/perfpilot-lan-backup.sh`
- Create: `infra/lan/backup/manifest.schema.json`
- Create: `tests/perfpilot-lan-backup.test.mjs`
- Modify: `scripts/perfpilot-lan`
- Modify: `infra/lan/bootstrap-ubuntu.sh`
- Modify: `infra/lan/versions.env`
- Create: `docs/operations/lan-backup-restore.md`

- [ ] **Step 1: Write failing backup safety tests**

```javascript
test("backup encrypts before optional NFS copy", async () => {
  await harness.run("backup");
  assert.ok(harness.events.indexOf("age-encrypt") < harness.events.indexOf("nfs-copy"));
  assert.equal(harness.copiedPlaintext, false);
});

test("restore verify uses a fresh isolated target", async () => {
  await harness.run("restore-verify", "backup.age");
  assert.match(harness.restoreRoot, /^\/data\/perfpilot\/restore-test\.[A-Za-z0-9]+$/);
  assert.equal(harness.productionComposeTouched, false);
});
```

- [ ] **Step 2: Run RED**

```bash
node --test tests/perfpilot-lan-backup.test.mjs
```

- [ ] **Step 3: Create a deterministic backup set**

`scripts/perfpilot-lan backup` acquires an advisory backup lock, writes into `mktemp -d /data/perfpilot/backups/.staging.XXXXXX`, and captures:

- `pg_dump --format=custom` for the control database and every active team database;
- object inventory with bucket, key digest, version, size, SHA-256, and retention class;
- current report/canonical-result objects and raw traces still inside retention;
- platform/engine deployment receipts, migration heads, image digests, and non-secret configuration;
- a closed manifest containing file hashes and backup timestamp.

It never includes Redis, transient workspaces, plaintext secret files, signed URLs, raw Agent tokens, or deleted objects. After hash validation, archive and encrypt with `age` to the configured local backup recipient. Delete plaintext staging in a trap, then atomically rename the `.age` file and manifest receipt.

- [ ] **Step 4: Validate NFS before copying**

`backup --copy-nfs` requires `/mnt/nfs` to be a distinct mount, writable by the backup process, have at least twice the encrypted backup size free, pass a create/fsync/read/delete probe, and contain the expected marker file `.perfpilot-backup-target`. Copy only encrypted data to `/mnt/nfs/perfpilot-backups`, fsync, rehash, then mark the copy successful. A failed NFS copy does not invalidate the verified local backup but makes `doctor` degraded.

- [ ] **Step 5: Restore into disposable resources**

`scripts/perfpilot-lan restore-verify BACKUP.age` decrypts into a fresh `/data/perfpilot/restore-test.XXXXXX`, starts a separate Compose project bound only to an internal network, restores databases and objects, then verifies:

```text
control and tenant migration heads
administrator and team counts
Agent/device/analysis/report referential integrity
every restored object size and SHA-256
one report read through the API service layer
absence of plaintext credentials in the restored artifact set
```

It destroys only the validated temporary root and separate Compose project in a trap. It never accepts `/data/perfpilot`, `/opt/perfpilot`, `/`, `$HOME`, or an unresolved variable as a restore target.

- [ ] **Step 6: Run GREEN and commit**

```bash
node --test tests/perfpilot-lan-backup.test.mjs
git add scripts infra/lan docs/operations tests
git commit -m "ops: add encrypted backup and restore verification"
```

## Task 3: Add host startup, timers, log policy, and the operator runbook

**Files:**
- Create: `infra/lan/systemd/perfpilot.service`
- Create: `infra/lan/systemd/perfpilot-backup.service`
- Create: `infra/lan/systemd/perfpilot-backup.timer`
- Create: `infra/lan/systemd/perfpilot-doctor.service`
- Create: `infra/lan/systemd/perfpilot-doctor.timer`
- Create: `tests/perfpilot-lan-systemd.test.mjs`
- Create: `docs/operations/perfpilot-lan-runbook.md`
- Modify: `infra/lan/bootstrap-ubuntu.sh`
- Modify: `infra/lan/compose.yaml`
- Modify: `README.md`

- [ ] **Step 1: Write failing systemd and log tests**

Assert the main unit starts after Docker and mounted `/data`, runs Compose with an absolute file/project path, has no destructive stop command, and restarts only on failure. Assert the backup timer runs daily with persistent catch-up and the doctor timer runs every 15 minutes. Assert each Compose service uses the local log driver with `max-size=20m` and `max-file=5`.

- [ ] **Step 2: Run RED**

```bash
node --test tests/perfpilot-lan-systemd.test.mjs tests/lan-compose.test.mjs
```

- [ ] **Step 3: Install bounded host services**

`bootstrap-ubuntu.sh` installs the units under `/etc/systemd/system`, runs `systemctl daemon-reload`, enables `perfpilot.service`, and enables both timers. The main unit calls only:

```text
ExecStart=/opt/perfpilot/platform/current/scripts/perfpilot-lan start --systemd
ExecReload=/opt/perfpilot/platform/current/scripts/perfpilot-lan restart --systemd
ExecStop=/opt/perfpilot/platform/current/scripts/perfpilot-lan stop --systemd
```

`stop` uses Compose stop, not `down -v`, and never deletes bind data. Timers use `flock` files beneath `/run/perfpilot` to avoid overlapping work.

- [ ] **Step 4: Write the operational runbook**

Document exact commands and expected output for status, health, logs, Agent revocation, disk pressure, restart, safe reset, platform update/rollback, engine update, AI credential rotation, backup, restore verification, certificate renewal, lost Agent, Android unauthorized/offline, and incident export. Include the rule that production problems are never bypassed by disabling TLS, exposing ADB 5555, publishing data-service ports, or placing secrets in `.env` committed to Git.

- [ ] **Step 5: Run GREEN and commit**

```bash
node --test tests/perfpilot-lan-systemd.test.mjs tests/lan-compose.test.mjs
git add infra/lan docs/operations README.md tests
git commit -m "ops: add LAN service lifecycle and runbook"
```

## Task 4: Automate contract-level and browser LAN acceptance

**Files:**
- Create: `contracts/v1/acceptance/lan-receipt.schema.json`
- Create: `scripts/acceptance/lan-api-smoke.py`
- Create: `scripts/acceptance/lan-browser-smoke.mjs`
- Create: `scripts/acceptance/lan-agent-fake.py`
- Create: `scripts/acceptance/lan-run.sh`
- Create: `services/api/tests/integration/test_agent_team_isolation.py`
- Create: `tests/lan-browser-acceptance.test.tsx`
- Create: `tests/lan-acceptance-scripts.test.mjs`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing isolation and receipt tests**

```python
@pytest.mark.parametrize("resource", ["agent", "device", "lease", "upload", "analysis", "report"])
async def test_team_b_cannot_observe_team_a_resource(resource, lan_harness) -> None:
    response = await lan_harness.team_b_get(resource, owner="team-a")
    assert response.status_code == 404
    assert "team-a" not in response.text
```

The receipt schema requires boolean results and durations but forbids usernames, device serials/digests, tokens, signed URLs, object keys, paths, model prompts, and report evidence content.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_agent_team_isolation.py -q
npm run test:unit -- tests/lan-browser-acceptance.test.tsx
node --test tests/lan-acceptance-scripts.test.mjs
```

- [ ] **Step 3: Implement a fake-Agent acceptance path**

`lan-agent-fake.py` uses real HTTPS/CA and Agent contracts with fake ADB artifacts. It registers once, heartbeats ready/unauthorized/offline/multiple devices, leases a task, uploads deterministic multipart content with one forced disconnect/resume, acknowledges cancellation, and completes a second task. It proves API/queue/storage/report integration without requiring hardware and destroys its temporary team/Agent afterward.

- [ ] **Step 4: Implement browser acceptance**

Against `https://perfpilot.lan`, the browser smoke test logs in with credentials supplied through an inherited file descriptor, verifies no demo device values, generates one code, observes fake/real device states, selects a device, submits an APK, sees the dialog close, sees the background task card, cancels, submits again, waits for a report, and opens the full report page. Screenshots may contain only sanitized fake data and are stored outside Git unless explicitly reviewed.

- [ ] **Step 5: Produce a sanitized receipt**

`lan-run.sh` writes `/data/perfpilot/receipts/lan-acceptance-TIMESTAMP.json` with:

```json
{
  "schema_version":"1.0",
  "platform_commit":"40 lowercase hexadecimal characters",
  "smartperfetto_commit":"1508f99788bfcf18cc861e4bf4f8b472e84240c3",
  "android_memory_commit":"d5514972ced78c3faa7fc17589c1ea9231645056",
  "checks":{
    "registration":true,
    "inventory":true,
    "task_lease":true,
    "cancel_seconds":4.2,
    "upload_resume":true,
    "smartperfetto_report":true,
    "ai_report":true,
    "team_isolation":true,
    "restart_persistence":true,
    "restore_verification":true
  }
}
```

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_agent_team_isolation.py -q
npm run test:unit -- tests/lan-browser-acceptance.test.tsx
node --test tests/lan-acceptance-scripts.test.mjs
.venv/bin/ruff check services/api/src services/api/tests scripts/acceptance
git add contracts/v1/acceptance scripts/acceptance services/api/tests tests .github/workflows
git commit -m "test: add LAN end-to-end acceptance"
```

## Task 5: Execute the real-device release gate

- [ ] **Step 1: Verify host and backup before testing**

On Ubuntu:

```bash
/opt/perfpilot/platform/current/scripts/perfpilot-lan doctor
/opt/perfpilot/platform/current/scripts/perfpilot-lan backup --copy-nfs
/opt/perfpilot/platform/current/scripts/perfpilot-lan restore-verify latest
```

All must exit `0`. If `/mnt/nfs` fails its probe, record the local backup as successful but do not mark the release complete.

- [ ] **Step 2: Test native package lifecycle**

Use native CI for macOS, Windows, and Linux install/start/stop/upgrade/uninstall evidence. On the actual Mac, install the `.pkg`, verify the CA fingerprint, register the Agent, and check `perfpilot-agent doctor --json`. If the Android device attaches to Ubuntu instead, install the `.deb` there and use the same flow.

- [ ] **Step 3: Run one real Android task**

Connect and authorize the real Android device, confirm the page shows its real manufacturer/model and no full serial, upload a test APK, select all three scenarios, and start analysis. During the first run cancel from the page and measure process stop under five seconds. Start again, briefly disconnect/reconnect the network during Trace upload, and confirm resume does not create duplicate analysis or report records.

- [ ] **Step 4: Verify the final report and failure degradation**

With AI configured, require true SmartPerfetto metrics/findings and the three-round PerfPilot synthesis in the full report. Then run one controlled test with AI disabled: the SmartPerfetto core report must remain visible and the UI must say AI analysis did not complete. Restore AI afterward.

- [ ] **Step 5: Verify persistence and recovery**

Restart all Compose services, then reboot Ubuntu. After each, require the admin, Agent, device, completed report, and report URL to persist. Confirm no old lease remains active and the Agent reconnects without a new registration code.

- [ ] **Step 6: Run the full closure suite**

```bash
.venv/bin/ruff check services/api/src services/api/tests agents/device-agent/src agents/device-agent/tests
.venv/bin/pytest -p no:cacheprovider services/api/tests agents/device-agent/tests -q
npm run lint
npm run test:unit
npm run test:ssr
docker compose -f infra/lan/compose.yaml config --quiet
```

Then run `/opt/perfpilot/platform/current/scripts/acceptance/lan-run.sh` on Ubuntu and validate its receipt against `contracts/v1/acceptance/lan-receipt.schema.json`.

## Final completion rule

Do not call the LAN version complete until every receipt check is true, the NFS encrypted copy and isolated restore both pass, all three native package CI jobs pass, and at least one real Android device completes the cancel and successful-report flows. A missing AI provider credential, unavailable Windows/macOS runner, failed NFS probe, or no real authorized Android device is a factual release blocker, not a test to skip.
