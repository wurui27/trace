# PerfPilot

PerfPilot analyzes Android performance evidence and publishes versioned reports.

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
