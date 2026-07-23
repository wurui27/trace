# PerfPilot Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved PerfPilot production platform in six independently testable execution packets, ending with a real RKGallery device run, a local standalone runtime, and direct Trace upload.

**Architecture:** The FastAPI control plane owns identity, tenant routing, uploads, orchestration, leases, claims, and API state. The versioned `tracekit`, analysis Worker, device Agent, and Sites-hosted Web communicate only through checked-in JSON contracts and immutable artifacts. Cloud adapters are replaced by SQLite, local files, and an in-process queue in Phase 2 without changing those contracts.

**Tech Stack:** Python 3.12, uv workspace, FastAPI 0.139.x, Pydantic 2.13.x, SQLAlchemy 2.0.x, PostgreSQL, Redis Streams, S3-compatible storage, Perfetto v56.1, ADB, React 19, Next.js 16/Vinext, Cloudflare Workers, Vitest, pytest.

---

## Execution packets

Run these plans in order unless a packet explicitly says it can overlap:

1. [Phase 1 — Platform foundation and control plane](./2026-07-23-perfpilot-phase1-control-plane.md)
2. [Phase 1 — Tracekit and analysis Worker](./2026-07-23-perfpilot-phase1-analysis-worker.md)
3. [Phase 1 — Device Agent and RKGallery fixture](./2026-07-23-perfpilot-phase1-device-agent.md)
4. [Phase 1 — Web integration and real-device acceptance](./2026-07-23-perfpilot-phase1-web-acceptance.md)
5. [Phase 2 — Local standalone runtime](./2026-07-23-perfpilot-phase2-local-runtime.md)
6. [Phase 3 — Direct Trace upload](./2026-07-23-perfpilot-phase3-trace-upload.md)

Packets 2 and 3 may run in parallel after Packet 1 publishes contract version `1.0`. Packet 4 starts only after the API, Worker, and Agent contract suites are green. Packet 5 starts only after the Phase 1 RKGallery acceptance gate passes, and Packet 6 starts only after Packet 5 passes its local RKGallery and restart-recovery gates.

## Approved-design coverage

| Design sections | Owning implementation plan and gate |
| --- | --- |
| 1–4 Goals, scope, and locked decisions | This roadmap; every packet preserves the approved order and exclusions |
| 5–7 Architecture, service boundaries, and repository layout | Packets 1–4; repository layout and cross-service contract gates |
| 8 Identity, roles, tenant databases, and development administrator | Packet 1 Tasks 3–5; Packet 4 Tasks 3 and 7 |
| 9 Immutable object storage | Packet 1 Task 6; Packet 3 Task 4; Packet 5 Task 3 |
| 10 Task, sample, lease, and claim state machines | Packet 1 Tasks 7–10; Packets 2–3 consumer/executor tasks; Packet 6 Tasks 2 and 4 |
| 11 API, signed snapshots, errors, and reliable delivery | Packet 1 Tasks 1, 4, and 7–9; Packet 3 Tasks 1 and 8; Packet 4 Tasks 1–2 |
| 12 End-to-end data flow | Packet 4 Tasks 4–9 and the RKGallery acceptance receipt |
| 13 APK preflight and three device scenarios | Packet 3 Tasks 3–9 |
| 14 Analysis, capabilities, provenance, and evidence rules | Packet 2 Tasks 2–10; Packet 4 Task 6 |
| 15 Retry, checkpoint, lease, claim, and restart recovery | Packet 1 Tasks 8–11; Packet 2 Tasks 9–10; Packet 3 Task 8; Packet 5 Tasks 4–7 |
| 16 Security, audit, retention, isolation, and operations | Packet 1 Tasks 4–6 and 10–11; Packet 2 Task 10; Packet 3 Tasks 2, 4, and 8–10; Packet 4 Tasks 1, 3, and 8–10 |
| 17 Local standalone runtime | Packet 5 |
| 18 Direct Trace upload | Packet 6 |
| 19 Test strategy and fault injection | Focused RED/GREEN steps in every task; final gate in every packet |
| 20 Delivery and release gates | Each packet’s final task plus this roadmap’s remote protocol |
| 21 Explicit environment assumptions | Packet 4 Task 9 and Packet 5 Task 7 fail closed when prerequisites are absent |

## Locked repository layout

```text
platform-web/
├── app/                                  # Existing Sites/Vinext Web
├── worker/                               # Sites Worker and same-origin API proxy
├── contracts/v1/                         # JSON Schema authority and examples
├── services/
│   ├── api/
│   │   ├── src/perfpilot_api/            # FastAPI control plane
│   │   ├── migrations/{control,tenant}/  # Independent database migrations
│   │   └── tests/
│   └── trace-worker/
│       ├── src/perfpilot_worker/          # Validator and full analysis consumers
│       └── tests/
├── agents/
│   └── device-agent/
│       ├── src/perfpilot_agent/           # ADB device-farm Agent
│       ├── fixtures/rkgallery/             # APK-bound fixture generated on a real device
│       └── tests/
├── tracekit/
│   ├── src/tracekit/                      # Migrated capture, SQL, models, rules, CLI
│   └── tests/
├── local-runtime/
│   ├── src/perfpilot_local/               # Phase 2 adapters and supervisor
│   └── tests/
├── infra/                                 # Compose, container, migration and fault scripts
├── tests/e2e/                             # Cross-service and real-device acceptance
├── pyproject.toml                         # uv workspace root
├── uv.lock                                # Exact Python dependency resolution
└── docs/superpowers/
```

