# PerfPilot

PerfPilot analyzes Android performance evidence and publishes versioned reports.

## Local report workspace

The local runtime keeps SmartPerfetto and PerfPilot decoupled. Configure an
OpenAI-compatible model for PerfPilot's one evidence-validated report pass,
then restart the workspace with one command:

```bash
export PERFPILOT_LOCAL_AI_BASE_URL="https://your-provider.example/v1/"
export PERFPILOT_LOCAL_AI_MODEL="your-model"
export PERFPILOT_LOCAL_AI_TOKEN="your-token"
export PERFPILOT_LOCAL_AI_PROVIDER_NAME="local-provider"
export PERFPILOT_LOCAL_AI_THINKING="disabled"
cp .dev.vars.example .dev.vars
npm run dev:restart
```

For persistent local use, store the same five values in
`.perfpilot/local-control/perfpilot-ai.env` and set the file mode to `600`.
`npm run dev:api` and `npm run dev:restart` load this ignored private file
automatically; credentials are never committed to Git.

`npm run dev:restart` preserves local analysis history and starts
SmartPerfetto on `127.0.0.1:3001`, the PerfPilot API on `127.0.0.1:8000`, and
the web workspace on `http://localhost:3000`. It expects the SmartPerfetto
backend at `~/SmartPerfetto/backend`; when the checkout lives elsewhere, set
`PERFPILOT_LOCAL_SMARTPERFETTO_ROOT` to its `backend` directory before running
the command. Logs and managed process IDs are stored in `.perfpilot/run`.
To intentionally delete all local analysis data without starting services, run
`bash scripts/restart-local.sh --reset-only`.

Open `http://localhost:3000`. Completed analyses expose an **打开完整报告**
link. The report page shows SmartPerfetto provenance, one PerfPilot AI report
pass, evidence-backed findings, recommendations, retest steps, known
limitations, and a **下载 PDF** action. New reports run one AI round and store
`round-1.json`; legacy three-round directories remain readable. Runtime state
and report artifacts live below `.perfpilot/local-runtime`; PerfPilot never
persists provider tokens there.

For a local device analysis, connect exactly one authorized Android device and
select it in the web page before uploading the APK. PerfPilot discovers `adb`
and `aapt2` from `ANDROID_SDK_ROOT` / `ANDROID_HOME`, the standard Android SDK
directory on macOS, Windows, or Linux, and then `PATH`. Use absolute
`PERFPILOT_LOCAL_ADB` and `PERFPILOT_LOCAL_AAPT2` overrides for a custom SDK.
The local runtime installs the APK, captures startup and scroll Perfetto traces,
and collects the Android memory evidence archive in the background. It loads the
Android Memory engine from `~/Android-App-Memory-Analysis` by default; set
`PERFPILOT_LOCAL_ANDROID_MEMORY_ROOT` when that checkout lives elsewhere. The
current checkout commit is pinned for each server process, so pulling an engine
update only requires restarting the local server.

The optional runtime overrides are `PERFPILOT_LOCAL_SMARTPERFETTO_ROOT`,
`PERFPILOT_LOCAL_SMARTPERFETTO_URL`, `PERFPILOT_LOCAL_DATA_DIR`,
`PERFPILOT_LOCAL_API_ORIGIN`, `PERFPILOT_LOCAL_ADB`,
`PERFPILOT_LOCAL_AAPT2`, and `PERFPILOT_LOCAL_ANDROID_MEMORY_ROOT`.

## Analysis reliability and health

`GET /v1/health` is a lightweight liveness check: `{"status":"ok"}` means the
API process can answer requests. `GET /v1/readiness` reports the safe aggregate
state of storage, SmartPerfetto, AI, Agent, device, source, and supervisor
capabilities. `GET /v1/teams/{team_id}/health` applies the same model to the
authenticated team's Agent, device, and source availability. Health responses
never include service URLs, local paths, credentials, or source content.

SmartPerfetto processing, source reading, and the PerfPilot AI Chinese summary
do not have a fixed total deadline while their task is still alive. After three
minutes without new progress the analysis is marked slow; after ten minutes it
is shown as waiting for its upstream dependency, not failed. Device acquisition,
capture, and control operations retain bounded waits and retries because they
hold exclusive resources.

A normal service restart reloads persisted analyses and reconciles the existing
upstream run, source task, or report generation. It must not submit a duplicate
task. The explicit local reset command is different: it deletes analysis data
without creating a backup, while preserving users, Agents, and registered source
workspaces.

Before a release, run the manual real-device check against an approved online
Agent and a ready source workspace. The command prompts for the account and a
hidden password, keeps neither, and prints only a redacted JSON summary:

```bash
PYTHONPATH=services/api/src:agents/device-agent/src .venv/bin/python \
  scripts/verify-real-device-reliability.py \
  --server-url https://server.example \
  --package com.rivotek.mediacenter \
  --activity mediacenteractivity \
  --test-type cold_start \
  --duration-seconds 15 \
  --source-workspace-id 71000000-0000-4000-8000-000000000001
```

The verifier creates one schema 1.3 remote-device analysis, never probes host
ADB, follows server-authoritative Chinese progress, and requires a completed
generation-1 report, SmartPerfetto HTML, Chinese synthesis, and strong source
references. It intentionally has no automatic total timeout for the three
analysis-heavy stages; use Ctrl-C to stop the manual check.

### Local analysis runtime boundaries

The local API keeps routes and dependency composition in `local_app.py`; analysis
state rules live in focused modules so a contract change has one clear owner:

| Change | Module |
| --- | --- |
| persisted state, public schema 1.0–1.3, report reconstruction | `local_analysis_projection.py` |
| legal state transitions and terminal commit rules | `local_analysis_lifecycle.py` |
| deterministic restart decisions | `local_analysis_recovery.py` |
| remote capture serialization, task definitions, manifest restore | `local_remote_capture.py` |
| SmartPerfetto normalization, source-aware preparation, AI/report stages | `local_stage_execution.py` |
| slow/waiting activity reconciliation | `local_task_supervisor.py` |
| aggregate and team capability status | `local_analysis_health.py` |
| browser Analysis types and closed response parsing | `app/lib/perfpilot-analysis-api.ts` |

When changing an analysis flow, update its focused module and unit tests first,
then use `local_app.py` only to wire storage, gateways, services, and routes. The
focused modules must not import FastAPI or `local_app`, and the browser parser
must not import the HTTP client.

## Local checks

Start PostgreSQL and Redis, then copy the safe, AI-disabled defaults:

```bash
cp .env.example .env
uv sync --locked --all-packages
```

Run the same complete backend suite that CI runs:

```bash
PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres \
PERFPILOT_TEST_TENANT_ADMIN_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
PERFPILOT_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
PERFPILOT_REQUIRE_REDIS_TESTS=1 \
uv run --locked --package perfpilot-api pytest -p no:cacheprovider services/api/tests -q
```

Run lint and web checks:

```bash
uv run --locked --package perfpilot-api ruff check services/api/src services/api/tests
npm ci
npm run lint
npm test
```

Backend tests keep AI disabled or use local fakes. Never add a real provider token to `.env` or CI. See the [AI synthesis operations guide](docs/operations/ai-synthesis.md) before configuring a production worker.
