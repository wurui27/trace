# PerfPilot Phase 2 Local Standalone Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the accepted PerfPilot API, Web, device execution, analysis, and report flow on one developer machine with one command, one local workspace, no cloud infrastructure, and durable restart recovery.

**Architecture:** Phase 2 preserves `contracts/v1`, FastAPI routes, task states, Agent snapshots, `AnalysisReport v1`, its scenario-level `AnalysisBundle v1`, and the tracekit analysis path. Dependency injection replaces PostgreSQL control/team stores with one SQLite WAL database, S3 with an immutable workspace file store, and Redis Streams with bounded in-process queues backed by the same durable SQLite outbox/inbox tables. API, scheduler, dispatcher, validator, and analysis consumers run together in one local core process so those queues never cross a process boundary; the existing Agent remains a separate loopback HTTP client. One workspace is exactly one tenant. A supervisor starts the core, Agent, and Web only on `127.0.0.1`, reports component readiness, and recovers unfinished work after a clean or abrupt restart.

**Tech Stack:** Python 3.12, uv workspace, FastAPI, SQLAlchemy async, SQLite WAL, aiosqlite, local immutable files, asyncio queues, existing device Agent and trace Worker packages, React/Vinext, pytest, Vitest, Playwright.

---

## File map

- Create `local-runtime/pyproject.toml`: standalone package and CLI.
- Create `local-runtime/src/perfpilot_local/config.py`: validated loopback and workspace configuration.
- Create `local-runtime/src/perfpilot_local/sqlite_store.py`: local control/team persistence and migrations.
- Create `local-runtime/src/perfpilot_local/file_store.py`: immutable artifact slots and signed local URLs.
- Create `local-runtime/src/perfpilot_local/queue.py`: durable outbox to in-process consumer groups.
- Create `local-runtime/src/perfpilot_local/runtime.py`: dependency composition for API, Worker, and Agent.
- Create `local-runtime/src/perfpilot_local/supervisor.py`: child lifecycle, readiness, shutdown, and restart.
- Create `local-runtime/src/perfpilot_local/cli.py`: `perfpilot local init|up|status`.
- Create `local-runtime/tests/`: adapter contracts, recovery, security, browser, and real-device acceptance.
- Modify root `pyproject.toml`, `uv.lock`, `infra/`, Web runtime configuration, and CI.

## Task 1: Scaffold the local package and validated workspace configuration

**Files:**
- Modify: `pyproject.toml`
- Create: `local-runtime/pyproject.toml`
- Create: `local-runtime/src/perfpilot_local/__init__.py`
- Create: `local-runtime/src/perfpilot_local/config.py`
- Create: `local-runtime/src/perfpilot_local/cli.py`
- Create: `local-runtime/tests/conftest.py`
- Create: `local-runtime/tests/factories.py`
- Create: `local-runtime/tests/unit/test_config.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add the local package to the uv workspace**

Append `local-runtime` to `[tool.uv.workspace].members` and `local-runtime/tests` to the root pytest `testpaths`, preserving all Phase 1 entries. Its package depends on the API, Agent, Worker, and tracekit through workspace sources:

```toml
[project]
name = "perfpilot-local"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "aiosqlite>=0.22,<0.23",
  "perfpilot-api",
  "perfpilot-device-agent",
  "perfpilot-trace-worker",
  "perfpilot-tracekit",
]

[project.scripts]
perfpilot = "perfpilot_local.cli:main"

[tool.uv.sources]
perfpilot-api = { workspace = true }
perfpilot-device-agent = { workspace = true }
perfpilot-trace-worker = { workspace = true }
perfpilot-tracekit = { workspace = true }
```

- [ ] **Step 2: Define shared test fixtures before using them**

`local-runtime/tests/factories.py` defines fixed `TEAM_ID`, `EVENT_ID`, task/claim IDs, deterministic `payload()`, and immutable artifact metadata builders. Every test module explicitly imports the values it uses.

`local-runtime/tests/conftest.py` provides:

- `workspace_path`: a resolved empty temporary directory with mode `0700`;
- `local_settings`: `LocalSettings` using loopback ports chosen by bound ephemeral sockets;
- `sqlite_inspector`: read-only helpers for schema, rows, WAL mode, and foreign keys;
- `local_runtime`: async context manager that starts composed services with fake ADB and fake trace processor;
- `crash_runtime`: terminates the dispatcher or Worker without graceful cleanup;
- `contract_examples`: loads only `contracts/v1/examples`;
- `artifact_bytes`: deterministic non-customer bytes and SHA-256 metadata.

Use lazy imports inside fixtures so Task 1 configuration tests collect before the SQLite, file, queue, runtime, and supervisor modules exist.

- [ ] **Step 3: Write failing configuration tests**

```python
def test_default_bindings_are_loopback(workspace_path: Path) -> None:
    settings = LocalSettings(workspace=workspace_path)
    assert settings.api_host == "127.0.0.1"
    assert settings.web_host == "127.0.0.1"
    assert settings.api_url.host == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10"])
