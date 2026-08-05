# PerfPilot Agent Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development before implementation and superpowers:verification-before-completion before each commit.

**Goal:** Add team-scoped Agent registration, sanitized device inventory, signed task leases, resumable capture upload, coordinated cancellation, and desktop-web device selection to the current FastAPI/React platform.

**Architecture:** Browser routes remain protected by the existing signed same-origin proxy, session cookie, CSRF token, and team membership. `/v1/agent/*` routes bypass browser proxy authentication and use short-lived opaque Agent access tokens. PostgreSQL is authoritative for Agents, device digests, jobs, leases, and upload state; Redis only wakes long polls. Task snapshots are compact Ed25519 JWS values. Raw ADB serials exist only in a TLS request long enough to compute an HMAC digest and masked suffix.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, Redis, boto3/S3 multipart upload, cryptography Ed25519, React/Vinext, Vitest, pytest.

---

## Endpoint contract

Browser routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/teams/{team_id}/agents/registration-codes` | Create a single-use ten-minute code |
| `GET` | `/v1/teams/{team_id}/agents` | List team Agents without credentials |
| `PATCH` | `/v1/teams/{team_id}/agents/{agent_id}` | Rename an Agent |
| `POST` | `/v1/teams/{team_id}/agents/{agent_id}/revoke` | Revoke credentials and leases |
| `GET` | `/v1/teams/{team_id}/devices` | List sanitized team devices |

Agent routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/agent/register` | Consume registration code and issue credentials |
| `POST` | `/v1/agent/token/refresh` | Rotate refresh/access credentials with key proof |
| `POST` | `/v1/agent/heartbeat` | Replace the Agent's device snapshot |
| `GET` | `/v1/agent/tasks/next?wait_seconds=20` | Long-poll for one signed task or cancellation |
| `POST` | `/v1/agent/tasks/{execution_id}/renew` | Renew a 60-second lease |
| `GET` | `/v1/agent/tasks/{execution_id}/inputs/{artifact_id}` | Authorize one immutable task input download |
| `POST` | `/v1/agent/tasks/{execution_id}/uploads` | Reserve a multipart output slot |
| `POST` | `/v1/agent/tasks/{execution_id}/uploads/{upload_id}/parts` | Sign one part |
| `POST` | `/v1/agent/tasks/{execution_id}/uploads/{upload_id}/complete` | Complete and validate multipart upload |
| `POST` | `/v1/agent/tasks/{execution_id}/complete` | Submit the execution manifest once |
| `POST` | `/v1/agent/tasks/{execution_id}/cancel-ack` | Confirm process termination and cleanup |

Every response uses `schema_version: "1.0"`. Browser responses never contain raw serials, token digests, public keys, object keys, storage upload IDs, signed URLs outside an active upload response, or filesystem paths.

## Task 1: Publish Agent contracts and migrate storage

**Files:**
- Create: `contracts/v1/agents/registration-code-response.schema.json`
- Create: `contracts/v1/agents/registration-request.schema.json`
- Create: `contracts/v1/agents/registration-response.schema.json`
- Create: `contracts/v1/agents/heartbeat-request.schema.json`
- Create: `contracts/v1/agents/device-list-response.schema.json`
- Create: `contracts/v1/agents/task-poll-response.schema.json`
- Create: `contracts/v1/agents/task-snapshot.schema.json`
- Create: `contracts/v1/agents/execution-manifest.schema.json`
- Create: `contracts/v1/examples/agent-*.valid.json`
- Create: `services/api/tests/contract/test_agent_contracts.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/agents.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/jobs.py`
- Modify: `services/api/src/perfpilot_api/db/tenant/models/artifacts.py`
- Modify: `services/api/src/perfpilot_api/db/tenant/models/__init__.py`
- Create: `services/api/migrations/control/versions/0009_remote_device_agents.py`
- Create: `services/api/migrations/tenant/versions/0008_agent_multipart_uploads.py`
- Modify: `services/api/tests/integration/test_migrations.py`

- [ ] **Step 1: Write failing schema tests**

Add deterministic valid examples and assert that extra fields and a raw `serial` are rejected:

