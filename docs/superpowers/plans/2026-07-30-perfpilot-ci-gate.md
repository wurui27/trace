# PerfPilot Required CI Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a required, reproducible GitHub Actions gate that proves the Python API, real PostgreSQL/Redis integrations, pinned Android Memory upstream contract, and Web application all pass before PerfPilot changes can merge.

**Architecture:** One workflow owns four jobs. `python-quality`, `python-tests`, and `web` run independently; `ci-gate` depends on all three and exposes one stable required-check name. The test job uses real PostgreSQL and Redis services and checks out the exact Android Memory commit used by the engine lock. A unit contract test protects the workflow from silent weakening.

**Tech Stack:** GitHub Actions, Python 3.12, uv 0.11.32, PostgreSQL 17, Redis 8, Node.js 22.13.0, npm, pytest, Ruff.

---

## Fixed inputs

- `actions/checkout@11d5960a326750d5838078e36cf38b85af677262`
- `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065`
- `astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e`
- `actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020`
- Android Memory repository: `Gracker/Android-App-Memory-Analysis`
- Android Memory commit: `d5514972ced78c3faa7fc17589c1ea9231645056`
- Every `actions/checkout` step sets `persist-credentials: false`.

## File map

### New files

- `.github/workflows/ci.yml`: required platform checks.
- `services/api/tests/unit/test_ci_workflow.py`: executable workflow policy contract.

### Existing files

No production source file changes are allowed in this task.

## Task 1: Add the workflow contract test

**Files:**

- Create: `services/api/tests/unit/test_ci_workflow.py`

- [ ] **Step 1: Write the failing contract test**

Load `.github/workflows/ci.yml` with `yaml.BaseLoader` and assert:

1. Triggers are `pull_request`, `push` on `main`, and `workflow_dispatch`.
2. Top-level permissions are exactly `contents: read`.
3. Concurrency cancels obsolete runs.
4. Jobs are exactly `python-quality`, `python-tests`, `web`, and `ci-gate`.
5. Every reusable action is pinned to the fixed full commit SHA above.
6. Each prerequisite job starts with the platform checkout pinned to the fixed
   SHA and an exact `with: {persist-credentials: false}` mapping. Quality and
   Web have exactly one checkout; tests have exactly two, with the fixed Android
   upstream checkout second.
7. `python-quality` runs `uv sync --locked --all-packages` and Ruff through
   `uv run --locked`.
8. `python-tests` checks out the pinned Android repository into
   `.ci/android-memory`, starts PostgreSQL 17 and Redis 8 services, sets the
   PostgreSQL and Redis `PERFPILOT_REQUIRE_*` flags, configures
   `PERFPILOT_ANDROID_MEMORY_ROOT`, and runs the complete API suite.
9. `web` runs `npm ci`, `npm run lint`, and `npm test` from the repository root.
10. `ci-gate` uses `always()`, needs all three jobs, and fails unless every
    dependency succeeded.
11. No job or step sets `continue-on-error`, no job defines `defaults`, and no
    step overrides `shell`. The three prerequisite jobs and their steps have no
    `if`, while only the `ci-gate` job may use the exact
    `if: ${{ always() }}` expression and its step has no `if`.
12. No job defines `permissions`, so a job cannot override the exact top-level
    `contents: read` policy.
13. `python-quality`, `python-tests`, and `web` have no `needs` and therefore
    remain independent.
14. The workflow has no top-level `defaults`; Web runs from the repository root,
    and no Web step sets `working-directory`.
15. `.github/workflows` contains exactly one workflow YAML file: `ci.yml`.

The test must inspect the parsed YAML rather than searching raw text. It must
also deep-copy the valid workflow, inject each forbidden policy weakening, and
prove the policy helper rejects every mutation with `AssertionError`. A
synthetic second workflow path must likewise be rejected.

- [ ] **Step 2: Prove the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-ci-contract /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_ci_workflow.py -q
```

Expected: FAIL because `.github/workflows/ci.yml` does not exist.

## Task 2: Implement the required workflow

**Files:**

- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Add workflow policy and triggers**

Use this top-level contract:

```yaml
name: CI