def test_non_loopback_requires_explicit_dangerous_flag(
    workspace_path: Path, host: str
) -> None:
    with pytest.raises(ValueError, match="loopback"):
        LocalSettings(workspace=workspace_path, api_host=host)


def test_workspace_cannot_be_home_or_filesystem_root(tmp_path: Path) -> None:
    for unsafe in (Path.home(), Path("/")):
        with pytest.raises(ValueError, match="workspace"):
            LocalSettings(workspace=unsafe)
```

- [ ] **Step 4: Run RED**

```bash
uv lock
uv sync --locked --all-packages --all-extras --dev
uv run --package perfpilot-local pytest local-runtime/tests/unit/test_config.py -q
```

Expected: FAIL because `perfpilot_local.config` does not exist.

- [ ] **Step 5: Implement configuration and non-destructive initialization**

Resolve and validate every workspace path before creating it. The workspace layout is:

```text
workspace/
├── config.json
├── state/perfpilot.sqlite3
├── artifacts/objects/
├── artifacts/staging/
├── run/
└── logs/
```

`perfpilot local init --workspace PATH` creates missing directories with owner-only permissions, creates a random local proxy secret, stores it in an owner-readable configuration file, and refuses a non-empty directory unless it already contains a compatible PerfPilot workspace marker. It never deletes or overwrites unknown files. Administrator credentials come only from an environment variable at first initialization and are stored as a password hash; neither the value nor hash is printed.

- [ ] **Step 6: Run GREEN and commit**

```bash
uv lock
uv run --package perfpilot-local pytest local-runtime/tests/unit/test_config.py -q
git add pyproject.toml uv.lock local-runtime/pyproject.toml local-runtime/src/perfpilot_local/__init__.py local-runtime/src/perfpilot_local/config.py local-runtime/src/perfpilot_local/cli.py local-runtime/tests/conftest.py local-runtime/tests/factories.py local-runtime/tests/unit/test_config.py
git commit -m "feat: scaffold PerfPilot local runtime"
```

## Task 2: Implement the SQLite control and tenant store

**Files:**
- Create: `local-runtime/src/perfpilot_local/sqlite_store.py`
- Create: `local-runtime/src/perfpilot_local/migrations/001_initial.sql`
- Create: `local-runtime/src/perfpilot_local/migrations/002_indexes.sql`
- Create: `local-runtime/tests/contract/test_sqlite_repository_contract.py`
- Create: `local-runtime/tests/integration/test_sqlite_migrations.py`

- [ ] **Step 1: Write failing repository contract tests**

Run the same API repository behavior suite against PostgreSQL and `SQLiteRepository`:

```python
@pytest.mark.parametrize("repository_name", ["postgres", "sqlite"])
async def test_idempotent_analysis_create(repository_name, repository_factory) -> None:
    repository = await repository_factory(repository_name)
    first = await repository.create_analysis(TEAM_ID, "idem-1", REQUEST_HASH, payload())
    second = await repository.create_analysis(TEAM_ID, "idem-1", REQUEST_HASH, payload())
    assert second.analysis_id == first.analysis_id


async def test_sqlite_compare_and_swap_rejects_stale_version(sqlite_repository) -> None:
    saved = await sqlite_repository.save_job(new_job(version=1))
    assert await sqlite_repository.transition(saved.id, 1, "queued")
    assert not await sqlite_repository.transition(saved.id, 1, "running")
```

Also reuse tests for sessions, CSRF, users, memberships, analysis state, attempts, claims, outbox/inbox, reports, audit, retention tombstones, and idempotency conflicts.

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_sqlite_repository_contract.py \
  local-runtime/tests/integration/test_sqlite_migrations.py -q
```

Expected: FAIL because the local repository and migrations are absent.

- [ ] **Step 3: Implement one-workspace persistence**

