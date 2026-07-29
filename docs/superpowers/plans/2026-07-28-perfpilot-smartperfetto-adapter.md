# PerfPilot SmartPerfetto Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Apply superpowers:test-driven-development to every source-changing task and superpowers:verification-before-completion before each commit.

**Goal:** Add a production-safe SmartPerfetto HTTP/SSE Adapter that provisions one server-owned workspace per PerfPilot team, uploads a verified Trace, starts analysis, recovers streams after disconnect or upstream restart, and exposes only stable platform states plus a tenant-artifact result boundary.

**Architecture:** SmartPerfetto remains an independently deployed network service locked to `v1.0.38` / `1508f99788bfcf18cc861e4bf4f8b472e84240c3`. PerfPilot owns a checked-in `workspace-agent-v1` consumer contract because the upstream does not advertise or negotiate that contract name. A typed async client validates selected upstream fields, strips unsafe fields, maps upstream states and errors, and never stores raw payloads in the control database. A control-plane service owns team-to-workspace mapping and execution compare-and-swap updates; raw reports cross an injected tenant-artifact sink boundary before the control row receives only an opaque artifact UUID.

**Tech Stack:** Python 3.12, httpx, Pydantic 2, SQLAlchemy 2 async, PostgreSQL, pytest, `httpx.MockTransport`, JSON/SSE fixtures, uv.

---

## Approved boundary and upstream truth

This packet consumes the exact SmartPerfetto source pinned in `infra/engines/engine-lock.yaml`. The reviewed upstream routes are:

| Operation | Method and path | Durable platform data |
| --- | --- | --- |
| List workspaces | `GET /api/tenant/workspaces` | none; reconciliation only |
| Create workspace | `POST /api/tenant/workspaces` | opaque workspace ID |
| Upload Trace | `POST /api/workspaces/{workspaceId}/traces/upload` | transient Trace ID only |
| Start analysis | `POST /api/workspaces/{workspaceId}/agent/analyze` | opaque session and run IDs |
| Resume after restart | `POST /api/workspaces/{workspaceId}/agent/resume` | refreshed run ID when supplied |
| Stream run | `GET /api/workspaces/{workspaceId}/agent/runs/{runId}/stream` | last decimal event cursor |
| Read status | `GET /api/workspaces/{workspaceId}/agent/{sessionId}/status` | mapped state/error only |
| Cancel | `POST /api/workspaces/{workspaceId}/agent/{sessionId}/cancel` | mapped terminal state |
| Fetch report | `GET /api/workspaces/{workspaceId}/agent/{sessionId}/report` | tenant raw-result artifact only |

The following facts are contract requirements, not implementation suggestions:

- SmartPerfetto has no named `workspace-agent-v1` manifest, handshake, or version endpoint. Never claim it does. The name identifies a PerfPilot-owned consumer contract frozen against the pinned commit.
- SmartPerfetto spells cancellation `cancelled`; PerfPilot stores `canceled`.
- SmartPerfetto can terminate an attempt with `quota_exceeded`; PerfPilot maps that attempt to retryable `capacity_exceeded` and keeps the parent analysis nonterminal while its bounded retry budget remains.
- PerfPilot profiles `startup` and `scroll` are fixed prompt/query templates. They are never sent as SmartPerfetto `analysisMode`; upstream accepts only `fast`, `full`, or `auto`.
- SSE cursors are non-negative decimal integers. PerfPilot's string cursor field stores the canonical decimal representation.
- After an upstream restart, status, stream, cancel, and report may return `404` until the session is resumed. Resume requires `sessionId`; `traceId` is optional and is only a mismatch guard. The upstream persisted session restores its own Trace ID.
- Because resume does not require a client-held Trace ID, this packet does **not** add `external_trace_id` to `EngineExecution`. The design phrase “save trace ID” is narrowed to the in-memory upload-to-analyze transaction. If a process dies between those calls, a later attempt may upload again; upstream retention owns the orphaned Trace.
- SmartPerfetto has no workspace-delete endpoint in this pin. This packet never invents one.
- Upload can return HTTP `200` with `success: false`; status code alone is never accepted as success.
- PerfPilot downloads its own short-lived artifact authorization and multipart-uploads verified bytes. It never calls SmartPerfetto's external `upload-url` surface and never forwards a signed URL.

## Delivery boundaries

This packet includes:

1. frozen consumer fixtures and validators tied to the exact upstream commit;
2. a secret-safe HTTP transport and incremental SSE parser;
3. idempotent, server-owned team workspace provisioning;
4. bounded Trace materialization, multipart upload, profile/query mapping, and analyze submission;
5. replay, restart recovery, status, cancel, report sanitization, and stable error mapping;
6. control-plane execution compare-and-swap updates and a tenant-artifact result-sink protocol;
7. focused, contract, real-PostgreSQL, and full backend gates.

This packet does **not** add a public Trace-upload Web/API path, Memory Adapter, Report Normalizer, AI summarizer, provider manager, user AI keys, SmartPerfetto UI embedding, external URL upload, MCP, code execution, RAG, self-learning, workspace deletion, deployment images, or upstream upgrade automation. The result-sink implementation and report normalization remain in the later report packet; this packet proves the boundary and forbids raw results in control persistence.

## Locked file structure

```text
services/api/src/perfpilot_api/
├── config.py
├── engines/
│   ├── __init__.py
│   ├── contracts.py
│   ├── errors.py
│   ├── smartperfetto.py
│   ├── smartperfetto_contracts.py
│   ├── smartperfetto_transport.py
│   └── sse.py
└── services/
    ├── engine_executions.py
    └── engine_workspaces.py
services/api/tests/
├── fixtures/smartperfetto_workspace_agent_v1/
│   ├── README.md
│   ├── analyze-smart-deep-dive-request.json
│   ├── analyze-smart-preview-request.json
│   ├── analyze-success.json
│   ├── cancel-success.json
│   ├── concurrent-quota.json
│   ├── monthly-quota.json
│   ├── progress-stream.sse
│   ├── report-completed.json
│   ├── resume-success.json
│   ├── smart-preview-stream.sse
│   ├── status-completed.json
│   ├── trace-upload-success.json
│   ├── trace-upload-success-false.json
│   ├── workspace-create-request.json
│   ├── workspace-create-success.json
│   └── workspace-list-success.json
├── contract/test_smartperfetto_workspace_agent_v1.py
├── integration/test_engine_execution_repository.py
├── integration/test_engine_workspace_repository.py
├── unit/test_engine_execution_service.py
├── unit/test_engine_workspaces.py
├── unit/test_smartperfetto_adapter.py
├── unit/test_smartperfetto_sse.py
└── unit/test_smartperfetto_transport.py
```