```python
def test_device_list_contract_never_accepts_raw_serial() -> None:
    payload = load("contracts/v1/examples/agent-device-list.valid.json")
    payload["devices"][0]["serial"] = "R3CN30SECRET"
    errors = list(validator("agents/device-list-response").iter_errors(payload))
    assert [error.validator for error in errors] == ["additionalProperties"]


def test_task_snapshot_binds_agent_device_execution_and_lease() -> None:
    payload = load("contracts/v1/examples/agent-task-snapshot.valid.json")
    assert set(payload) >= {
        "agent_id", "device_digest", "execution_id", "lease_version", "expires_at"
    }
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/contract/test_agent_contracts.py -q
```

Expected: FAIL because `contracts/v1/agents` does not exist.

- [ ] **Step 3: Define closed versioned contracts**

The device list shape is exactly:

```json
{
  "schema_version": "1.0",
  "devices": [{
    "device_id": "72000000-0000-4000-8000-000000000001",
    "agent_id": "71000000-0000-4000-8000-000000000001",
    "agent_name": "Ray Mac",
    "serial_suffix": "7K2A",
    "manufacturer": "UNISOC",
    "model": "ums9620",
    "android_release": "15",
    "api_level": 35,
    "connection_type": "usb",
    "adb_state": "device",
    "state": "ready",
    "last_seen_at": "2026-08-05T08:00:00Z"
  }]
}
```

Set `additionalProperties: false` at every object level. Cap heartbeat devices at 32 and execution manifest artifacts at 32. Cap every display string and diagnostic code; do not permit free-form stderr.

- [ ] **Step 4: Write failing migration tests**

Assert these storage changes:

```python
assert {"team_id", "owner_user_id", "public_key_b64", "platform",
        "agent_version", "access_token_expires_at", "refresh_token_digest",
        "refresh_token_expires_at"} <= columns("agents")
assert "serial" not in columns("devices")
assert {"team_id", "serial_digest", "serial_suffix", "manufacturer", "model",
        "android_release", "connection_type", "adb_state"} <= columns("devices")
assert {"selected_device_id"} <= columns("global_jobs")
assert {"execution_id", "renewed_at", "cancel_acknowledged_at",
        "task_snapshot_digest"} <= columns("agent_leases")
assert "artifact_multipart_uploads" in TENANT_TABLES
```

Also test unique `(team_id, name)`, unique `serial_digest`, one active lease per device, and one lease per `execution_id`. Migration `0009` must lock and refuse upgrade if `agents` or `devices` contains rows, because existing plaintext serials cannot be safely converted without the runtime HMAC secret.

- [ ] **Step 5: Implement models and migrations**

Use these state sets:

```text
Agent.state: pending, online, offline, revoked
Device.state: ready, busy, unauthorized, booting, quarantined, offline
Device.adb_state: device, unauthorized, offline, booting
AgentLease.state: active, cancel_requested, released, expired, revoked
ArtifactMultipartUpload.state: pending, completed, aborted, expired
```

`Agent.team_id`, `Agent.owner_user_id`, and `Device.team_id` are non-null foreign keys. `GlobalJob.selected_device_id` is nullable for non-device modes and required by a check constraint when `analysis_mode='device'` after creation reaches `queued`. Store multipart transport data only in the tenant database.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/contract/test_agent_contracts.py -q
PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres .venv/bin/pytest -p no:cacheprovider services/api/tests/integration/test_migrations.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add contracts/v1/agents contracts/v1/examples services/api/src/perfpilot_api/db services/api/migrations services/api/tests
git commit -m "feat: add Agent registration contracts and storage"
```

## Task 2: Register, refresh, list, rename, and revoke Agents

**Files:**
- Create: `services/api/src/perfpilot_api/security/agent_credentials.py`
- Create: `services/api/src/perfpilot_api/security/agent_signatures.py`
- Create: `services/api/src/perfpilot_api/services/agents.py`
- Create: `services/api/src/perfpilot_api/api/agents.py`
- Create: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/config.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Create: `services/api/tests/unit/test_agent_credentials.py`
- Create: `services/api/tests/unit/test_agent_service.py`
- Create: `services/api/tests/integration/test_agent_api.py`

- [ ] **Step 1: Write failing credential tests**

```python
def test_registration_code_is_single_use_and_expires(agent_service) -> None:
    issued = await agent_service.create_registration_code(
        team_id=TEAM_ID, owner_user_id=USER_ID, name="Ray Mac"
    )
    await agent_service.register(valid_registration(issued.code))
    with pytest.raises(RegistrationCodeRejected):
        await agent_service.register(valid_registration(issued.code))


