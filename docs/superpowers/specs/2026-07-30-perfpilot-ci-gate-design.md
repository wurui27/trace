# PerfPilot CI Gate Design

## Status

Approved for implementation on 2026-07-30. This design adds the first required
GitHub check to Pull Request #1. It does not publish images, deploy services, or
hold production credentials.

## Goal

Every pull request and every push to `main` must prove that the Web application,
API, PostgreSQL paths, Redis paths, migrations, lint rules, and pinned Android
Memory upstream contract still work. Branch protection will depend on one stable
`ci-gate` job instead of the names of individual test jobs.

## Alternatives

### One workflow with independent jobs — selected

A single `.github/workflows/ci.yml` runs Python quality, Python tests, and Web
tests in parallel. A final `ci-gate` job fails unless all three jobs succeed.
This keeps branch protection stable while allowing each test group to evolve.

### One sequential job

This is simpler, but a Web failure waits for every Python test and service to
finish. It also hides which toolchain failed.

### Separate workflow per toolchain

This gives each toolchain full autonomy, but branch protection must track several
workflow-specific check names. It adds no value while the repository has one
Web package and one Python package.

## Triggers and concurrency

The workflow runs for:

- every `pull_request`;
- every push to `main`;
- manual `workflow_dispatch` runs.

Concurrency uses the workflow name plus the pull-request head ref or Git ref.
New commits cancel obsolete runs for the same ref.

## Permissions and supply-chain controls

The workflow grants only `contents: read`. It never receives deployment
environments or write tokens. All third-party Actions use full commit SHAs:

- `actions/checkout@11d5960a326750d5838078e36cf38b85af677262` (`v4`);
- `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065` (`v5`);
- `actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020` (`v4`);
- `astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e`
  (`v6.8.0`).

Checkout steps set `persist-credentials: false`. The Android Memory contract job
checks out `Gracker/Android-App-Memory-Analysis` at the exact commit
`d5514972ced78c3faa7fc17589c1ea9231645056` into `.ci/android-memory`.

## Jobs

### `python-quality`

This job uses Python 3.12 and the locked uv workspace. It runs:

```bash
uv sync --locked --all-packages
uv run --locked --package perfpilot-api ruff check services/api/src services/api/tests
```

It has no service containers and should finish quickly.

### `python-tests`

This job starts:

- PostgreSQL 17 with an ephemeral `postgres/postgres` test account;
- Redis 8 Alpine with an isolated test database.

Both services define health checks. The job checks out the pinned Android Memory
repository, installs the locked Python workspace, and runs the complete API suite
with PostgreSQL, Redis, and upstream-contract skips forbidden:

```bash
env \
  PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PERFPILOT_TEST_TENANT_ADMIN_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
  PERFPILOT_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
  PERFPILOT_REQUIRE_REDIS_TESTS=1 \
  PERFPILOT_ANDROID_MEMORY_ROOT="$GITHUB_WORKSPACE/.ci/android-memory" \
  PYTHONDONTWRITEBYTECODE=1 \
  uv run --locked --package perfpilot-api pytest -p no:cacheprovider services/api/tests -q
```

The migration tests run inside this suite. A missing service or upstream checkout
must fail the job instead of becoming a skip.

### `web`

This job uses Node 22.13.0 and the npm cache. It runs:

```bash
npm ci
npm run lint
npm test
```

`npm test` already performs the production build through the SSR test script, so
the workflow does not run a duplicate build.

### `ci-gate`

This job uses `if: always()` and depends on all three jobs. It succeeds only when
every dependency reports `success`. The job executes a shell assertion and uses
no repository checkout.

## Workflow contract test

`services/api/tests/unit/test_ci_workflow.py` loads the workflow with
`yaml.BaseLoader` and proves:

- the trigger, permissions, concurrency, and stable job names exist;
- each Action is pinned to the approved full SHA;
- the Android upstream checkout uses the exact repository and commit;
- PostgreSQL and Redis are present;
- required test environment variables force integration coverage;
- Web and Python commands use locked installs;
- `ci-gate` depends on all required jobs and runs after failures.

The test must fail before `.github/workflows/ci.yml` exists.

## Failure behavior

No job uses `continue-on-error`. A test timeout, missing dependency, unavailable
service, contract mismatch, lint failure, or Web build failure blocks `ci-gate`.
Logs may contain test database coordinates because they are ephemeral; they must
not contain repository, user, or production credentials.

## Acceptance criteria

1. The workflow contract test passes locally.
2. Ruff, the complete API suite, Web lint, and Web tests pass locally.
3. GitHub runs `python-quality`, `python-tests`, `web`, and `ci-gate` on PR #1.
4. `ci-gate` succeeds on the exact pull-request head SHA.
5. PR #1 remains a draft until the gate is green.

## Deferred work

Image build, SBOM generation, vulnerability scanning, signing, release promotion,
live S3-compatible testing, and deployment belong to later workflows. Adding them
to this pull-request gate before their artifacts exist would create false checks.