Do not add a migration in this packet. `TeamEngineWorkspace` and `EngineExecution` from revision `0004_external_engine_foundation` already contain every durable field required by the reviewed contract.

## Stable mappings

### State mapping

| SmartPerfetto value | PerfPilot value | Notes |
| --- | --- | --- |
| `pending` | `pending` | submission accepted but not running |
| `running` | `running` | ordinary progress |
| `awaiting_user` | `awaiting_user`, then `failed` | service cancels the upstream session and records `engine_interaction_required`; this packet has no interactive continuation |
| `completed` | `completed` | only after a usable, sanitized report crosses the result-sink boundary |
| `cancelled` | `canceled` | spelling normalization |
| `failed` | `failed` | stable code selected from response class, never raw text |
| `quota_exceeded` | retryable capacity outcome | current attempt reports `capacity_exceeded`; the overall analysis retries within its wall-clock deadline |
| completed partial result with usable conclusion/evidence | `completed` | payload retains `partial: true` |
| completed result without usable conclusion or evidence | `insufficient_data` | never manufacture a finding |

### Error mapping

| Upstream condition | Stable code | Retryable |
| --- | --- | --- |
| analyze `429 CONCURRENT_RUN_QUOTA_EXCEEDED` | `capacity_exceeded` | yes, with bounded backoff outside Adapter |
| analyze `402 MONTHLY_RUN_QUOTA_EXCEEDED` | `engine_quota_exceeded` | no immediate retry |
| upload `413 TRACE_SIZE_QUOTA_EXCEEDED` | `engine_quota_exceeded` | no |
| upload `409 WORKSPACE_TRACE_STORAGE_QUOTA_EXCEEDED` | `engine_quota_exceeded` | no |
| upload/analyze `423 TENANT_TOMBSTONED` | `engine_tenant_unavailable` | no |
| analyze `409` lease unavailable | `capacity_exceeded` | yes |
| terminal status/SSE `quota_exceeded` | `capacity_exceeded` | yes, as a new attempt within the analysis deadline |
| Trace/session/report `404` after one resume attempt | `engine_session_lost` | no |
| malformed success body, invalid SSE, or incompatible result | `engine_contract_invalid` | no |
| authentication `401/403` | `engine_auth_failed` | no |
| timeout/connect/`5xx` | `engine_unavailable` or `engine_timeout` | yes when operation is safe to retry |
| local size/hash mismatch | `trace_integrity_failed` | no |
| upstream requests interactive input | `engine_interaction_required` | no |
| Smart preview has no startup/scroll family | `unsupported_trace_profile` | no |
| tenant result sink cannot persist before deadline | `result_persistence_failed` | no |

No exception message may include response bodies, queries, signed URLs, authorization headers, upstream absolute paths, bucket names, object keys, or raw report content.

## Task 1: Freeze the PerfPilot-owned SmartPerfetto consumer contract

**Files:**

- Modify: `services/api/pyproject.toml`
- Modify: `uv.lock`
- Create: `services/api/src/perfpilot_api/engines/smartperfetto_contracts.py`
- Create: `services/api/tests/fixtures/smartperfetto_workspace_agent_v1/README.md`
- Create: all JSON and SSE fixtures listed in the locked file structure
- Create: `services/api/tests/contract/test_smartperfetto_workspace_agent_v1.py`

- [ ] **Step 1: Add exact upstream-derived fixtures before parser code**

Copy the smallest representative response objects from the pinned upstream tests/routes into sanitized fixtures. Use only synthetic IDs and text. `README.md` must record:

```text
Upstream: Gracker/SmartPerfetto
Tag: v1.0.38
Commit: 1508f99788bfcf18cc861e4bf4f8b472e84240c3
Contract owner: PerfPilot
Contract name: workspace-agent-v1
Upstream handshake: none
```

For every fixture, list its reviewed upstream file and line range. Do not copy tokens, local paths, provider configuration, prompts, or real Trace data.

`workspace-create-request.json` freezes the exact consumer-owned POST body, including `workspaceId`, generic `name`, `quotaPolicy`, and `retentionPolicy`. Its deterministic `workspaceId` is the only identity used for list reconciliation; a similar name is never sufficient.

- [ ] **Step 2: Write RED contract tests**

Tests must prove:

- workspace list/create require `success: true` and usable IDs;
- upload rejects HTTP-success bodies with `success: false`;
- analyze requires non-empty `sessionId` and `runId`;
- Smart preview and deep-dive request fixtures serialize exactly with upstream-native `analysisMode: auto`, `preset: smart`, `smartAction`, and the allowlisted scene selection; neither request can contain platform profile spellings or unreviewed options;
- status accepts the seven reviewed upstream spellings and rejects unknown values;
- resume works without `traceId` and can return an observability run ID;
- cancel accepts only `cancelled` as the upstream terminal spelling;
- report drops `logFile`, validates a safe report identifier from relative `reportUrl`, and rejects arbitrary absolute URLs;
- Smart preview terminal SSE data requires `smartScenePreview.reportId` plus bounded scene objects with `id` and `sceneType`;
- retained nested values redact credential markers, signed URLs, object-store URIs, and absolute local paths, while forbidden operational keys such as `logFile`, `objectKey`, `bucket`, `authorization`, `apiKey`, and `token` are removed;
- unknown extra response fields are ignored for forward compatibility, while missing required consumer fields fail closed.

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-contract-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/contract/test_smartperfetto_workspace_agent_v1.py -q
```

Expected: FAIL because the consumer models do not exist.

- [ ] **Step 3: Add `httpx` as a direct production dependency**

Add `httpx>=0.28.1,<0.29` to `services/api/pyproject.toml` and update `uv.lock`. Do not rely on FastAPI's transitive dependency.

```bash
UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-lock \
  /Users/ray/Library/Python/3.12/bin/uv lock --offline