def test_refresh_rotates_both_tokens(agent_service, registered_agent) -> None:
    refreshed = await agent_service.refresh(registered_agent.refresh_request())
    assert refreshed.access_token != registered_agent.access_token
    assert refreshed.refresh_token != registered_agent.refresh_token
    with pytest.raises(AgentAuthenticationRejected):
        await agent_service.refresh(registered_agent.refresh_request())
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_agent_credentials.py services/api/tests/unit/test_agent_service.py services/api/tests/integration/test_agent_api.py -q
```

Expected: FAIL because the Agent service and routes are absent.

- [ ] **Step 3: Implement opaque credentials and key proof**

Issue tokens with these fixed policies:

```text
registration code: ppreg_ + 32 random bytes, 10 minutes, one use
access token:       ppat_  + 32 random bytes, 15 minutes
refresh token:      pprt_  + 32 random bytes, 30 days, rotate on every use
```

Digest all three with HMAC-SHA256 using `agent_registration_secret_reference`. Verify with `hmac.compare_digest`. Registration accepts only a 44-character canonical Base64 Ed25519 public key. Refresh requires a signature over `agent_id + newline + nonce + newline + timestamp`; reject a timestamp outside 60 seconds and reserve each nonce in Redis for 120 seconds.

- [ ] **Step 4: Implement browser and Agent routes**

`api/agents.py` uses `proxy_router_dependencies()` plus the existing `AuthService.authorize_team_request`. Only `team_owner` may create/revoke codes; team members may list devices. `api/agent_control.py` never accepts cookies, browser CSRF, proxy signatures, `team_id`, bucket, or database identifiers from the Agent.

Add `agent_service` injection to `create_app()` following the existing `analysis_service` pattern. In production it uses the control session factory, Redis nonce store, and the encrypted secret store. In tests it accepts an injected fake and opens no external connection.

- [ ] **Step 5: Test isolation and redaction**

Integration tests must prove:

- a code for Team A cannot register into Team B;
- Team B receives `404`, not a distinguishing `403`, for Team A's Agent;
- revoked access and refresh tokens fail immediately;
- API responses and exception text contain no code, token, digest, public key, or raw serial;
- `/v1/agent/register` works without proxy headers, while browser Agent routes reject missing proxy headers.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_agent_credentials.py services/api/tests/unit/test_agent_service.py services/api/tests/integration/test_agent_api.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: register and authenticate remote Agents"
```

## Task 3: Replace heartbeats and publish sanitized device inventory

**Files:**
- Create: `services/api/src/perfpilot_api/services/device_directory.py`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/api/agents.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Create: `services/api/tests/unit/test_device_directory.py`
- Modify: `services/api/tests/integration/test_agent_api.py`

- [ ] **Step 1: Write failing heartbeat tests**

```python
async def test_heartbeat_replaces_snapshot_and_masks_serial(directory) -> None:
    await directory.replace_heartbeat(AGENT_ID, heartbeat(serial="R3CN30ABC7K2A"))
    view = await directory.list_devices(TEAM_ID)
    assert view[0].serial_suffix == "7K2A"
    assert "R3CN30ABC7K2A" not in repr(view)


async def test_stale_agent_and_devices_are_offline(directory, clock) -> None:
    await directory.replace_heartbeat(AGENT_ID, heartbeat())
    clock.advance(seconds=31)
    await directory.expire_stale()
    assert (await directory.list_devices(TEAM_ID))[0].state == "offline"
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_device_directory.py services/api/tests/integration/test_agent_api.py -q
```

- [ ] **Step 3: Implement full-snapshot replacement**

Compute `serial_digest = HMAC-SHA256(agent_serial_hmac_key, utf8(serial))` and keep only the final four Unicode-safe ASCII characters as `serial_suffix`. Never place the raw serial in a dataclass, ORM attribute, exception, structured log, or return value. Match moved devices by digest and reject movement while an unexpired active lease exists.

One transaction locks the Agent, upserts every reported digest, marks omitted devices offline, records Agent clock skew/disk/slot status in bounded JSON, and updates `last_heartbeat_at`. A separate `expire_stale()` marks an Agent and its devices offline at 30 seconds.

- [ ] **Step 4: Add heartbeat and device-list APIs**

`POST /v1/agent/heartbeat` accepts at most 32 devices. Every submitted device has an Agent-generated ephemeral `client_ref`. The response maps that reference to the server identity needed to execute a signed task:

```json
{
  "schema_version":"1.0",
  "accepted_at":"2026-08-05T08:00:00Z",
  "next_heartbeat_seconds":10,
  "devices":[{
    "client_ref":"74000000-0000-4000-8000-000000000001",
    "device_id":"72000000-0000-4000-8000-000000000001",
    "device_digest":"64 lowercase hexadecimal characters"
  }]
}
```

This digest mapping is returned only to the authenticated owning Agent. It is never returned by the browser device-list endpoint.

`GET /v1/teams/{team_id}/devices` orders `ready`, `busy`, `unauthorized`, `booting`, `quarantined`, `offline`, then Agent name and model. It returns the closed contract from Task 1.

- [ ] **Step 5: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_device_directory.py services/api/tests/integration/test_agent_api.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: publish team device inventory"
```

## Task 4: Bind device analyses and lease signed tasks

**Files:**
- Modify: `contracts/v1/analyses/create-request.schema.json`
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Modify: `contracts/v1/events/event-envelope.schema.json`
- Create: `services/api/src/perfpilot_api/security/task_snapshots.py`
- Create: `services/api/src/perfpilot_api/services/agent_tasks.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Create: `services/api/src/perfpilot_api/workers/scheduler.py`
- Create: `services/api/tests/unit/test_task_snapshots.py`
- Create: `services/api/tests/unit/test_agent_task_service.py`
- Modify: `services/api/tests/unit/test_analysis_service.py`
- Modify: `services/api/tests/integration/test_analysis_api.py`
- Create: `services/api/tests/integration/test_agent_task_api.py`

- [ ] **Step 1: Add `device_id` to the closed create contract**

The device branch now requires this property:

```json
"device_id": {"type": "string", "format": "uuid"}
```

Update `CreateDeviceAnalysisRequest`, `canonical_analysis_request_hash`, `AnalysisService.create_device_analysis`, and repository reservation to bind the selected device id. Before creating a job, require the device to belong to the request team and have state `ready`; return one nondisclosing `resource_not_found` response for wrong-team and missing devices, and `device_unavailable` for a known but unusable device.

- [ ] **Step 2: Write failing lease and signing tests**

```python
async def test_only_selected_agent_can_poll_signed_task(task_service) -> None:
    await task_service.schedule(analysis_id=ANALYSIS_ID)
    assert await task_service.poll(OTHER_AGENT_ID, wait_seconds=0) is None
    task = await task_service.poll(AGENT_ID, wait_seconds=0)
    claims = verify_task_jws(task.snapshot_jws, SERVER_PUBLIC_KEY)
    assert claims["device_digest"] == DEVICE_DIGEST
    assert claims["lease_version"] == 1


async def test_renew_is_idempotent_and_fenced(task_service) -> None:
    first = await task_service.renew(EXECUTION_ID, lease_version=1)
    again = await task_service.renew(EXECUTION_ID, lease_version=1)
    assert again == first
    with pytest.raises(StaleLeaseVersion):
        await task_service.renew(EXECUTION_ID, lease_version=0)
```

- [ ] **Step 3: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_task_snapshots.py services/api/tests/unit/test_agent_task_service.py services/api/tests/integration/test_agent_task_api.py -q
```

- [ ] **Step 4: Implement scheduling, leases, and Redis wakeup**

`Scheduler.run_once()` claims queued device analyses with `FOR UPDATE SKIP LOCKED`, verifies Agent/device freshness, creates one 60-second `AgentLease`, changes device to `busy`, and wakes `perfpilot:agent:{agent_id}:tasks`. Heavy analysis concurrency is not consumed during device capture.

`AgentTaskService.poll()` waits no longer than the requested 0–20 seconds. It loads authoritative state after every Redis wakeup; Redis payloads contain only the Agent ID and are never authoritative. `renew()` extends expiry from server time by 60 seconds and returns `renew_after_seconds: 20`.

Sign compact JWS with EdDSA and claims:

```json
{
  "aud":"perfpilot-agent",
  "agent_id":"71000000-0000-4000-8000-000000000001",
  "device_digest":"64 lowercase hexadecimal characters",
  "execution_id":"73000000-0000-4000-8000-000000000001",
  "lease_version":1,
  "analysis_id":"30000000-0000-4000-8000-000000000001",
  "expires_at":"2026-08-05T08:01:30Z",
  "allowed_uploads":["startup_trace","scroll_trace","memory_evidence","agent_log"]
}
```

The snapshot lifetime is at most 90 seconds. Store its SHA-256 digest with the lease; never store the compact JWS.

- [ ] **Step 5: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_task_snapshots.py services/api/tests/unit/test_agent_task_service.py services/api/tests/unit/test_analysis_service.py services/api/tests/integration/test_analysis_api.py services/api/tests/integration/test_agent_task_api.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add contracts/v1 services/api/src/perfpilot_api services/api/tests
git commit -m "feat: lease signed device tasks"
```