Use SQLAlchemy async with `sqlite+aiosqlite`. On every connection enable:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

Use one schema containing control and tenant content but preserve repository interfaces and `team_id` checks. Initialization creates exactly one team with a stable UUID stored in `config.json`; every repository entry point asserts that team ID. Do not expose a caller-selectable database path or tenant route.

Use explicit UTC ISO timestamps, UUID strings, integer nanoseconds, and JSON text validated through the same Pydantic models. Every outbox write and authoritative state transition occurs in one SQLite transaction. Use partial/unique indexes for idempotency keys, active claims, active leases, event inbox keys, artifact versions, and report IDs.

- [ ] **Step 4: Implement forward-only migrations and backup-safe startup**

Acquire an owner-only workspace lock before migrations. Before a schema-changing migration, create a SQLite online backup under `state/backups/` with a timestamp and schema version. Apply forward-only migrations in one transaction where SQLite permits; on failure, keep the old database and refuse readiness. Never silently downgrade or recreate a database.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_sqlite_repository_contract.py \
  local-runtime/tests/integration/test_sqlite_migrations.py -q
git add local-runtime/src/perfpilot_local/sqlite_store.py local-runtime/src/perfpilot_local/migrations local-runtime/tests/contract/test_sqlite_repository_contract.py local-runtime/tests/integration/test_sqlite_migrations.py
git commit -m "feat: add durable local SQLite state"
```

## Task 3: Implement immutable local artifact storage

**Files:**
- Create: `local-runtime/src/perfpilot_local/file_store.py`
- Create: `local-runtime/src/perfpilot_local/api/artifacts.py`
- Create: `local-runtime/tests/contract/test_file_store_contract.py`
- Create: `local-runtime/tests/integration/test_local_uploads.py`

- [ ] **Step 1: Write failing storage contract tests**

```python
async def test_finalize_moves_bytes_to_content_addressed_read_only_object(
    file_store, artifact_bytes
) -> None:
    slot = await file_store.create_slot(expected=artifact_bytes.metadata)
    await file_store.test_put(slot.token, artifact_bytes.value)
    artifact = await file_store.finalize(slot.upload_id, artifact_bytes.metadata)
    assert artifact.version_id == artifact_bytes.sha256_hex
    assert artifact.path.stat().st_mode & 0o222 == 0


async def test_changed_or_replayed_upload_is_rejected(file_store, artifact_bytes) -> None:
    slot = await file_store.create_slot(expected=artifact_bytes.metadata)
    await file_store.test_put(slot.token, b"changed")
    with pytest.raises(UploadMismatchError):
        await file_store.finalize(slot.upload_id, artifact_bytes.metadata)
```

Also test expired upload tokens, path traversal, symlinks, oversize streams, cross-analysis finalize, overwrite attempts, tombstoned downloads, and interrupted staging files.

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_file_store_contract.py \
  local-runtime/tests/integration/test_local_uploads.py -q
```

Expected: FAIL because `LocalFileArtifactStore` does not exist.

- [ ] **Step 3: Implement the S3-compatible application boundary**

Keep the application-level `UploadSlot`, finalize request, artifact metadata, and download response contracts unchanged. In local mode, slot URLs point to an unguessable loopback route and expire after 15 minutes. Stream bytes to a random staging filename while enforcing the declared byte limit and SHA-256; `fsync` the file, atomically move it to `artifacts/objects/{sha256-prefix}/{sha256}`, `fsync` the containing directory, then set read-only permissions.

Never derive a path from customer filename, package, team, task, or request path. Save those values only as validated metadata. If identical bytes already exist, reuse the content object but create a distinct immutable artifact/version record. Downloads require the normal authenticated membership API; a file path is never returned to the browser.

The tokenized PUT/HEAD route has a narrow CORS policy for the one configured local Web origin, signed content-type/checksum headers, and no credentials. It accepts only loopback requests and never enables wildcard origin. All other business API requests still traverse the Web’s same-origin signed `/api` proxy.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_file_store_contract.py \
  local-runtime/tests/integration/test_local_uploads.py -q