```

- [ ] **Step 4: Implement narrow Pydantic consumer models**

Use frozen models with `extra="ignore"`. Validate only fields PerfPilot consumes. Keep raw report content in a dedicated sanitized mapping, never in a control-plane model. Put endpoint error-code parsing in a typed response object rather than branching on human `error` text.

The model module must not make network calls and must not expose SmartPerfetto classes as PerfPilot public API.

- [ ] **Step 5: Run GREEN and regression**

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-contract-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/contract/test_smartperfetto_workspace_agent_v1.py \
  services/api/tests/unit/test_engine_lock.py \
  services/api/tests/unit/test_engine_contracts.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit and push Task 1**

```bash
git diff --check
git add services/api/pyproject.toml uv.lock \
  services/api/src/perfpilot_api/engines/smartperfetto_contracts.py \
  services/api/tests/fixtures/smartperfetto_workspace_agent_v1 \
  services/api/tests/contract/test_smartperfetto_workspace_agent_v1.py
git diff --cached --check
git commit -m "feat: freeze SmartPerfetto consumer contract"
git push origin HEAD:main
```

## Task 2: Add stable errors, secret-safe HTTP transport, and incremental SSE parsing

**Files:**

- Modify: `services/api/src/perfpilot_api/config.py`
- Modify: `services/api/src/perfpilot_api/engines/__init__.py`
- Create: `services/api/src/perfpilot_api/engines/errors.py`
- Create: `services/api/src/perfpilot_api/engines/smartperfetto_transport.py`
- Create: `services/api/src/perfpilot_api/engines/sse.py`
- Create: `services/api/tests/unit/test_smartperfetto_sse.py`
- Create: `services/api/tests/unit/test_smartperfetto_transport.py`
- Modify: `services/api/tests/unit/test_security.py`

- [ ] **Step 1: Write RED error/config/privacy tests**

Cover a frozen `EngineAdapterError` with `stable_code`, `retryable`, and terminal-state semantics. Its `str()` and `repr()` must be constant/redacted. Add settings tests for:

- an HTTPS SmartPerfetto base URL in production;
- rejection of credentials, fragments, query strings, loopback, and non-HTTPS production URLs;
- a `SecretStr` service credential reference whose value never appears in validation errors or repr;
- finite connect/read/write/pool timeouts and a bounded JSON/SSE response size.
- a bounded SSE batch event count and batch wall-clock duration, both greater than zero.

Use an injected credential resolver in runtime code. Settings hold only the reference, not the resolved API-key token. Add an explicit `smartperfetto_enabled` switch so unrelated production deployments remain valid; when enabled, endpoint and secret-reference validation is mandatory.

- [ ] **Step 2: Write RED incremental SSE tests**

The parser tests must split input at every byte boundary and cover CRLF, comments, multiple `data:` lines, blank-event delimiters, UTF-8, and a final incomplete frame. Prove that:

- only canonical non-negative decimal `id` values advance the cursor;
- `connected` without an ID never replaces the stored cursor;
- malformed JSON, negative/non-decimal IDs, oversized frames, and invalid UTF-8 fail with `engine_contract_invalid`;
- raw `data`, query, conclusion, and upstream error text never appear in `EngineEvent.message_code` or exception text;
- progress is either `None` or an integer from `0` through `100`.

Run RED:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-sse-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_smartperfetto_sse.py \
  services/api/tests/unit/test_smartperfetto_transport.py \
  services/api/tests/unit/test_security.py -q
```

Expected: FAIL because the transport/error/parser foundations do not exist.

- [ ] **Step 3: Implement the stable error and settings boundary**

Add server-only settings for the base URL, secret reference, timeouts, maximum Trace bytes, maximum JSON bytes, and maximum SSE event bytes. Keep development defaults explicit and make production fail closed when SmartPerfetto is enabled without an HTTPS endpoint or resolvable secret reference.

Implement path-segment validation for all external IDs with the upstream-compatible ASCII alphabet and length bound. Never interpolate an unchecked browser value into a URL.

- [ ] **Step 4: Implement the SSE parser**

`sse.py` accepts an async byte iterator and emits an internal frame type. It performs incremental decoding, size accounting, and canonical cursor checks before the Adapter maps data to `EngineEvent`. Do not use `splitlines()` over the entire response and do not retain the complete stream in memory.

The existing Adapter protocol returns a tuple, so one `stream()` call is explicitly a bounded batch: it closes and returns after the configured event count, a terminal event, or the configured wall-clock deadline. It must close the HTTP response on normal return, deadline, caller cancellation, and parser failure. It never waits for the lifetime of the upstream SSE connection or accumulates an unbounded tuple.

- [ ] **Step 5: Implement the shared HTTP transport behavior**

The transport must:

- use one injected `httpx.AsyncClient` with `follow_redirects=False`;
- send `Authorization: Bearer <resolved secret>` and `Accept: application/json` by default;
- add `X-Workspace-Id` only from the server-owned route ID;
- enforce response limits while streaming;
- parse a bounded JSON body once;
- map timeouts/connectivity/auth/`5xx` to stable errors without response text;
- provide a close method only when it owns the client.

The SmartPerfetto transport and artifact-download transport must be separate clients. The latter has no SmartPerfetto base URL or default Authorization header, so an engine API key cannot be sent to a signed artifact host.