## Task 5: Add multipart capture upload, completion, and cancellation

**Files:**
- Modify: `services/api/src/perfpilot_api/storage/base.py`
- Modify: `services/api/src/perfpilot_api/storage/s3.py`
- Create: `services/api/src/perfpilot_api/services/agent_uploads.py`
- Modify: `services/api/src/perfpilot_api/services/agent_tasks.py`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/api/agent_control.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/workers/trace_orchestrator.py`
- Create: `services/api/src/perfpilot_api/workers/reconciler.py`
- Create: `services/api/src/perfpilot_api/workers/dispatcher.py`
- Create: `services/api/tests/unit/test_agent_upload_service.py`
- Modify: `services/api/tests/unit/test_analysis_service.py`
- Create: `services/api/tests/integration/test_agent_completion.py`
- Create: `services/api/tests/integration/test_agent_cancellation.py`
- Modify: `services/api/tests/integration/test_trace_orchestrator.py`

- [ ] **Step 1: Write failing multipart tests**

```python
async def test_512_mib_trace_uses_64_mib_parts(upload_service) -> None:
    slot = await upload_service.reserve(
        execution_id=EXECUTION_ID,
        kind="startup_trace",
        size=512 * 1024 * 1024,
        sha256_b64=CHECKSUM,
        mime="application/x-perfetto-trace",
    )
    assert slot.part_size_bytes == 64 * 1024 * 1024
    assert slot.part_count == 8


async def test_completion_is_exactly_once(upload_service, object_store) -> None:
    first = await upload_service.complete(UPLOAD_ID, valid_parts())
    second = await upload_service.complete(UPLOAD_ID, valid_parts())
    assert second.artifact_id == first.artifact_id
    assert object_store.complete_calls == 1
```

Reject output larger than 512 MiB, part count above 10,000, non-contiguous part numbers, wrong ETags, wrong checksum, wrong execution, an expired lease, or an upload kind absent from the signed task.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_agent_upload_service.py services/api/tests/integration/test_agent_completion.py services/api/tests/integration/test_agent_cancellation.py -q
```

- [ ] **Step 3: Extend the storage boundary**

Add `create_multipart`, `authorize_part`, `complete_multipart`, and `abort_multipart` to a new `MultipartArtifactStore` protocol. `S3ArtifactStore` delegates blocking boto3 calls through `asyncio.to_thread`. Each part authorization expires in 15 minutes and is scoped to the stored bucket, key, storage upload ID, and part number. The final `head` check must match size, content type, and canonical SHA-256 before the artifact becomes finalized.

- [ ] **Step 4: Complete an execution into the existing analysis pipeline**

`AgentTaskService.complete()` validates the closed execution manifest, current lease token/version, all finalized artifact IDs, and one completion per execution. In a single control transaction it releases the lease, marks the device ready, moves scenario jobs to `analyzing`, and creates deterministic outbox events. Extend `TraceOrchestrator` to consume device scenario trace artifacts through the current SmartPerfetto canonical-result path; memory evidence uses the existing Android Memory adapter. The current report writer and synthesis worker remain the only publishers of the final report.

- [ ] **Step 5: Coordinate browser cancellation**

Add the currently missing `POST /v1/teams/{team_id}/analyses/{analysis_id}/cancel` route and `AnalysisService.request_cancel()`. It atomically sets `GlobalJob.cancel_requested_at`, transitions an active lease to `cancel_requested`, cancels unclaimed downstream work, and wakes the Agent. Poll and renew responses return `action: "cancel"`. `cancel-ack` releases the device, aborts pending multipart uploads, records only a stable diagnostic code, and is idempotent.