git add local-runtime/src/perfpilot_local/file_store.py local-runtime/src/perfpilot_local/api/artifacts.py local-runtime/tests/contract/test_file_store_contract.py local-runtime/tests/integration/test_local_uploads.py
git commit -m "feat: add immutable local artifact store"
```

## Task 4: Replace Redis delivery with a restart-safe in-process dispatcher

**Files:**
- Create: `local-runtime/src/perfpilot_local/queue.py`
- Create: `local-runtime/src/perfpilot_local/consumers.py`
- Create: `local-runtime/tests/contract/test_local_delivery_contract.py`
- Create: `local-runtime/tests/integration/test_local_queue_recovery.py`

- [ ] **Step 1: Write failing delivery and crash tests**

```python
async def test_event_is_not_complete_until_state_and_inbox_commit(
    local_dispatcher, sqlite_inspector
) -> None:
    await local_dispatcher.publish(EVENT_ID)
    await local_dispatcher.crash_after_handler_before_commit(EVENT_ID)
    assert sqlite_inspector.inbox_state(EVENT_ID) == "received"
    await local_dispatcher.restart()
    await local_dispatcher.wait_processed(EVENT_ID)
    assert sqlite_inspector.effect_count(EVENT_ID) == 1


async def test_expired_claim_is_recovered_after_process_restart(
    crash_runtime, local_runtime, sqlite_inspector
) -> None:
    analysis_id = await crash_runtime.start_analysis()
    await crash_runtime.stop_after_claim()
    async with local_runtime() as restarted:
        await restarted.clock.advance_beyond_claim()
        await restarted.wait_terminal(analysis_id)
    assert sqlite_inspector.report_count(analysis_id) == 1
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_local_delivery_contract.py \
  local-runtime/tests/integration/test_local_queue_recovery.py -q
```

Expected: FAIL because the local dispatcher and consumers are absent.

- [ ] **Step 3: Implement durable delivery**

SQLite outbox rows remain authoritative. One publisher claims unpublished rows with a compare-and-swap lease, commits, and places only the event ID into one of three bounded `asyncio.Queue` instances: schedule, sample validation, or analysis. Each consumer reloads the envelope from SQLite and uses the same inbox/claim/state transaction as the cloud consumer. Mark `published_at` only after queue insertion succeeds.

On startup and every 30 seconds, reconcile:

- unpublished or expired-publish outbox rows;
- `received` inbox rows with expired claims;
- `validating`, `queued`, or `analyzing` work without a live claim;
- completed authoritative state whose event still appears pending.

Queue capacity applies backpressure and never drops events. Unknown event types enter the same dead-letter model. Graceful shutdown stops intake, waits for the current transaction, and leaves remaining rows in the outbox.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_local_delivery_contract.py \
  local-runtime/tests/integration/test_local_queue_recovery.py -q
git add local-runtime/src/perfpilot_local/queue.py local-runtime/src/perfpilot_local/consumers.py local-runtime/tests/contract/test_local_delivery_contract.py local-runtime/tests/integration/test_local_queue_recovery.py
git commit -m "feat: add restart-safe local event delivery"
```

## Task 5: Compose the existing API, Worker, and Agent with local adapters

**Files:**
- Create: `local-runtime/src/perfpilot_local/runtime.py`
- Create: `local-runtime/src/perfpilot_local/dependencies.py`
- Create: `local-runtime/src/perfpilot_local/agent_registration.py`
- Create: `local-runtime/tests/contract/test_api_contract_compatibility.py`
- Create: `local-runtime/tests/integration/test_local_pipeline.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/trace-worker/src/perfpilot_worker/main.py`
- Modify: `agents/device-agent/src/perfpilot_agent/main.py`

- [ ] **Step 1: Write failing compatibility tests**

```python
@pytest.mark.parametrize("runtime_name", ["cloud", "local"])
async def test_same_contract_examples_validate(runtime_name, runtime_factory, contract_examples) -> None:
    runtime = await runtime_factory(runtime_name)
    for exchange in contract_examples:
        response = await runtime.execute(exchange.request)
        assert response.json() == exchange.expected_response


async def test_local_runtime_has_exactly_one_tenant(local_runtime) -> None:
    async with local_runtime() as runtime:
        me = await runtime.client.get("/v1/me")
        assert len(me.json()["teams"]) == 1
        assert runtime.dependencies.tenant_router.database_count == 1
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_api_contract_compatibility.py \
  local-runtime/tests/integration/test_local_pipeline.py -q
```

Expected: FAIL because the production service factories do not accept local adapters.

- [ ] **Step 3: Extract dependency factories without branching domain behavior**