- [ ] **Step 6: Run GREEN, then commit and push**

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-sse-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_smartperfetto_sse.py \
  services/api/tests/unit/test_smartperfetto_transport.py \
  services/api/tests/unit/test_security.py -q
git diff --check
git add services/api/src/perfpilot_api/config.py \
  services/api/src/perfpilot_api/engines/__init__.py \
  services/api/src/perfpilot_api/engines/errors.py \
  services/api/src/perfpilot_api/engines/smartperfetto_transport.py \
  services/api/src/perfpilot_api/engines/sse.py \
  services/api/tests/unit/test_smartperfetto_sse.py \
  services/api/tests/unit/test_smartperfetto_transport.py \
  services/api/tests/unit/test_security.py
git diff --cached --check
git commit -m "feat: add SmartPerfetto transport foundations"
git push origin HEAD:main
```

## Task 3: Provision one idempotent server-owned workspace per team

**Files:**

- Create: `services/api/src/perfpilot_api/services/engine_workspaces.py`
- Create: `services/api/tests/unit/test_engine_workspaces.py`
- Create: `services/api/tests/integration/test_engine_workspace_repository.py`

- [ ] **Step 1: Write RED repository and ownership tests**

Using the real PostgreSQL fixture, prove:

- a team and engine claim one `TeamEngineWorkspace` row;
- two concurrent claimers converge on one row and one version owner;
- activation is compare-and-swap protected by expected version and current `provisioning` state;
- a team cannot load another team's mapping by presenting the other external workspace ID;
- repository APIs require `team_id` and `engine_id`; there is no public lookup by external ID alone;
- failure records only a stable code and never a response body.

- [ ] **Step 2: Write RED HTTP reconciliation tests with `httpx.MockTransport`**

Cover:

1. existing active mapping returns without HTTP;
2. provisioning lists upstream workspaces before creating;
3. an exact server-derived candidate from the list is adopted;
4. absence causes one create request with reviewed quota/retention policy;
5. a create conflict/`5xx` triggers one list reconciliation and adopts only the exact candidate;
6. a workspace with a similar name but different ID is never adopted;
7. caller-supplied workspace IDs, names, quotas, and retention policies are impossible in the service signature;
8. no delete request is issued on any failure path.

The deterministic candidate is `pp-` plus a UUIDv5 derived from the PerfPilot team UUID and a code-owned namespace. It is an opaque server decision, not a browser field. The human workspace name contains no customer/team name.

The deployed SmartPerfetto service credential is tenant-bound and must carry only the required management and analysis scopes (`tenant:manage`, `trace:read`, `trace:write`, `agent:run`, and `report:read`). Workspace route context comes from the server-owned path/header. No per-user token is accepted.

- [ ] **Step 3: Run RED**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-workspaces-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_workspaces.py \
  services/api/tests/integration/test_engine_workspace_repository.py -q
```

Expected: FAIL because the service and repository do not exist.

- [ ] **Step 4: Implement repository claim/CAS behavior**

Use PostgreSQL `INSERT ... ON CONFLICT DO NOTHING`, row locks where ownership changes, and version-qualified `UPDATE`. A stale worker must receive a stable stale-version error and must not overwrite an active mapping.

- [ ] **Step 5: Implement list-before-create reconciliation**

Parse only the frozen consumer fields. POST body contains the server candidate, a generic name, and code-owned quota/retention dictionaries. POST is treated as non-idempotent: after any ambiguous create result, reconcile with GET instead of blindly POSTing again.

Serialize the body exactly as `workspace-create-request.json` after substituting the deterministic candidate. Exact candidate ID, not display name, proves ownership during reconciliation.

- [ ] **Step 6: Run GREEN and the existing mapping constraints**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-workspaces-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_workspaces.py \
  services/api/tests/integration/test_engine_workspace_repository.py \
  services/api/tests/integration/test_analysis_repository.py -q
```

- [ ] **Step 7: Commit and push Task 3**

```bash
git diff --check
git add services/api/src/perfpilot_api/services/engine_workspaces.py \
  services/api/tests/unit/test_engine_workspaces.py \
  services/api/tests/integration/test_engine_workspace_repository.py
git diff --cached --check
git commit -m "feat: provision SmartPerfetto team workspaces"
git push origin HEAD:main
```

## Task 4: Materialize the Trace, multipart-upload it, and submit analysis

**Files:**

- Modify: `services/api/src/perfpilot_api/engines/contracts.py`
- Create: `services/api/src/perfpilot_api/engines/smartperfetto.py`
- Modify: `services/api/src/perfpilot_api/engines/__init__.py`
- Modify: `services/api/tests/unit/test_engine_contracts.py`
- Create: `services/api/tests/unit/test_smartperfetto_adapter.py`

- [ ] **Step 1: Write RED descriptor and input tests**

Prove the Adapter descriptor is:

```text
engine_id = smartperfetto
adapter_version = 1.0.0
profiles = auto,startup,scroll
required_inputs = trace
accepted_contracts = workspace-agent-v1
resource_profile = network_service
```

Reject missing/multiple Trace inputs, unsupported kinds, invalid size/hash metadata, missing server workspace mapping, and timeout values outside the configured bound. Finalized artifact ownership is enforced by the server-side claim builder before it constructs `EngineInput`; the Adapter has no client-controlled artifact-state field.

Extend `EngineRunRef` with an optional, opaque `external_workspace_id` field. Keep it last with a default of `None` so non-workspace Adapters and existing positional test doubles remain compatible. SmartPerfetto must populate it because every post-submit endpoint is workspace-scoped and a restarted process must reconstruct the full route from the control row, not from in-memory state.

Extend the generic contract with two immutable types:

```python
@dataclass(frozen=True, slots=True)
class EngineEventBatch:
    run_ref: EngineRunRef
    events: tuple[EngineEvent, ...]

@dataclass(frozen=True, slots=True)
class EngineStatus:
    run_ref: EngineRunRef
    state: ExecutionStateValue
    stable_error_code: str | None
    retryable: bool