Do not add production behavior to the existing `db/` D1 example. The production business database remains PostgreSQL behind FastAPI. The Sites Worker is a signed same-origin proxy, not a second control plane.

## Global implementation rules

- Every behavior change begins with a failing focused test.
- Every RED step records the expected failure message before implementation.
- Every GREEN step runs the focused test and the packet-level regression command.
- Every source-changing task ends in one non-interactive Git commit and that commit is fast-forward pushed to `origin/main` before the next task is integrated. A verification/deployment-only task creates no empty commit and operates on the preceding verified SHA.
- Each packet re-verifies the terminal remote SHA; force push is forbidden.
- No packet commits passwords, cookies, Agent tokens, DSNs, bucket credentials, presigned URLs, customer artifacts, APKs, or Trace files.
- Production code never reads `/Users/ray/Desktop/trace/tools` at runtime. That directory is migration input only.
- Queue messages contain only opaque identifiers and routing metadata.
- Worker analysis never depends on a live device lease.
- The Web never selects a database or bucket and never calls the FastAPI origin directly.
- Site changes use the existing project ID in `.openai/hosting.json`, are pushed before a Sites version is saved, and deploy only that saved version.

## Environment gates

- Tasks that do not need containers may proceed on the current Mac. Before Packet 1 Task 11, `docker compose version` or an explicitly supplied compatible OCI/Compose host must succeed. The executor must not auto-install a privileged runtime.
- Before the Phase 1 private cloud gate, require a controlled Linux/OCI host, HTTPS API origin, PostgreSQL, Redis, S3-compatible versioned storage, encrypted secret mounts, and non-production acceptance credentials.
- Before real-device acceptance, require the user-supplied RKGallery APK, dataset, registered Agent, one explicitly selected Android serial, platform-tools, and the checked-in fixture hash. Missing prerequisites stop the acceptance task; they never reduce sample requirements.
- Before protected Perfetto integration tests, require `PERFPILOT_TEST_TRACE_DIR`; fixture loaders verify the checked-in hashes and never commit or skip missing raw Trace data.
- Before a Sites version is saved, require the FastAPI origin and proxy-signing secret in the Sites runtime configuration. Never print either secret.

## Per-task Git protocol

Every task uses the same closeout order:

1. Run `git diff --check` and `git status --short`.
2. Run the task’s exact printed `git add` command.
3. Run `git diff --cached --check`.
4. Run the task’s exact printed `git commit -m` command.

Do not combine unrelated task files. If `git status --short` shows a user change outside the task, leave it unstaged.

After each task commit, the integration owner runs:

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
```

The ancestor command must exit `0`. When Packets 2 and 3 are developed in parallel, only one integration owner pushes at a time. The other worktree rebases its still-unpushed task commit onto the new `origin/main`, reruns that task’s GREEN and packet-regression commands, and then uses the same fast-forward protocol. Never use force push.

## Per-packet verification protocol

Run the packet-specific command first, then the repository regression:

```bash
test -n "$PERFPILOT_TEST_TRACE_DIR"
uv sync --locked --all-packages --all-extras --dev
uv run pytest -q
npm ci
npm run lint
npm test
```

For packets that do not yet contain all Python packages, run only the package commands named in that packet. A packet cannot be called complete because a narrower unit test passed.

## Remote integration protocol

At the end of each packet:

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
git status --short
```

Expected: the ancestor check exits `0`, the push is fast-forward, both SHAs are identical, and status is empty.

## Roadmap checklist

### Task 1: Execute the control-plane packet

- [ ] Follow every checkbox in `2026-07-23-perfpilot-phase1-control-plane.md`.
- [ ] Confirm its contract, unit, integration, migration, isolation, and fault tests are green.
- [ ] Push its terminal commit to `origin/main` and verify the SHA.

### Task 2: Execute analysis and Agent packets

- [ ] Run `2026-07-23-perfpilot-phase1-analysis-worker.md`.
- [ ] Run `2026-07-23-perfpilot-phase1-device-agent.md`.
- [ ] Run the shared `contracts/v1` compatibility suite against API, Worker, and Agent.
- [ ] Push both packet terminal commits to `origin/main` in dependency order.

### Task 3: Execute Web integration and RKGallery acceptance

- [ ] Follow `2026-07-23-perfpilot-phase1-web-acceptance.md`.
- [ ] Run the real-device RKGallery acceptance without simulated API data.
- [ ] Push the exact source, save a Sites version for that commit, deploy it privately, and inspect deployment status.

### Task 4: Execute the local-runtime packet

- [ ] Follow `2026-07-23-perfpilot-phase2-local-runtime.md`.
- [ ] Prove restart recovery from SQLite and local immutable artifacts.
- [ ] Push and verify the packet commit.

### Task 5: Execute the direct-Trace packet

- [ ] Follow `2026-07-23-perfpilot-phase3-trace-upload.md`.
- [ ] Prove startup and scroll Trace upload use the same report contract without creating device work.
- [ ] Prove trace-only `memory_cycle` is rejected with `unsupported_trace_scenario`.
- [ ] Push, deploy the saved Sites version, and verify the remote SHA and production route.