API, Worker, and Agent entry points accept typed dependency bundles. Their default factories continue to construct PostgreSQL, S3, Redis, remote control clients, and normal Agent HTTP clients. `perfpilot_local.runtime` supplies SQLite, local files, local dispatcher, and loopback HTTP clients. Do not add `if local` branches to task state machines, contract models, trace adapters, sample rules, findings, or report rendering.

Create the one local tenant and its active resource mapping on first initialization. Generate and store one local Agent identity through the normal registration service. The Agent still receives signed snapshots, uses a lease token, executes serial-bound ADB commands, uploads through authorized slots, and reads authoritative validator verdicts.

Inject a local cookie policy that omits `Secure` only for the product session served from the configured loopback HTTP origin. Keep `HttpOnly`, `SameSite=Lax`, host-only scope, CSRF, and Origin checks. The settings factory must refuse that policy unless both API and Web listeners resolve to loopback; cloud/production factories always require `Secure`.

- [ ] **Step 4: Prove the fake-device full flow**

With fake ADB and fake trace processor, create one device analysis through `/v1`, direct-upload an APK fixture, finalize it, execute all three scenarios, validate samples, analyze artifacts, and read the same report response. Restart the runtime once between upload and schedule and once after Worker report write but before claim completion; the task must finish exactly once.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/contract/test_api_contract_compatibility.py \
  local-runtime/tests/integration/test_local_pipeline.py -q
uv run --package perfpilot-api pytest services/api/tests -q
uv run --package perfpilot-trace-worker pytest services/trace-worker/tests -q
uv run --package perfpilot-device-agent pytest agents/device-agent/tests -q
git add local-runtime/src/perfpilot_local/runtime.py local-runtime/src/perfpilot_local/dependencies.py local-runtime/src/perfpilot_local/agent_registration.py local-runtime/tests/contract/test_api_contract_compatibility.py local-runtime/tests/integration/test_local_pipeline.py services/api/src/perfpilot_api/main.py services/trace-worker/src/perfpilot_worker/main.py agents/device-agent/src/perfpilot_agent/main.py
git commit -m "feat: compose cloud services for local mode"
```

## Task 6: Add one-command loopback supervision and Web startup

**Files:**
- Create: `local-runtime/src/perfpilot_local/supervisor.py`
- Create: `local-runtime/src/perfpilot_local/processes.py`
- Create: `local-runtime/src/perfpilot_local/readiness.py`
- Create: `local-runtime/tests/integration/test_supervisor.py`
- Create: `local-runtime/tests/integration/test_loopback_security.py`
- Create: `tests/e2e/local-runtime.spec.ts`
- Modify: `app/lib/api/runtime.ts`
- Modify: `worker/api-proxy.ts`
- Modify: `package.json`

- [ ] **Step 1: Write failing process and network tests**

```python
async def test_local_up_reaches_ready_and_shuts_down_children(supervisor, local_settings) -> None:
    async with supervisor(local_settings) as running:
        assert await running.health() == {
            "api": "ready",
            "dispatcher": "ready",
            "worker": "ready",
            "agent": "ready",
            "web": "ready",
        }
    assert running.live_child_pids() == []


async def test_every_listening_socket_is_loopback(supervisor, local_settings) -> None:
    async with supervisor(local_settings) as running:
        assert set(await running.listening_addresses()) <= {"127.0.0.1", "::1"}
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/integration/test_supervisor.py \
  local-runtime/tests/integration/test_loopback_security.py -q
```

Expected: FAIL because the supervisor has not been implemented.

- [ ] **Step 3: Implement `perfpilot local up`**

Acquire `run/perfpilot.lock` so one workspace cannot start twice. Resolve free ports before launching children and pass exact ports without shell interpolation. Start the local core (API, scheduler, dispatcher, validator, and analysis consumers in one event loop), Agent, and Web in dependency order. Component readiness may be reported separately, but the in-process queues must never be shared between OS processes. A service becomes ready only after its own readiness endpoint plus dependency checks pass. Write a non-secret `run/status.json` atomically with PID, port, start time, and state.

Prefix redacted child logs by service and rotate them under `logs/`. On `SIGINT` or `SIGTERM`, stop Web intake, API intake, Agent, dispatcher, Worker, then release the lock. If a child exits unexpectedly, mark the runtime unhealthy, stop accepting new analyses, allow bounded automatic restarts, and preserve SQLite/outbox state. Never use broad process-name killing.

The Web Worker receives loopback `PERFPILOT_API_ORIGIN` and the workspace proxy secret. Browser code still calls `/api/v1/...`; it does not contain a separate direct-origin mode.

- [ ] **Step 4: Add local browser recovery coverage**

Playwright launches `perfpilot local up`, logs in through the Web, submits the fake-device analysis, terminates the runtime after the task becomes running, restarts the same workspace, refreshes the route, and waits for the authoritative terminal report. Assert that browser storage contains no database path, artifact path, proxy secret, session secret, or Agent token.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-local pytest \
  local-runtime/tests/integration/test_supervisor.py \
  local-runtime/tests/integration/test_loopback_security.py -q
npm run test:e2e -- tests/e2e/local-runtime.spec.ts
git add local-runtime/src/perfpilot_local/supervisor.py local-runtime/src/perfpilot_local/processes.py local-runtime/src/perfpilot_local/readiness.py local-runtime/tests/integration/test_supervisor.py local-runtime/tests/integration/test_loopback_security.py tests/e2e/local-runtime.spec.ts app/lib/api/runtime.ts worker/api-proxy.ts package.json
git commit -m "feat: add one-command local runtime"
```