```

`EngineAdapter.stream()` returns `EngineEventBatch`, and the protocol gains `status(run_ref) -> EngineStatus`. Update every existing fake Adapter. The returned/refreshed `run_ref` is how the execution service CAS-persists a run ID recovered after upstream restart; no Adapter may hide that update in process memory.

- [ ] **Step 2: Write RED artifact materialization tests**

With a separate `httpx.MockTransport` for the server-issued artifact URL, prove:

- bytes are streamed into a `SpooledTemporaryFile`, not accumulated in one bytes object;
- the configured maximum, declared size, and exact SHA-256 Base64 are all enforced;
- redirects are refused;
- URL and authorization data are absent from repr, logs, exception text, and returned references;
- the temporary file closes on download, upload, cancellation, timeout, and parse failure.
- the artifact GET uses a separate client and never receives the SmartPerfetto Authorization header.

- [ ] **Step 3: Write RED upload/analyze request tests**

Assert the exact request sequence and bodies:

1. GET the short-lived PerfPilot artifact URL;
2. multipart `POST /api/workspaces/{workspaceId}/traces/upload` with field name `file` and a generated filename;
3. verify both HTTP success and `success: true`;
4. `POST /api/workspaces/{workspaceId}/agent/analyze` with the returned Trace ID;
5. return only engine/workspace/session/run/cursor in `EngineRunRef`.

Also prove:

- the Adapter never calls `/traces/upload-url`;
- it never places the signed URL in SmartPerfetto JSON, headers, or multipart metadata;
- upstream `port`, `leaseId`, lease reason, request ID, and observability details are discarded;
- analyze `options.analysisMode` is always one of upstream `auto|fast|full`, never `startup|scroll`;
- startup and scroll use code-owned fixed query templates; an optional user question is appended only as analysis context and is never persisted by the Adapter;
- `auto` first submits `{preset:"smart", smartAction:"preview", analysisMode:"auto"}`, reads only the typed `smartScenePreview` projection from the bounded terminal SSE frame, and then submits a second deep-dive run with `smartAction:"analyze"` plus the preview `reportId`;
- auto selection admits only startup types (`cold_start`, `warm_start`, `hot_start`) and scroll types (`scroll`, `inertial_scroll`); all other preview scene types are ignored;
- a preview with neither supported family fails with `unsupported_trace_profile` and does not submit the deep-dive run;
- the final deep-dive `EngineRunRef`, not the preview run, is returned;
- preview timeout/caller cancellation issues a best-effort preview-session cancel and closes all responses;
- `200` plus `success:false`, quota, tombstone, auth, lease, and invalid-Trace responses map according to the stable table.

- [ ] **Step 4: Run RED**

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-submit-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider services/api/tests/unit/test_smartperfetto_adapter.py -q
```

Expected: FAIL because `SmartPerfettoAdapter` does not exist.

- [ ] **Step 5: Implement bounded submit**

Keep the upstream Trace ID local to `submit()`. Once analyze returns, the upstream persisted session owns the Trace association and later recovery uses `sessionId`. Do not add `external_trace_id` or migration `0005`.

For `auto`, keep the Trace ID in memory across the bounded preview and final deep-dive submissions. The pending control attempt is not moved to `running` until the final deep-dive submit succeeds. A crash during preview leaves the attempt retryable and may leave an upstream preview/Trace for upstream retention; it must never fabricate a durable final run reference.

The exact preview read sequence is:

1. submit the preview analyze request and retain its preview session/run IDs in memory;
2. read bounded batches from `GET /api/workspaces/{workspaceId}/agent/runs/{previewRunId}/stream`, replaying with the preview cursor and never exceeding the preview sub-deadline or overall timeout;
3. on the replayable `analysis_completed` frame, privately parse only `data.smartScenePreview` from `smart-preview-stream.sse`; ordinary progress projection still discards raw SSE data;
4. require `smartScenePreview.reportId` and a bounded `scenes` array;
5. map a valid preview with no supported scene types to `unsupported_trace_profile`;
6. map missing/malformed preview fields to `engine_contract_invalid`;
7. map preview failed/cancelled/quota/awaiting-user states through the stable table and never submit deep dive;
8. submit deep dive only with the validated report ID and allowlisted scene types.

If preview SSE ends or yields no terminal event, read `GET /api/workspaces/{workspaceId}/agent/{previewSessionId}/status`, then reconnect/replay before declaring the preview contract invalid. Task 5 applies the same one-resume-on-404 behavior to this private preview stream/status path. Tests use a fake monotonic clock/sleeper, so no real sleeps occur.

Never automatically retry multipart upload or analyze after an ambiguous transport failure inside the Adapter. Raise a stable retry decision to the orchestration layer so a new attempt is explicit and auditable.

- [ ] **Step 6: Run GREEN and protocol tests**

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-submit-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_smartperfetto_adapter.py \
  services/api/tests/unit/test_engine_contracts.py -q
```

- [ ] **Step 7: Commit and push Task 4**

```bash
git diff --check
git add services/api/src/perfpilot_api/engines/contracts.py \
  services/api/src/perfpilot_api/engines/smartperfetto.py \
  services/api/src/perfpilot_api/engines/__init__.py \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_smartperfetto_adapter.py
