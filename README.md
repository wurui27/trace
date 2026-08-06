# PerfPilot

PerfPilot analyzes Android performance evidence and publishes versioned reports.

## Local report workspace

The local runtime keeps SmartPerfetto and PerfPilot decoupled. Start the
SmartPerfetto backend on `127.0.0.1:3001`, then configure an OpenAI-compatible
model for PerfPilot's three report passes:

```bash
export PERFPILOT_LOCAL_AI_BASE_URL="https://your-provider.example/v1/"
export PERFPILOT_LOCAL_AI_MODEL="your-model"
export PERFPILOT_LOCAL_AI_TOKEN="your-token"
export PERFPILOT_LOCAL_AI_PROVIDER_NAME="local-provider"
npm run dev:api
```

In a second terminal, start the web workspace:

```bash
cp .dev.vars.example .dev.vars
npm run dev
```

Open `http://localhost:3000`. Completed analyses expose an **打开完整报告**
link. The report page shows SmartPerfetto provenance, the three PerfPilot AI
rounds, evidence-backed findings, recommendations, retest steps, and known
limitations. Runtime state and every AI round are stored below
`.perfpilot/local-runtime`; provider tokens are never persisted there.

For a local device analysis, connect exactly one authorized Android device and
select it in the web page before uploading the APK. PerfPilot discovers `adb`
and `aapt2` from `ANDROID_SDK_ROOT` / `ANDROID_HOME`, the standard Android SDK
directory on macOS, Windows, or Linux, and then `PATH`. Use absolute
`PERFPILOT_LOCAL_ADB` and `PERFPILOT_LOCAL_AAPT2` overrides for a custom SDK.
The local runtime installs the APK, captures startup and scroll Perfetto traces,
and collects the Android memory evidence archive in the background.

The optional runtime overrides are `PERFPILOT_LOCAL_SMARTPERFETTO_URL`,
`PERFPILOT_LOCAL_DATA_DIR`, `PERFPILOT_LOCAL_API_ORIGIN`,
`PERFPILOT_LOCAL_ADB`, and `PERFPILOT_LOCAL_AAPT2`.

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