## Task 7: Pass local RKGallery acceptance, restart recovery, and push

**Files:**
- Create: `local-runtime/tests/acceptance/test_rkgallery_local.py`
- Create: `local-runtime/tests/acceptance/test_restart_recovery.py`
- Create: `docs/acceptance/rkgallery/local-runtime.md`
- Modify: `.github/workflows/platform-ci.yml`

- [ ] **Step 1: Add the local real-device gate**

Reuse the checked-in RKGallery fixture and every quality assertion from Phase 1. The local run must use the same physical device for all scenarios and prove 5 valid process-cold launches in at most 10 attempts, 5 valid 30-second scroll runs in at most 10 attempts, 10 memory rounds under one PID, per-attempt thermal gates, complete manifests, immutable artifacts, provenance, and device cleanup. Compare the local `AnalysisReport` and each scenario `AnalysisBundle` with the cloud contract shapes while allowing run IDs, timestamps, environment fingerprints, and measured values to differ.

- [ ] **Step 2: Add abrupt restart recovery**

Run a second analysis and terminate the entire supervisor without graceful shutdown at each boundary in separate parametrized cases:

```text
after upload finalize
after schedule outbox commit
after Agent sample finalize
after validator verdict commit
after analysis claim
after report write before claim completion
```

Restart the same workspace. Each analysis reaches the correct terminal or partial terminal state, each side effect occurs once, and no immutable object is overwritten.

- [ ] **Step 3: Run the complete Phase 2 gate**

```bash
export PERFPILOT_TEST_TRACE_DIR=/Users/ray/Desktop/trace_learn/test-traces
uv sync --locked --all-packages --all-extras --dev
uv run ruff check services tracekit agents local-runtime
uv run pytest -q
npm ci
npm run lint
npm test
npm run test:e2e
uv run --package perfpilot-local pytest local-runtime/tests/acceptance -q
git diff --check
```

Expected: all commands exit `0`; real-device inputs and raw output remain ignored; only the redacted acceptance record and intended source changes are present.

- [ ] **Step 4: Commit the acceptance gate**

```bash
git add local-runtime/tests/acceptance docs/acceptance/rkgallery/local-runtime.md .github/workflows/platform-ci.yml
git commit -m "test: verify standalone local runtime"
```

- [ ] **Step 5: Fast-forward push and verify**

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
git status --short
```

Expected: the push is fast-forward, local and remote SHAs match, and the worktree is clean.

- [ ] **Step 6: Roll out the exact pushed cloud-compatible backend**

On the private acceptance host, fetch and verify `local_sha`, build the API/Worker images from that detached source, run forward migrations, and replace services only after readiness succeeds. Run the cloud repository, queue, storage, Agent, and report smoke suites to prove dependency injection still selects PostgreSQL, Redis, S3, and remote Agent adapters. On failure, restore the prior image digests and leave the local-only workspace untouched.

- [ ] **Step 7: Save and deploy the exact shared Web source**

Because this packet modifies shared Web/proxy source, read `.openai/hosting.json`, reuse its existing opaque project ID, and confirm `d1` and `r2` remain null. Upload the exact tree at `local_sha`, save a Sites version with `commit_sha=local_sha`, deploy only that saved version with private access, and poll to a terminal status. Run the Phase 1 production smoke checks to prove the cloud API path is unchanged; do not expose the loopback-only local runtime.