`Reconciler.run_once()` expires leases after server time, marks their device ready only when no newer lease exists, aborts orphan multipart uploads, and moves interrupted jobs to the documented retryable state. `Dispatcher.run_once()` publishes outbox events idempotently; both scripts already named in `services/api/pyproject.toml` must become importable.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_agent_upload_service.py services/api/tests/integration/test_agent_completion.py services/api/tests/integration/test_agent_cancellation.py services/api/tests/integration/test_trace_orchestrator.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: coordinate Agent uploads and cancellation"
```

## Task 6: Enable Agent management and device analysis in the desktop web UI

**Files:**
- Create: `app/components/perfpilot-session-provider.tsx`
- Create: `app/components/agent-management.tsx`
- Create: `app/components/device-analysis-form.tsx`
- Create: `app/agents/page.tsx`
- Modify: `app/layout.tsx`
- Modify: `app/components/app-shell.tsx`
- Modify: `app/components/connected-device.tsx`
- Modify: `app/components/new-analysis-dialog.tsx`
- Modify: `app/components/dashboard.tsx`
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/globals.css`
- Modify: `tests/perfpilot-api.test.ts`
- Modify: `tests/app-shell-device.test.tsx`
- Modify: `tests/new-analysis-dialog.test.tsx`
- Create: `tests/agent-management.test.tsx`
- Create: `tests/device-analysis-form.test.tsx`
- Modify: `tests/dashboard-analysis-coordinator.test.tsx`

- [ ] **Step 1: Write failing client and component tests**

```tsx
it("shows real remote devices and never renders a demo Pixel", async () => {
  render(<ConnectedDevice />);
  expect(await screen.findByText("UNISOC ums9620")).toBeInTheDocument();
  expect(screen.queryByText(/Pixel 8/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/R3CN30SECRET/)).not.toBeInTheDocument();
});

it("submits the selected device id with a device analysis", async () => {
  await user.click(screen.getByRole("button", { name: "真机自动测试" }));
  await user.upload(screen.getByLabelText("APK 文件"), apkFile);
  await user.click(screen.getByRole("button", { name: "开始真机分析" }));
  expect(createDeviceAnalysis).toHaveBeenCalledWith(
    expect.objectContaining({ deviceId: DEVICE_ID })
  );
});
```

- [ ] **Step 2: Run RED**

```bash
npm run test:unit -- tests/perfpilot-api.test.ts tests/app-shell-device.test.tsx tests/new-analysis-dialog.test.tsx tests/agent-management.test.tsx tests/device-analysis-form.test.tsx
```

- [ ] **Step 3: Add shared team/device state**

Wrap the desktop application in `PerfPilotSessionProvider`. It creates one client, initializes CSRF, resolves the first membership, polls `/devices` every ten seconds, and stores the selected `device_id` per team. It clears selection when the device disappears or leaves `ready`. It never stores an Agent token or raw serial.

`ConnectedDevice` becomes a compact selector. Required states are “正在读取设备”, “尚未连接设备”, “等待 USB 调试授权”, “设备离线”, “正在执行任务”, and a ready device name. Multiple devices are selectable; do not tell the user to disconnect all but one.

- [ ] **Step 4: Add Agent management and registration code UI**

Add a desktop navigation item “设备 Agent”. The page lists Agent name, platform, version, state, last heartbeat, rename, revoke, and “生成注册码”. Show the plaintext code once with its exact expiration and a copy button; never persist it to local storage or render it after page reload.

- [ ] **Step 5: Enable true device analysis**

Replace the disabled “待接入” card with a selectable mode. `DeviceAnalysisForm` requires one ready device and one APK, hashes the APK, creates a `device` analysis with `device_id` and all three ordered scenarios, uploads/finalizes the APK through the returned authorization, closes the dialog, and hands the background job to the existing dashboard task card. Trace upload remains unchanged.

Widen `AnalysisResponse.analysis_mode` to `"device" | "trace_upload" | "memory_upload"`; validate each discriminated response instead of casting. Keep the final report entry and report pages unchanged so the existing SmartPerfetto/AI report appears in the same location.

- [ ] **Step 6: Run GREEN and commit**

```bash
npm run test:unit
npm run test:ssr
npm run lint
git add app tests
git commit -m "feat: enable remote device analyses in web"
```

## Plan 1 closure gate

Run:

```bash
.venv/bin/ruff check services/api/src services/api/tests
.venv/bin/pytest -p no:cacheprovider services/api/tests -q
npm run lint
npm run test:unit
npm run test:ssr
```

Expected: all commands exit `0`; the local Trace upload tests remain green; no response fixture, snapshot, or rendered HTML contains a raw serial, token, digest, object key, storage upload ID, or signed URL outside its upload authorization contract.