on:
  pull_request: {}
  push:
    branches:
      - main
  workflow_dispatch: {}

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
```

- [ ] **Step 2: Add `python-quality`**

Use Ubuntu latest, Python 3.12, uv 0.11.32,
`uv sync --locked --all-packages`, and:

```bash
uv run --locked --package perfpilot-api ruff check services/api/src services/api/tests
```

- [ ] **Step 3: Add `python-tests` with real services**

Use PostgreSQL `17` and Redis `8-alpine` service containers with health checks.
Check out the platform first, then the pinned Android Memory repository into
`.ci/android-memory`. Both checkout steps set `persist-credentials: false`.

Set:

```yaml
PERFPILOT_TEST_POSTGRES_URL: postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres
PERFPILOT_TEST_TENANT_ADMIN_URL: postgresql://postgres:postgres@127.0.0.1:5432/postgres
PERFPILOT_TEST_REDIS_URL: redis://127.0.0.1:6379/15
PERFPILOT_REQUIRE_POSTGRES_TESTS: "1"
PERFPILOT_REQUIRE_REDIS_TESTS: "1"
PERFPILOT_ANDROID_MEMORY_ROOT: ${{ github.workspace }}/.ci/android-memory
PYTHONDONTWRITEBYTECODE: "1"
```

Install from the lock and run:

```bash
uv sync --locked --all-packages
uv run --locked --package perfpilot-api pytest -p no:cacheprovider services/api/tests -q
```

- [ ] **Step 4: Add `web`**

Use Node.js 22.13.0, npm cache keyed by the root `package-lock.json`, and run
from the repository root:

```bash
npm ci
npm run lint
npm test
```

- [ ] **Step 5: Add the stable aggregate gate**

`ci-gate` must depend on all three checks, execute under `if: ${{ always() }}`, and compare `needs.<job>.result` to `success` before exiting zero.

- [ ] **Step 6: Prove the green state**

Run the workflow contract test again. Expected: PASS.

Then run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-ci-contract /Users/ray/Library/Python/3.12/bin/uv run --offline --locked --package perfpilot-api ruff check services/api/tests/unit/test_ci_workflow.py
git diff --check
```

- [ ] **Step 7: Commit the gate**

```bash
git add .github/workflows/ci.yml services/api/tests/unit/test_ci_workflow.py
git commit -m "ci: add required platform checks"
```

## Task 3: Validate the gate locally and on GitHub

**Files:**

- Verify only; modify Task 1 or Task 2 files only if a validation failure exposes a defect.

- [ ] **Step 1: Run the complete API suite against real dependencies**

Confirm PostgreSQL and Redis are reachable, sync the complete locked workspace,
then run the suite with the PostgreSQL and Redis skip guards forced and the
Android Memory root configured:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-ci-full /Users/ray/Library/Python/3.12/bin/uv sync --offline --locked --all-packages
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres PERFPILOT_TEST_TENANT_ADMIN_URL=postgresql://ray@127.0.0.1:55441/postgres PERFPILOT_TEST_REDIS_URL=redis://127.0.0.1:6379/15 PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PERFPILOT_REQUIRE_REDIS_TESTS=1 PERFPILOT_ANDROID_MEMORY_ROOT=/Users/ray/Android-App-Memory-Analysis PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-ci-full /Users/ray/Library/Python/3.12/bin/uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider services/api/tests -q
```

Expected: all tests PASS with no PostgreSQL, Redis, or Android upstream skip.

- [ ] **Step 2: Run complete quality and Web checks**

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-ci-full /Users/ray/Library/Python/3.12/bin/uv sync --offline --locked --all-packages
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-ci-full /Users/ray/Library/Python/3.12/bin/uv run --offline --locked --package perfpilot-api ruff check services/api/src services/api/tests
npm run lint
npm test
git diff --check
git status --short
```

Expected: all checks PASS and only the intended commits are present.

- [ ] **Step 3: Push and observe GitHub Actions**

```bash
git push origin HEAD
gh pr checks 1 --watch
```

If a check fails, inspect its log, reproduce the root cause locally where possible, add a failing regression test, fix it, commit, push, and watch again.

- [ ] **Step 4: Complete the pull request**

When every check is green:

1. Mark PR #1 ready for review.
2. Confirm the PR still targets `main` and contains only the intended commits.
3. Merge with a merge commit while preserving the feature branch.
4. Fetch `origin/main` and verify the merge commit is reachable.

Do not delete the linked worktree or feature branch because the next canonical-result phase begins immediately after verification.

## Definition of done

- The workflow contract test prevents removal or weakening of required checks.
- API tests exercise real PostgreSQL, Redis, and pinned Android Memory code in CI.
- Web lint, test, and production build pass in CI.
- `ci-gate` is the single stable required-check name.
- PR #1 is green, reviewed, and merged into `main`.
- Local and remote branch state is recorded before the canonical-result phase starts.
