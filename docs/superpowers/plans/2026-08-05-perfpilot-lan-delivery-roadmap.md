# PerfPilot LAN Delivery Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement these plans in order. Use superpowers:test-driven-development for every behavior change and superpowers:verification-before-completion before each commit.

**Goal:** Deliver the approved Ubuntu LAN deployment and a macOS, Windows, and Linux Device Agent without regressing the existing local Trace-upload workflow.

**Architecture:** The Ubuntu host owns the browser application, FastAPI control plane, PostgreSQL, Redis, S3-compatible storage, SmartPerfetto, Android Memory, and synthesis workers. Android devices remain attached to user computers. An outbound-only Agent registers to one team, reports sanitized device inventory, leases signed tasks, uploads capture artifacts directly to object storage, and acknowledges cancellation. The existing local runtime remains a separate development path.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL 17, Redis 8, S3-compatible object storage, React 19/Vinext, Docker Compose, Caddy, PyInstaller, launchd, Windows Service/WiX, systemd/deb, pytest, Vitest.

---

## Baseline and authority

- Approved design: `docs/superpowers/specs/2026-08-05-perfpilot-lan-deployment-device-agent-design.md`.
- Deployment target: `rivotek@10.166.0.125`.
- Persistent data root: `/data/perfpilot`.
- Release root: `/opt/perfpilot`.
- Heavy analysis concurrency: exactly one task.
- Browser and Agent entry point: `https://10.166.0.125` using the deployment CA.
- Existing `services/api/src/perfpilot_api/local_app.py` and `services/api/src/perfpilot_api/local_device.py` remain available for local development.
- The plaintext development password `ray_wu` is forbidden in LAN production configuration.

## Execution order

1. [Agent control plane and web device selection](./2026-08-05-perfpilot-agent-control-plane.md)
2. [Cross-platform Device Agent](./2026-08-05-perfpilot-cross-platform-device-agent.md)
3. [Ubuntu LAN deployment](./2026-08-05-perfpilot-ubuntu-lan-deployment.md)
4. [Operations and end-to-end acceptance](./2026-08-05-perfpilot-lan-operations-acceptance.md)

Plan 2 starts only after Plan 1 publishes contract version `1.0`. Plan 3 may build generic container images while Plan 2 is in progress, but its deployment smoke test waits for Plans 1 and 2. Plan 4 is the closure gate.

## Delivery gates

| Gate | Required evidence |
| --- | --- |
| Control plane | PostgreSQL migration round trip, Agent API contract tests, cross-team denial tests, browser device-selection tests |
| Agent core | Fake-ADB tests, signed-task rejection tests, interrupted-upload resume, lease loss, five-second cancellation |
| Platform packages | Native CI artifacts for macOS `.pkg`, Windows `.msi`, Linux `.deb`; install/start/stop/uninstall smoke tests |
| Ubuntu deployment | Compose config validation, container health, HTTPS/CA validation, persistent restart, pinned engine versions |
| Operations | Reset safety, encrypted backup, isolated restore, resource gate, reconciler recovery, real-device LAN receipt |

## Commit sequence

Each numbered item is an independent commit after its focused tests pass:

1. `feat: add Agent registration contracts and storage`
2. `feat: register and authenticate remote Agents`
3. `feat: publish team device inventory`
4. `feat: lease signed device tasks`
5. `feat: coordinate Agent uploads and cancellation`
6. `feat: enable remote device analyses in web`
7. `feat: scaffold cross-platform Device Agent`
8. `feat: discover and report Android devices`
9. `feat: execute leased Trace captures`
10. `build: package Device Agent for three platforms`
11. `build: add PerfPilot LAN containers`
12. `ops: add Ubuntu bootstrap and release commands`
13. `ops: add backup restore and reconciliation`
14. `test: add LAN end-to-end acceptance`

Do not squash these commits during implementation. Do not push or deploy a commit whose focused test command is red.

## Whole-repository verification

Run after each plan and once more before the first Ubuntu deployment:

```bash
.venv/bin/ruff check services/api/src services/api/tests agents/device-agent/src agents/device-agent/tests
.venv/bin/pytest -p no:cacheprovider services/api/tests agents/device-agent/tests -q
npm run lint
npm run test:unit
npm run test:ssr
docker compose -f infra/lan/compose.yaml config --quiet
```

Expected: every command exits `0`; PostgreSQL-only tests may skip locally only when `PERFPILOT_REQUIRE_POSTGRES_TESTS` is unset, but they must run in CI and on Ubuntu acceptance.