git diff --cached --check
git commit -m "feat: submit SmartPerfetto trace analyses"
git push origin HEAD:main
```

## Task 5: Add SSE replay, restart recovery, status, cancel, and report fetch

**Files:**

- Modify: `services/api/src/perfpilot_api/engines/smartperfetto.py`
- Modify: `services/api/src/perfpilot_api/engines/smartperfetto_contracts.py`
- Modify: `services/api/tests/unit/test_smartperfetto_adapter.py`
- Modify: `services/api/tests/unit/test_smartperfetto_sse.py`

- [ ] **Step 1: Write RED replay tests**

Prove `stream(run_ref, cursor)` returns an `EngineEventBatch` and:

- uses the run-scoped endpoint;
- sends `Accept: text/event-stream`;
- sends `Last-Event-ID` only when the cursor is a canonical decimal string;
- replays fixture events after that cursor and returns strictly increasing event IDs;
- returns no more than the configured batch size and closes the response at the event limit or wall-clock deadline;
- closes promptly when the caller task is cancelled, without committing a partially parsed event;
- projects only ID, mapped state, coarse progress, stable message code, and local occurrence time;
- ignores duplicate/older replayed event IDs;
- maps terminal `analysis_completed`, `analysis_cancelled`, `error`, and `end` without copying raw `data` text.
- returns the refreshed `EngineRunRef` when resume changes the upstream run ID.

`analysis_completed` is an observed terminal signal, not permission to mark the control execution completed. Completion is durable only after report fetch and tenant result-sink success. `end` or an empty resumed stream requires a status read so a lost terminal event cannot strand the execution.

- [ ] **Step 2: Write RED restart recovery tests**

For status, stream, cancel, and report, simulate initial `404`, then require exactly:

```http
POST /api/workspaces/{workspaceId}/agent/resume
Content-Type: application/json

{"sessionId":"..."}
```

Do not send `traceId`. Retry the original operation exactly once after successful resume. A second `404`, a mismatched session, or a malformed resume body maps to `engine_session_lost`. If resume returns a refreshed run ID, use it only after ownership-safe validation.

`status(run_ref)` returns `EngineStatus` with that refreshed ref, mapped state, stable code, and retryability. Task 6 must CAS-persist a changed run ID together with the next cursor/status update before a later process uses it.

- [ ] **Step 3: Write RED status/cancel/report tests**

Cover every state mapping and assert:

- `cancel()` sends `{}` and returns platform `canceled` for upstream `cancelled`;
- terminal status/SSE quota returns retryable `capacity_exceeded`; Task 6 ends only the current attempt and requests a new attempt while the overall 30-minute analysis deadline remains;
- `awaiting_user` is briefly observable, then the execution service cancels the session and terminates it as `failed` with `engine_interaction_required`;
- a partial but usable sanitized report is `completed` and retains a partial marker;
- a report without usable conclusion/evidence is `insufficient_data`;
- `logFile` is removed recursively from the accepted report surface;
- only a safe relative report ID may be extracted from `reportUrl`;
- arbitrary absolute report URLs are never followed;
- forbidden nested operational keys are removed and retained strings cannot contain credential markers, signed URLs, `s3://`/object-store URIs, or absolute local paths;
- a fixture with dynamically nested secret/path/object-key values reaches the sink only in redacted form, and the original unsafe values appear nowhere in repr, exceptions, or logs;
- full report/query/findings never enter an `EngineEvent`, `EngineRunRef`, error, or control persistence DTO.

- [ ] **Step 4: Run RED**

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-recovery-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_smartperfetto_adapter.py \
  services/api/tests/unit/test_smartperfetto_sse.py -q
```

- [ ] **Step 5: Implement one-resume recovery and terminal mapping**

Keep retry scope per method call. Resume is not a general retry loop. Network errors during cancel/report/status remain stable retryable outcomes when safe; callers decide whether to schedule another call and the execution service applies the wall-clock deadline.

`fetch_result()` returns `EngineResult(contract="workspace-agent-v1", ...)` with the sanitized report payload. It does not write a database or object store. That write belongs to the injected tenant result sink in Task 6.

Sanitization is a recursive allowlist projection, not a single `logFile` pop. Remove known operational/secret keys case-insensitively, reduce `reportUrl` to a validated opaque report ID, and redact unsafe retained string patterns for bearer/API credentials, signed HTTP URLs, object-store URIs, POSIX absolute paths, and Windows absolute paths. Apply depth, collection-count, string-length, and total serialized-size limits before constructing `EngineResult`. The original unsafe value must never appear in an exception.

- [ ] **Step 6: Run GREEN, then commit and push**

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-recovery-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_smartperfetto_adapter.py \
  services/api/tests/unit/test_smartperfetto_sse.py \
  services/api/tests/contract/test_smartperfetto_workspace_agent_v1.py -q
git diff --check
git add services/api/src/perfpilot_api/engines/smartperfetto.py \
  services/api/src/perfpilot_api/engines/smartperfetto_contracts.py \
  services/api/tests/unit/test_smartperfetto_adapter.py \
  services/api/tests/unit/test_smartperfetto_sse.py
git diff --cached --check
git commit -m "feat: recover SmartPerfetto analysis sessions"
git push origin HEAD:main
```

## Task 6: Persist execution references and enforce the tenant result boundary

**Files:**

- Modify: `services/api/src/perfpilot_api/engines/contracts.py`
- Modify: `services/api/src/perfpilot_api/engines/__init__.py`
- Create: `services/api/src/perfpilot_api/services/engine_executions.py`
- Modify: `services/api/tests/unit/test_engine_contracts.py`
- Create: `services/api/tests/unit/test_engine_execution_service.py`
- Create: `services/api/tests/integration/test_engine_execution_repository.py`
- Modify: `services/api/tests/integration/test_analysis_repository.py`

- [ ] **Step 1: Write RED real-PostgreSQL repository tests**

Add repository methods that always require `team_id` and `analysis_id`. Prove:

- attempt allocation is serialized and increments per analysis/engine;
- submit reference updates require expected version and atomically store workspace/session/run IDs while transitioning `pending` to `running`;
- event cursor updates are CAS protected and monotonically increasing as decimal integers;
- a refreshed workspace/session/run ref from `EngineEventBatch` or `EngineStatus` is CAS-persisted with the cursor/status update;
- duplicate replay events are no-ops, not version bumps;
- legal state transitions use `transition_engine_state()` and terminal rows cannot reopen;
- a completed SSE event can persist its cursor but cannot persist control state `completed` before a raw-result artifact UUID exists;
- finalization claims preallocate one deterministic `raw_result_artifact_id` from `execution_id`; terminal success stores only that ID, while terminal failure stores only a stable code;
- two concurrent retry reservations consume one `GlobalJob.retry_count`, terminalize the current attempt once, and return the same next pending attempt;
- control rows reject/never receive report payloads, queries, paths, URLs, headers, object keys, or evidence;
- a stale or cross-team caller cannot update an execution by presenting an execution ID, session ID, run ID, workspace ID, or cursor.

- [ ] **Step 2: Write RED service tests with fake Adapter and sink**

Define a narrow protocol:

```python
class EngineResultSink(Protocol):
    async def write(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        execution_id: UUID,
        artifact_id: UUID,
        engine_id: str,
        result: EngineResult,
    ) -> UUID: ...
```

Freeze orchestration outcomes in `engines/contracts.py`:

```python
RetryMode = Literal["reconnect", "new_attempt"]

@dataclass(frozen=True, slots=True)
class EngineRetryDirective:
    mode: RetryMode
    execution_id: UUID
    attempt_number: int
    stable_error_code: str
    retry_after_seconds: int

@dataclass(frozen=True, slots=True)
class EngineStepOutcome:
    execution_id: UUID
    state: ExecutionStateValue
    retry: EngineRetryDirective | None
```

These types carry no URLs, payloads, upstream text, or external IDs. `new_attempt` always names an already reserved pending `EngineExecution`; it is never a suggestion for an unsynchronized caller to allocate one.

Tests must prove the service:

1. resolves workspace from `team_id` through `EngineWorkspaceService`;
2. selects Adapter from the explicit registry;
3. creates a pending execution from the checked engine lock;
4. persists only workspace/session/run IDs after submit;
5. resumes streaming from the stored cursor, ignores replay duplicates, and persists a refreshed run ID returned after resume;
6. treats `analysis_completed` as a fetch trigger, and treats `end`/empty replay as a status-recovery trigger;
7. passes a completed/insufficient result to the tenant sink before marking terminal;
8. CAS-claims finalization by preallocating the execution's deterministic artifact UUID before calling the sink;
9. stores only that artifact UUID in control and requires the sink to return the same UUID;
10. leaves the execution non-terminal if the sink fails, even when the completed event cursor was saved;
11. lets a later worker retry the same artifact UUID idempotently after a crash or sink-success/CAS-failure only while the row is still `running`;
12. makes duplicate completion and cancel-versus-completion races converge through terminal CAS: cancel may still win while sink work is pending, or completion wins first and later cancel becomes a no-op;
13. maps nonretryable Adapter errors to failure and retryable errors to bounded retry outcomes without storing exception text;
14. never accepts external workspace/session/run IDs from a browser/request DTO;
15. keeps retryable stream/connect errors on the same nonterminal execution before the deadline;
16. converts terminal `quota_exceeded` into a failed capacity-limited attempt plus a retry directive for the overall analysis, rather than terminalizing the parent analysis;
17. fails deterministically with `engine_timeout` when the authoritative `GlobalJob.started_at` deadline reaches 30 minutes;
18. on `awaiting_user`, persists the observation, cancels upstream, and transitions to `failed/engine_interaction_required` so no execution is stranded;
19. after a retryable sink failure, allows idempotent sink retry, explicit cancellation, or deadline convergence to `failed/result_persistence_failed`;
20. atomically reserves retry budget and the next pending attempt under the `GlobalJob` lock, so concurrent quota handlers return the same directive and cannot exceed `max_retries`.

The fake sink must inspect the full sanitized result, while the fake repository records every argument. Assert no payload field reaches a repository call. Add explicit duplicate-completion, crash-after-sink, sink-success/CAS-failure, cancel-racing-completion, and claim-crash-cancel-recovery-without-sink cases.

- [ ] **Step 3: Run RED**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-executions-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/integration/test_engine_execution_repository.py -q
```

- [ ] **Step 4: Implement repository CAS operations**

Use one explicit persistence DTO containing only the columns of `EngineExecution`. Serialize attempt allocation by locking the authoritative `GlobalJob` row before selecting `max(attempt_number) + 1`. Parse and compare cursors as integers but store canonical decimal strings. Do not add a generic `**metadata` or raw JSON field.

Persist nonterminal state changes from events. For `analysis_completed`, save only the newer cursor and fetch/store the result before the terminal transition. For `analysis_cancelled` and stable terminal errors, terminal persistence may occur without a result artifact. For `end` or an empty replay, query status before deciding the next state.

When a batch or status carries a refreshed run ref, validate that engine/workspace/session are unchanged and CAS-update only `external_run_id` with the cursor/status. A changed workspace or session is an ownership error and fails closed.

Finalization uses the existing `raw_result_artifact_id` as a durable idempotency marker, without a migration:

1. derive one UUIDv5 from `execution_id` and a code-owned result namespace;
2. CAS `raw_result_artifact_id` from null to that UUID while state is `running` and bump version;
3. once set, cursor updates are no-ops, but cancellation remains allowed until the terminal completion CAS wins;
4. call the sink with that UUID; its future production implementation must create-or-load exactly that tenant artifact ID;
5. CAS to `completed`/`insufficient_data` only after the sink returns the same UUID;
6. after a crash, a worker repeats the same idempotent sink call and terminal CAS only when the marker row is still `running`; canceled/failed rows never invoke the sink.

The marker reserves identity, not terminal ownership. Completion and cancellation each use a state/version CAS; whichever terminal CAS wins is authoritative. If cancellation wins while a sink call is already in flight, the idempotent artifact may finish and remain attached for audit, but completion cannot reopen the canceled row. If cancellation wins after the marker claim but before the sink starts, recovery observes `canceled` and must not call the sink. If the sink repeatedly fails, later workers retry the same artifact ID only while the row remains `running`; deadline expiry closes it as `failed/result_persistence_failed`. Add the explicit claim-crash-cancel-recover race test. This prevents duplicate result artifacts and permanent `running` rows without holding a database transaction across network or object-storage work.

- [ ] **Step 5: Implement the internal execution service**

The service is internal only; do not add FastAPI routes or background-worker entry points in this packet. Cancellation is an explicit internal method scoped by team/analysis/execution ownership. Provide a small internal composition function that accepts settings, control sessions, credential resolver, the two HTTP clients, and a required result sink, then constructs the transport, Adapter registry, workspace service, and execution service. Unit-test this composition without wiring `main.py`; public runtime wiring belongs to the later Trace-upload/orchestrator packet.

On completion, claim the deterministic artifact UUID, call `EngineResultSink.write()` idempotently, require the same UUID back, then CAS the row to `completed` or `insufficient_data`. The production tenant sink is deliberately supplied by the later Report Normalizer packet; reject a missing sink rather than falling back to control storage or process memory.

Retry handling distinguishes operation class:

- a retryable disconnect/timeout while a session exists leaves the execution `running`, preserves its cursor, records only the stable code, and returns a bounded retry outcome;
- a safe submit-capacity response and upstream terminal `quota_exceeded` both close only the current attempt as `failed/capacity_exceeded`, atomically reserve retry budget plus the next pending attempt, and return a `new_attempt` directive;
- monthly quota and other nonretryable errors close the attempt with their stable code;
- no retry outcome is returned after the overall 30-minute deadline derived from `GlobalJob.started_at` (falling back to `created_at`) or after `max_retries` is exhausted.

For `new_attempt`, one repository transaction locks `GlobalJob`, locks/validates the current execution, checks deadline and `retry_count < max_retries`, terminalizes the current attempt with the stable capacity code, increments `GlobalJob.retry_count` and version, and creates the next pending attempt by copying only immutable lock/config/input hashes. A duplicate concurrent call returns that same next attempt after validation. It never creates another row or consumes the budget twice. The later orchestrator mints fresh artifact authorization and submits the already reserved execution ID from the directive; it does not allocate another attempt. Add real-PostgreSQL concurrency tests for both submit-capacity and terminal-quota handling at the final available retry slot.

The packet does not add a scheduler loop, but its typed outcome and tests make the future orchestrator decision explicit. A retryable attempt failure must not change the parent `GlobalJob` to terminal `failed`.

- [ ] **Step 6: Run GREEN and database regression**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-executions-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q
```

Expected: pass with no PostgreSQL skips and no migration change.

- [ ] **Step 7: Commit and push Task 6**

```bash
git diff --check
git add services/api/src/perfpilot_api/engines/contracts.py \
  services/api/src/perfpilot_api/engines/__init__.py \
  services/api/src/perfpilot_api/services/engine_executions.py \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/integration/test_analysis_repository.py
git diff --cached --check
git commit -m "feat: orchestrate SmartPerfetto engine executions"
git push origin HEAD:main
```

## Task 7: Run the packet gate and freeze the next boundary

**Files:**

- Modify only when a command or verified expectation is wrong: this plan
- Do not create an empty source commit

- [ ] **Step 1: Run formatting and focused unit/contract tests**

```bash
git diff --check
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-focused \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/contract/test_smartperfetto_workspace_agent_v1.py \
  services/api/tests/unit/test_smartperfetto_transport.py \
  services/api/tests/unit/test_smartperfetto_sse.py \
  services/api/tests/unit/test_smartperfetto_adapter.py \
  services/api/tests/unit/test_engine_workspaces.py \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_execution_service.py -q
```

- [ ] **Step 2: Run real-PostgreSQL integration tests**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-pg \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/integration/test_engine_workspace_repository.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q
```

Expected: pass with no PostgreSQL skips.

- [ ] **Step 3: Run the full backend regression**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-smartperfetto-all \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider services/api/tests -q
```

Any unrelated failure stops the packet; do not weaken, skip, or xfail it.

- [ ] **Step 4: Perform a secret/privacy audit**

```bash
rg -n "logFile|upload-url|download_url|Authorization|X-API-Key|object_key|bucket" \
  services/api/src/perfpilot_api/engines \
  services/api/src/perfpilot_api/services/engine_workspaces.py \
  services/api/src/perfpilot_api/services/engine_executions.py
```

Review every match. Allowed matches are typed private inputs, the fixed Authorization header construction, and explicit rejection/sanitization. No logging or control persistence is allowed.

- [ ] **Step 5: Verify commit and remote SHA**

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
git status --short
```

Expected: fast-forward or up-to-date push, matching SHAs, and only `.superpowers/` may remain untracked. Never stage `.superpowers/`.

- [ ] **Step 6: Record the next plan boundary**

The next implementation plan is the Android Memory Worker/Adapter packet. It may start only after this packet is green and pushed. Report Normalizer and PerfPilot AI remain a separate packet after both engine Adapters have stable result contracts.

## Packet acceptance

The packet is complete only when all of these are true:

- fixtures identify PerfPilot as contract owner and the exact upstream pin; no upstream handshake is claimed;
- every source-changing task was independently committed and fast-forward pushed to `origin/main`;
- workspace creation is list-before-create, deterministic, server-owned, and race-safe;
- browser/request data cannot choose an external workspace/session/run identifier;
- Trace bytes are bounded and hash/size verified before multipart upload;
- no signed URL is forwarded to SmartPerfetto and `/upload-url` is never used;
- startup/scroll never appear as upstream `analysisMode` values;
- auto performs bounded Smart preview, selects only startup/scroll scene families, and returns `unsupported_trace_profile` when neither exists;
- replay is a bounded batch, uses canonical `Last-Event-ID`, and survives one upstream restart via session-only resume while persisting a refreshed run ID;
- `cancelled`, retryable quota, awaiting-user, partial, insufficient evidence, auth, timeout, and malformed contract cases map deterministically;
- `logFile`, arbitrary report URLs, raw errors, queries, evidence, object paths, and credentials never enter control persistence or logs;
- a terminal successful execution contains only the opaque tenant raw-result artifact UUID, never the report payload;
- no `external_trace_id` column or `0005` migration was added;
- focused, contract, real-PostgreSQL, migration, and full API tests pass;
- local HEAD and remote `main` resolve to the same SHA.
