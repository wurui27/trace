# PerfPilot Phase 1 Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-shaped FastAPI control plane that authenticates users, isolates every team, finalizes immutable uploads, orchestrates device work, and reliably publishes schedule, validation, and analysis events.

**Architecture:** A PostgreSQL control database is authoritative for accounts, tenant resource mappings, global job state, leases, claims, idempotency, outbox/inbox records, and audit events. A `TenantRouter` opens bounded pools to a separate PostgreSQL database per team, while an S3 adapter exposes only artifact IDs and short-lived signed URLs. Redis Streams carries opaque event envelopes; every event is recoverable from the control outbox.

**Tech Stack:** Python 3.12, uv workspace, FastAPI 0.139.x, Pydantic 2.13.x, SQLAlchemy 2.0.x, Alembic 1.18.x, psycopg 3.3.x, redis-py 8.0.x, boto3 1.43.x, PostgreSQL 17, Redis 8, MinIO, pytest.

---

## File map

- Create `pyproject.toml`: uv workspace and shared development dependencies.
- Create `services/api/pyproject.toml`: control-plane runtime dependencies and CLI entry points.
- Create `services/api/src/perfpilot_api/main.py`: FastAPI application factory.
- Create `services/api/src/perfpilot_api/config.py`: validated environment settings.
- Create `services/api/src/perfpilot_api/errors.py`: stable error envelope and handlers.
- Create `services/api/src/perfpilot_api/security/`: password, session, CSRF, proxy-signature, and Agent-token primitives.
- Create `services/api/src/perfpilot_api/db/control/`: control database engine, models, repositories, and unit of work.
- Create `services/api/src/perfpilot_api/db/tenant/`: tenant database models, router, repositories, and unit of work.
- Create `services/api/src/perfpilot_api/migrations.py`: migration runner used by CLI and Provisioner.
- Create `services/api/src/perfpilot_api/services/`: auth, tenancy, upload, analysis, lease, outbox, delete, and retention use cases.
- Create `services/api/src/perfpilot_api/api/`: Web, admin, and Agent route modules.
- Create `services/api/src/perfpilot_api/workers/`: Provisioner, dispatcher, scheduler, and reconciler entry points.
- Create `services/api/migrations/control/`: Alembic environment and control revisions.
- Create `services/api/migrations/tenant/`: Alembic environment and tenant revisions.
- Create `services/api/tests/conftest.py`: deterministic IDs, authenticated clients, infrastructure doubles, and inspectors used by all API tests.
- Create `services/api/tests/{unit,contract,integration}/`: focused and infrastructure-backed tests.
- Create `contracts/v1/`: JSON Schema authority and valid/invalid examples.
- Create `infra/compose.yaml`: PostgreSQL, Redis, MinIO, API, and control workers.
- Create `.github/workflows/platform-ci.yml`: locked dependency, test, migration, and build checks.

## Task 1: Create the Python workspace and contract harness

**Files:**
- Create: `pyproject.toml`
- Create: `services/api/pyproject.toml`
- Create: `services/api/src/perfpilot_api/__init__.py`
- Create: `services/api/tests/conftest.py`
- Create: `services/api/tests/contract/test_contract_examples.py`
- Create: `contracts/v1/common/error.schema.json`
- Create: `contracts/v1/events/event-envelope.schema.json`
- Create: `contracts/v1/examples/error.invalid.json`
- Create: `contracts/v1/examples/event-envelope.valid.json`
- Create: `uv.lock`

- [ ] **Step 1: Create the workspace metadata**

```toml
# pyproject.toml
[tool.uv.workspace]
members = ["services/api"]

[dependency-groups]
dev = [
  "httpx>=0.28,<0.29",
  "jsonschema>=4.25,<5",
  "pytest>=9,<10",
  "pytest-asyncio>=1.3,<2",
  "pytest-cov>=7,<8",
  "ruff>=0.14,<0.15",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["services/api/tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

```toml
# services/api/pyproject.toml
[project]
name = "perfpilot-api"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "alembic>=1.18.5,<1.19",
  "argon2-cffi>=25.1,<26",
  "boto3>=1.43.51,<1.44",
  "cryptography>=46,<47",
  "fastapi[standard]>=0.139.2,<0.140",
  "psycopg[binary]>=3.3.4,<3.4",
  "pydantic>=2.13.4,<2.14",
  "pydantic-settings>=2.12,<3",
  "redis[hiredis]>=8.0.1,<8.1",
  "sqlalchemy[asyncio]>=2.0.51,<2.1",
]

[project.scripts]
perfpilot-api = "perfpilot_api.main:run"
perfpilot-admin = "perfpilot_api.cli:main"
perfpilot-provisioner = "perfpilot_api.workers.provisioner:main"
perfpilot-dispatcher = "perfpilot_api.workers.dispatcher:main"
perfpilot-scheduler = "perfpilot_api.workers.scheduler:main"
perfpilot-reconciler = "perfpilot_api.workers.reconciler:main"

[build-system]
requires = ["uv_build>=0.11.25,<0.12"]
build-backend = "uv_build"
```

- [ ] **Step 2: Lock and install the workspace**

Run:

```bash
python3 -m pip install --user "uv>=0.11.25,<0.12"
uv lock
uv sync --locked --all-packages --dev
uv run --package perfpilot-api python -c \
  "import importlib.metadata as m; assert m.version('perfpilot-api') == '0.1.0'"
```

Expected: `uv.lock` is created, synchronization exits `0`, and the installed workspace distribution reports version `0.1.0`.

- [ ] **Step 3: Write the failing contract-example test**

```python
# services/api/tests/contract/test_contract_examples.py
import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parents[4]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_event_envelope_example_matches_schema() -> None:
    schema = load("contracts/v1/events/event-envelope.schema.json")
    payload = load("contracts/v1/examples/event-envelope.valid.json")
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_error_example_rejects_missing_request_id() -> None:
    schema = load("contracts/v1/common/error.schema.json")
    payload = load("contracts/v1/examples/error.invalid.json")
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(payload))
    assert [error.validator for error in errors] == ["required"]
```

- [ ] **Step 4: Run the contract test and verify RED**

Run:

```bash
uv run --package perfpilot-api pytest services/api/tests/contract/test_contract_examples.py -q
```

Expected: FAIL because `contracts/v1/events/event-envelope.schema.json` does not exist.

- [ ] **Step 5: Add the two schemas and examples**

```json
// contracts/v1/events/event-envelope.schema.json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://perfpilot.internal/contracts/v1/events/event-envelope.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "event_id", "event_type", "subject_type", "subject_id"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "event_id": {"type": "string", "format": "uuid"},
    "event_type": {
      "enum": ["analysis_queued", "sample_validation_requested", "analysis_requested"]
    },
    "subject_type": {"enum": ["analysis", "scenario_job", "sample_attempt"]},
    "subject_id": {"type": "string", "format": "uuid"}
  }
}
```

```json
// contracts/v1/common/error.schema.json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "error"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "error": {
      "type": "object",
      "additionalProperties": false,
      "required": ["code", "message", "retryable", "request_id"],
      "properties": {
        "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]+$"},
        "message": {"type": "string", "minLength": 1},
        "retryable": {"type": "boolean"},
        "request_id": {"type": "string", "minLength": 1}
      }
    }
  }
}
```

Use these exact examples:

```json
// contracts/v1/examples/event-envelope.valid.json
{
  "schema_version": "1.0",
  "event_id": "10000000-0000-4000-8000-000000000001",
  "event_type": "analysis_queued",
  "subject_type": "analysis",
  "subject_id": "20000000-0000-4000-8000-000000000001"
}
```

```json
// contracts/v1/examples/error.invalid.json
{
  "schema_version": "1.0",
  "error": {
    "code": "example_failure",
    "message": "用于验证缺少 request_id 的无效载荷",
    "retryable": false
  }
}
```

- [ ] **Step 6: Define the shared test fixture contract**

Create `services/api/tests/conftest.py` before any later task uses its fixtures. It owns fixed UUID constants for two users, two teams, two analyses, one Agent, one device, one sample, and one event. It provides:

- `api_client`, `agent_client`, `internal_worker_client`, and `contract_validator`: `TestClient` instances with separate browser-proxy, Agent, and private Worker-service authentication helpers plus a Draft 2020-12 schema/example loader;
- `admin_user`, `team_member_session`, `team_id`, `team_a`, and `team_b`: deterministic identities whose plaintext test passwords exist only in memory;
- `control_inspector` and `tenant_inspector`: SQLAlchemy inspectors for independently migrated disposable PostgreSQL databases;
- `provisioner`, `fake_postgres`, `failing_bucket_admin`, `tenant_router`, `control_resources`, `encrypted_secret_store`, and `secret_store_inspector`: explicit saga, routing, and ciphertext-store doubles;
- `upload_service` and `s3_store`: an in-memory metadata repository plus a checksum/version-aware object-store fake;
- `registration_code`, `active_lease`, `revoked_lease`, and `finalized_sample_uploads`: single-use registration, current/expired lease records, and a finalized immutable sample manifest;
- `dispatcher`, `redis_client`, `outbox_repo`, `consumer`, `analysis_event`, `expired_claim`, and `inbox_repo`: deterministic delivery/claim fakes;
- `api`, `running_analysis`, and `audit_repo`: lifecycle client and redacting repository fixtures.

Define `TEAM_ID`, `IDEMPOTENCY_KEY`, `SAMPLE_ID`, `create_test_slot`, and other one-test helper factories at the top of the test module that first uses them. No later test may depend on an undeclared global, wall-clock time, network service, or random UUID.

Keep production imports inside the fixture functions that need them, and avoid annotations that evaluate an unavailable production type. Importing `conftest.py` during Task 1 must succeed even though later repositories and services do not exist yet.

- [ ] **Step 7: Run GREEN and commit**

Run:

```bash
uv run --package perfpilot-api pytest services/api/tests/contract/test_contract_examples.py -q
uv run ruff check services/api
git add pyproject.toml uv.lock services/api contracts/v1
git commit -m "build: scaffold PerfPilot control plane"
```

Expected: 2 tests pass and the commit contains no application behavior.

## Task 2: Add settings, the app factory, request IDs, and stable errors

**Files:**
- Create: `services/api/src/perfpilot_api/config.py`
- Create: `services/api/src/perfpilot_api/errors.py`
- Create: `services/api/src/perfpilot_api/main.py`
- Create: `services/api/src/perfpilot_api/api/health.py`
- Create: `services/api/tests/unit/test_app.py`

- [ ] **Step 1: Write failing app tests**

```python
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app


def test_health_returns_request_id() -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get("/v1/health", headers={"x-request-id": "req-health"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"] == "req-health"


def test_unknown_route_uses_stable_error_shape() -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get("/v1/missing", headers={"x-request-id": "req-404"})
    assert response.status_code == 404
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "route_not_found",
            "message": "请求的接口不存在",
            "retryable": False,
            "request_id": "req-404",
        },
    }


def test_production_rejects_development_secrets() -> None:
    with pytest.raises(ValidationError, match="production secret"):
        Settings(app_env="production")
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run --package perfpilot-api pytest services/api/tests/unit/test_app.py -q
```

Expected: collection fails because `perfpilot_api.main` does not exist.

- [ ] **Step 3: Implement the settings and application boundary**

```python
# services/api/src/perfpilot_api/config.py
from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PERFPILOT_", env_file=".env")

    app_env: Literal["development", "test", "production"] = "development"
    control_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://perfpilot:perfpilot@127.0.0.1:5432/perfpilot_control"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    s3_endpoint_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:9000")
    proxy_secret: SecretStr = SecretStr("development-only-proxy-secret")
    session_secret: SecretStr = SecretStr("development-only-session-secret")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`Settings` must reject every development default in production, require explicit proxy/session/JWS/Agent-registration secret references, require TLS origins, and refuse a loopback database/object endpoint. `errors.py` must define `ApiError(code, message, status_code, retryable)` and handlers for `ApiError`, `RequestValidationError`, and `StarletteHTTPException`. `main.py` must use a FastAPI lifespan context, install request-ID middleware, register the handlers, and include the `/v1/health` router. In test mode the lifespan must not open external connections.

- [ ] **Step 4: Run GREEN**

Run:

```bash
uv run --package perfpilot-api pytest services/api/tests/unit/test_app.py -q
uv run ruff check services/api/src/perfpilot_api
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/perfpilot_api services/api/tests/unit/test_app.py
git commit -m "feat: add control API application boundary"
```

## Task 3: Create control and tenant database schemas

**Files:**
- Create: `services/api/src/perfpilot_api/db/base.py`
- Create: `services/api/src/perfpilot_api/db/control/models/{auth,tenancy,jobs,agents,events}.py`
- Create: `services/api/src/perfpilot_api/db/control/session.py`
- Create: `services/api/src/perfpilot_api/db/tenant/models/{apps,artifacts,reports}.py`
- Create: `services/api/src/perfpilot_api/db/tenant/session.py`
- Create: `services/api/migrations/control/alembic.ini`
- Create: `services/api/migrations/control/env.py`
- Create: `services/api/migrations/control/versions/0001_control_schema.py`
- Create: `services/api/migrations/tenant/alembic.ini`
- Create: `services/api/migrations/tenant/env.py`
- Create: `services/api/migrations/tenant/versions/0001_tenant_schema.py`
- Create: `services/api/tests/integration/test_migrations.py`

- [ ] **Step 1: Write migration inventory tests**

```python
CONTROL_TABLES = {
    "users", "teams", "memberships", "tenant_resources", "agents", "devices",
    "global_jobs", "scenario_jobs", "agent_leases",
    "sample_validation_claims", "worker_claims", "outbox_events", "inbox_events",
    "idempotency_keys", "sessions", "tenant_quotas", "audit_events",
}
TENANT_TABLES = {
    "applications", "application_versions", "scenario_recipes", "analyses",
    "scenario_results", "sample_attempts", "artifacts", "report_versions", "metrics", "findings",
    "evidence", "recommendations",
}


def test_control_migration_creates_only_control_tables(control_inspector) -> None:
    assert set(control_inspector.get_table_names()) == CONTROL_TABLES | {"alembic_version"}


def test_tenant_migration_creates_only_tenant_tables(tenant_inspector) -> None:
    assert set(tenant_inspector.get_table_names()) == TENANT_TABLES | {"alembic_version"}
```

The integration fixture must create two empty PostgreSQL databases, run each Alembic tree against the correct URL, and return SQLAlchemy inspectors.

- [ ] **Step 2: Run RED**

Run:

```bash
PERFPILOT_TEST_POSTGRES_URL="postgresql+psycopg://perfpilot:perfpilot@127.0.0.1:5432/postgres" \
uv run --package perfpilot-api pytest services/api/tests/integration/test_migrations.py -q
```

Expected: FAIL because both Alembic environments are absent.

- [ ] **Step 3: Implement the mapped table inventory**

Use a UUID primary key, timezone-aware `created_at`/`updated_at`, and optimistic integer `version` on mutable orchestration rows. Enforce these database constraints:

```text
users.username                                  UNIQUE
memberships(team_id, user_id)                  UNIQUE
tenant_resources(team_id, resource_version)    UNIQUE
global_jobs(team_id, idempotency_key)           UNIQUE
scenario_jobs(analysis_id, scenario_type)       UNIQUE
devices.serial                                  UNIQUE
agent_leases(device_id) WHERE state='active'    UNIQUE partial index
inbox_events(consumer_name, event_id)           UNIQUE
report_versions(analysis_id, report_version)    UNIQUE
artifacts(object_key, version_id)                UNIQUE
sample_attempts(scenario_job_id, attempt_no)     UNIQUE in tenant database
```

`global_jobs` and `scenario_jobs` may store team IDs, opaque artifact IDs, state, timestamps, ABI, minimum API, valid/attempt counts, and retry metadata. They must not contain package names, customer filenames, object keys, database URLs, metrics, evidence, or individual sample content. Individual sample state, artifact metadata, and validator verdict live only in the owning team database.

`outbox_events.ready_at` is nullable. Ordinary single-database events set it in their creating transaction. The sample-finalize cross-database saga creates an unready row and sets `ready_at` only after the tenant sample commit; the dispatcher ignores unready rows.

- [ ] **Step 4: Add reversible Alembic revisions**

Both `upgrade()` functions create exactly the tested inventory. Both `downgrade()` functions drop foreign-key dependents before parents. Run:

```bash
uv run alembic -c services/api/migrations/control/alembic.ini upgrade head
uv run alembic -c services/api/migrations/control/alembic.ini downgrade base
uv run alembic -c services/api/migrations/control/alembic.ini upgrade head
uv run alembic -c services/api/migrations/tenant/alembic.ini upgrade head
uv run alembic -c services/api/migrations/tenant/alembic.ini downgrade base
uv run alembic -c services/api/migrations/tenant/alembic.ini upgrade head
```

Expected: all six migration commands exit `0`.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest services/api/tests/integration/test_migrations.py -q
git add services/api/src/perfpilot_api/db services/api/migrations services/api/tests/integration/test_migrations.py
git commit -m "feat: add isolated control and tenant schemas"
```

## Task 4: Implement bootstrap admin, sessions, CSRF, and RBAC

**Files:**
- Create: `services/api/src/perfpilot_api/security/{passwords,sessions,csrf,proxy_signature}.py`
- Create: `services/api/src/perfpilot_api/services/auth.py`
- Create: `services/api/src/perfpilot_api/api/{auth,me,members}.py`
- Create: `services/api/src/perfpilot_api/cli.py`
- Create: `services/api/tests/unit/test_security.py`
- Create: `services/api/tests/integration/test_auth_api.py`
- Create: `contracts/v1/auth/{login-request,session-response}.schema.json`

- [ ] **Step 1: Write failing security tests**

```python
def test_login_rotates_pre_auth_session_and_csrf(api_client, admin_user) -> None:
    csrf = api_client.get("/v1/auth/csrf")
    pre_cookie = csrf.cookies["perfpilot_session"]
    login = api_client.post(
        "/v1/auth/login",
        json={"username": admin_user.username, "password": admin_user.password},
        headers={"x-csrf-token": csrf.json()["csrf_token"], "origin": "https://app.example"},
    )
    assert login.status_code == 200
    assert login.cookies["perfpilot_session"] != pre_cookie
    assert login.json()["csrf_token"] != csrf.json()["csrf_token"]


def test_team_member_cannot_change_members(api_client, team_member_session, team_id) -> None:
    response = api_client.post(
        f"/v1/teams/{team_id}/members",
        json={"user_id": "11111111-1111-4111-8111-111111111111", "role": "team_viewer"},
        headers=team_member_session.state_headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_forbidden"
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_security.py \
  services/api/tests/integration/test_auth_api.py -q
```

Expected: FAIL because the auth router and password/session primitives are absent.

- [ ] **Step 3: Implement the security contract**

Use `argon2.PasswordHasher()` for password hashes. Store only a SHA-256 digest of a random 32-byte session token. A pre-auth session expires after 10 minutes; an authenticated session has a 12-hour idle timeout and 7-day absolute timeout. A session row carries `user_id`, `kind` (`pre_auth` or `authenticated`), CSRF secret, last-seen time, absolute expiry, and revocation time.

```python
COOKIE_NAME = "perfpilot_session"


def set_session_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
```

The proxy signature is `base64url(HMAC-SHA256(secret, timestamp + "\n" + request_id + "\n" + method + "\n" + path_and_query + "\n" + body_sha256))`. `path_and_query` is the ASGI raw path plus the unmodified raw query string, without reordering or decoding. Reject signatures older than 60 seconds and duplicate request IDs. State-changing Web routes require the authenticated CSRF token and an allowed `Origin`.

Rate-limit login by normalized username and server-owned client address: five failures in 15 minutes returns `429 login_rate_limited`; success clears the username bucket. Login errors do not reveal whether an account exists. Logout revokes the session digest, expires the host-only cookie, and rotates the CSRF secret.

Require the proxy signature on browser `/v1/auth`, `/v1/me`, `/v1/admin`, and `/v1/teams` routes. `/v1/agent` uses Agent authentication instead, `/internal/v1/worker` uses Worker service plus claim tokens, and readiness is exposed only on the private listener. A credential from one identity class is never accepted by another router.

- [ ] **Step 4: Add the one-time admin CLI**

```bash
PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD='a-development-secret-from-the-shell' \
uv run perfpilot-admin create-user --username ray_wu --role platform_admin
```

The command reads the password only from the environment, refuses production passwords shorter than 12 characters or equal to the username, hashes it, prints only the created user ID, and exits non-zero if the username already exists without `--idempotent`.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_security.py \
  services/api/tests/integration/test_auth_api.py -q
git add services/api/src/perfpilot_api services/api/tests contracts/v1/auth
git commit -m "feat: add session authentication and team roles"
```

## Task 5: Implement tenant provisioning and bounded routing

**Files:**
- Create: `services/api/src/perfpilot_api/services/provisioning.py`
- Create: `services/api/src/perfpilot_api/db/tenant/router.py`
- Create: `services/api/src/perfpilot_api/secrets/{base,encrypted_file}.py`
- Create: `services/api/src/perfpilot_api/workers/provisioner.py`
- Create: `services/api/src/perfpilot_api/api/admin_teams.py`
- Create: `services/api/tests/unit/test_tenant_router.py`
- Create: `services/api/tests/unit/test_secret_store.py`
- Create: `services/api/tests/integration/test_provisioning.py`

- [ ] **Step 1: Write failing saga and isolation tests**

```python
async def test_provisioning_compensates_database_when_bucket_fails(
    provisioner, fake_postgres, failing_bucket_admin
) -> None:
    result = await provisioner.provision(TEAM_ID, IDEMPOTENCY_KEY)
    assert result.state == "cleanup_pending"
    assert fake_postgres.deleted_databases == [result.database_name]


async def test_router_ignores_caller_supplied_database(
    tenant_router, control_resources, team_a, team_b
) -> None:
    async with tenant_router.session(team_a.id) as session:
        assert session.info["team_id"] == team_a.id
    with pytest.raises(TenantRouteError):
        await tenant_router.session_for_untrusted_value(team_b.database_url)


async def test_secret_store_never_writes_plaintext(
    encrypted_secret_store, secret_store_inspector
) -> None:
    reference = await encrypted_secret_store.put(
        "tenant-db", b"postgresql-password-test-value"
    )
    assert reference.startswith("secret://")
    assert b"postgresql-password-test-value" not in secret_store_inspector.all_bytes()
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_tenant_router.py \
  services/api/tests/unit/test_secret_store.py \
  services/api/tests/integration/test_provisioning.py -q
```

Expected: imports fail for `TenantRouter` and `Provisioner`.

- [ ] **Step 3: Implement resource lifecycle and compensation**

`tenant_resources` transitions only through:

```text
requested → provisioning → active
requested → provisioning → cleanup_pending → requested
active → migrating → active
```

Provision in this exact order: database, least-privilege role, encrypted secret version, tenant migration, versioned bucket, active mapping. `EncryptedFileSecretStore` receives a 32-byte AES-GCM master key from an owner-only mounted secret file, writes ciphertext and nonce atomically under an owner-only secret-store mount, and returns an opaque reference; the control database never receives tenant credentials or the master key. Tests use an in-memory implementation of the same `SecretStore` interface.

The bucket is unique to one team, blocks public access, enables versioning and server-side encryption, and applies the raw-artifact lifecycle policy. Its CORS policy allows only the configured Sites origin, `PUT` and `HEAD`, and the exact signed content-type/checksum headers; it never allows `*` origins or credentials. On failure, delete in reverse order. Persist each completed step before continuing so the reconciler can retry compensation.

- [ ] **Step 4: Implement bounded `TenantRouter` pools**

```python
@asynccontextmanager
async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
    resource = await self._control_resources.active_for_team(team_id)
    entry = await self._pools.get_or_create(resource)
    async with entry.sessionmaker.begin() as session:
        session.info["team_id"] = team_id
        yield session
```

Key pools by `(team_id, resource_version)`, cap each pool and the global pool count, close idle pools, and dispose the prior version immediately after a resource switch. A missing or unavailable tenant store maps to `503 tenant_store_unavailable`.

Credential rotation creates a new database role and secret version, verifies a new pool, atomically switches `tenant_resources.resource_version`, closes the old pool, and only then revokes the old role. Secret-store master-key rotation decrypts with the old key and atomically rewrites with the new key ID; interrupted rewrites retain the last valid ciphertext.

`POST /v1/admin/teams` requires an existing `owner_user_id`. Provisioning success creates that explicit `team_owner` membership; a platform administrator does not gain report access merely by creating or administering the team. The development administrator may select their own user ID as the first acceptance team owner, and that role assignment is audited.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_tenant_router.py \
  services/api/tests/unit/test_secret_store.py \
  services/api/tests/integration/test_provisioning.py -q
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: provision isolated team resources"
```

## Task 6: Implement immutable artifact slots and signed URLs

**Files:**
- Create: `services/api/src/perfpilot_api/storage/{base,s3}.py`
- Create: `services/api/src/perfpilot_api/services/uploads.py`
- Create: `services/api/src/perfpilot_api/api/uploads.py`
- Create: `services/api/tests/unit/test_upload_service.py`
- Create: `services/api/tests/integration/test_s3_uploads.py`
- Create: `contracts/v1/artifacts/{slot-request,slot-response,finalize-request}.schema.json`

- [ ] **Step 1: Write failing finalize tests**

```python
async def test_finalize_uses_saved_slot_and_object_metadata(upload_service, s3_store) -> None:
    slot = await upload_service.create_slot(
        team_id=TEAM_ID,
        artifact_kind="apk",
        mime="application/vnd.android.package-archive",
        size=4,
        sha256_b64="iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E=",
    )
    await s3_store.put_for_test(slot.object_key, b"test", slot.required_headers)
    artifact = await upload_service.finalize(
        slot.upload_id, slot.sha256_b64, slot.size
    )
    assert artifact.version_id


async def test_finalize_rejects_changed_checksum(upload_service, s3_store) -> None:
    slot = await create_test_slot(upload_service)
    await s3_store.put_for_test(slot.object_key, b"evil", slot.required_headers)
    with pytest.raises(UploadMismatchError):
        await upload_service.finalize(slot.upload_id, slot.sha256_b64, slot.size)
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_upload_service.py \
  services/api/tests/integration/test_s3_uploads.py -q
```

Expected: FAIL because `S3ArtifactStore` and `UploadService` are absent.

- [ ] **Step 3: Implement slot creation and finalize**

The create call saves expected MIME, bytes, SHA-256, random `upload_id`, team, never-reused object key, and expiry before returning a presigned PUT. Sign `Content-Type` and `x-amz-checksum-sha256`. Finalize performs `head_object` or `get_object_attributes`, compares stored expectations, caller repetition, and S3 metadata, then CAS-saves `version_id`.

```python
if (
    caller_size != slot.expected_size
    or caller_sha256 != slot.expected_sha256
    or metadata.size != slot.expected_size
    or metadata.sha256_b64 != slot.expected_sha256
    or metadata.content_type != slot.expected_mime
):
    raise UploadMismatchError()
```

`POST .../uploads` is idempotent for the same analysis, artifact kind, MIME, bytes, and checksum. It returns the existing live unfinalized authorization; after expiry it invalidates that slot and creates a new `upload_id` and never-reused object key. A finalized slot returns immutable artifact metadata, not another PUT URL. Reusing the same idempotency key with different file metadata returns `409 idempotency_conflict`.

- [ ] **Step 4: Add membership-bound downloads**

`POST /v1/teams/{team_id}/analyses/{analysis_id}/artifacts/{artifact_id}/download` must verify membership, task ownership, artifact ownership, and tombstone state before returning a five-minute version-bound GET URL. Set attachment disposition and `nosniff`.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_upload_service.py \
  services/api/tests/integration/test_s3_uploads.py -q
git add services/api/src/perfpilot_api services/api/tests contracts/v1/artifacts
git commit -m "feat: add immutable artifact uploads"
```

## Task 7: Implement analysis creation, idempotency, and state machines

**Files:**
- Create: `services/api/src/perfpilot_api/domain/{states,transitions}.py`
- Create: `services/api/src/perfpilot_api/services/analyses.py`
- Create: `services/api/src/perfpilot_api/api/analyses.py`
- Create: `services/api/tests/unit/test_state_machines.py`
- Create: `services/api/tests/integration/test_analysis_api.py`
- Create: `contracts/v1/analyses/{create-request,analysis-response,scenario-execution-manifest}.schema.json`
- Create: `contracts/v1/reports/{analysis-bundle,analysis-report}.schema.json`
- Create: `contracts/v1/examples/analysis-report.partial.valid.json`

- [ ] **Step 1: Write failing transition tests**

```python
@pytest.mark.parametrize(
    ("children", "expected"),
    [
        (["queued", "queued", "queued"], "queued"),
        (["scheduled", "queued", "queued"], "scheduled"),
        (["completed", "scheduled", "queued"], "running"),
        (["completed", "analyzing", "completed"], "analyzing"),
        (["completed", "failed", "completed"], "partially_completed"),
    ],
)
def test_parent_state_is_derived(children: list[str], expected: str) -> None:
    assert derive_parent_state(children) == expected


def test_terminal_state_cannot_move_back() -> None:
    with pytest.raises(InvalidTransition):
        transition("completed", "running")


def test_partial_report_contract_keeps_successful_siblings(contract_validator) -> None:
    report = contract_validator.valid_example("analysis-report.partial.valid.json")
    contract_validator.validate("reports/analysis-report.schema.json", report)
    assert [item["result_state"] for item in report["scenario_reports"]] == [
        "completed", "failed", "completed"
    ]
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_state_machines.py \
  services/api/tests/integration/test_analysis_api.py -q
```

Expected: FAIL because the domain state module is absent.

- [ ] **Step 3: Implement device analysis creation**

`POST /v1/teams/{team_id}/analyses` requires `Idempotency-Key`. For `analysis_mode=device`, create a parent in internal `creating`, create the team record, create an APK slot, then expose `created`. Finalizing the APK creates exactly `cold_start`, `scroll`, and `memory_cycle` children plus `analysis_queued` in the same control transaction.

Hash the canonical request body and enforce:

```text
same team + same key + same request hash      → existing response
same team + same key + different request hash → 409 idempotency_conflict
```

- [ ] **Step 4: Implement CAS transitions and aggregate responses**

Every transition SQL includes `WHERE id=:id AND version=:expected_version AND state IN (...)`. Return `409 stale_task_version` on zero updated rows. The GET response includes the parent, all children, sample verdict counts, active lease summary, and `report_available`.

Publish two separate immutable contracts:

- `AnalysisBundle v1` is one scenario result and contains scenario type, validity, metrics, findings, evidence, artifact references, trace health/capabilities, and provenance.
- `AnalysisReport v1` is the parent response and contains analysis ID/mode/state, report version/time, and an ordered `scenario_reports` array. Each entry has scenario type, terminal result state, `device_group_id` or an explicit not-applicable reason, either one bundle or a stable failure summary, and never hides successful siblings.

The valid partial example contains `cold_start=completed` with a bundle, `scroll=failed` with a stable failure plus any partial bundle, and `memory_cycle=completed` with a bundle. JSON Schema enforces that a completed entry has a bundle and that a failed/canceled entry has either a failure or a partial bundle.

The same membership and task-to-team check guards `GET .../report`. Route through `TenantRouter` and, only after the parent is terminal, assemble the immutable `AnalysisReport v1` from the latest non-overwritten scenario report versions. Keep `report_available=false` and return `404 report_not_available` while the parent is non-terminal, even if an internal scenario bundle already exists. Never return a tenant database URL, bucket, object key, or presigned URL in either response. Enforce the team’s queued-parent quota before creating an analysis; a rejected create returns `429 team_queue_limit` without allocating an upload slot.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_state_machines.py \
  services/api/tests/integration/test_analysis_api.py -q
git add services/api/src/perfpilot_api services/api/tests contracts/v1/analyses contracts/v1/reports contracts/v1/examples/analysis-report.partial.valid.json
git commit -m "feat: add device analysis orchestration"
```

## Task 8: Implement Agent administration, devices, and leases

**Files:**
- Create: `services/api/src/perfpilot_api/security/agent_tokens.py`
- Create: `services/api/src/perfpilot_api/services/{agents,leases}.py`
- Create: `services/api/src/perfpilot_api/api/{admin_agents,agent}.py`
- Create: `services/api/tests/integration/test_agent_api.py`
- Create: `contracts/v1/agent/{register,heartbeat,task-snapshot,task-event,sample-attempt-manifest}.schema.json`

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_registration_code_is_single_use(api_client, registration_code) -> None:
    first = api_client.post("/v1/agent/register", json={"code": registration_code})
    second = api_client.post("/v1/agent/register", json={"code": registration_code})
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "registration_code_used"


def test_old_lease_token_cannot_append_event(agent_client, revoked_lease) -> None:
    response = agent_client.post(
        f"/v1/agent/tasks/{revoked_lease.scenario_job_id}/events",
        json={"event": "capture_started", "task_version": revoked_lease.task_version},
        headers=revoked_lease.headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lease_not_active"


def test_sample_finalize_makes_outbox_ready_only_after_tenant_commit(
    agent_client, active_lease, finalized_sample_uploads, outbox_repo
) -> None:
    response = agent_client.post(
        f"/v1/agent/tasks/{active_lease.scenario_job_id}/samples",
        json=finalized_sample_uploads.manifest,
        headers=active_lease.headers,
    )
    assert response.status_code == 201
    event = outbox_repo.for_subject(response.json()["sample_id"])
    assert event.event_type == "sample_validation_requested"
    assert event.ready_at is not None
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest services/api/tests/integration/test_agent_api.py -q
```

Expected: Agent routes return `404`.

- [ ] **Step 3: Implement registration and device inventory**

Registration codes are random, single-use, hashed, scoped, and expire. Agent tokens are random and hashed with a stored version. Heartbeat upserts only devices owned by that Agent and records API level, ABI, build fingerprint, display modes, temperatures, thermal state, storage, root/profileable flags, and Perfetto capabilities.

- [ ] **Step 4: Implement parent-scoped leases**

Acquire a lease with a partial unique active-device index and a CAS update of the selected parent. `tasks/next` returns an Ed25519 JWS with a protected `kid` and payload containing `analysis_id`, `scenario_job_id`, `lease_id`, `agent_id`, device identity, input artifact IDs, recipe hash, issued/expiry times, and `audience=perfpilot-agent`. Configuration exposes one active signing key and one staged next public key; rotation changes `kid` only after Agents report the staged key. Subsequent task calls require both the Agent token and lease token.

Offline reconciliation performs one transaction: mark Agent offline, revoke its active parent leases, requeue retryable current scenarios, and quarantine associated devices.

- [ ] **Step 5: Implement sample and scenario completion endpoints**

`POST .../samples` validates the lease, snapshot slot, attempt number, artifact versions, manifest, and the team derived from the scenario job. Bridge the control and tenant databases with this idempotent saga:

1. reserve the opaque `sample_id` and attempt number by CAS on `scenario_jobs`, and insert a control outbox row with `ready_at=NULL`;
2. create or read the same `sample_id` in the owning team database with state `finalized` and immutable artifact references;
3. set the control outbox `ready_at` and commit the scenario attempt count;
4. let the dispatcher publish only rows whose `ready_at` is non-null.

If the API exits at any boundary, retrying the same manifest resumes by sample ID. The reconciler checks unready outbox rows through `TenantRouter`: it marks the event ready when the tenant sample exists, or safely expires the reservation when no tenant write ever occurred. It never stores the sample manifest, package, filename, object key, or verdict in the control database.

The validator idempotently writes its verdict to the team sample, then completes the control claim and updates opaque valid/invalid counters. A crash between those commits reuses the same verdict ID. `GET .../tasks/{scenario_job_id}` and `POST .../complete` read authoritative team verdicts through the server-derived route; the Agent cannot declare validity.

- [ ] **Step 6: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest services/api/tests/integration/test_agent_api.py -q
git add services/api/src/perfpilot_api services/api/tests contracts/v1/agent
git commit -m "feat: add device Agent leases"
```

## Task 9: Implement outbox dispatch, scheduler, claims, and reconciliation

**Files:**
- Create: `services/api/src/perfpilot_api/events/{envelope,streams}.py`
- Create: `services/api/src/perfpilot_api/workers/{dispatcher,scheduler,reconciler}.py`
- Create: `services/api/src/perfpilot_api/services/claims.py`
- Create: `services/api/src/perfpilot_api/security/service_tokens.py`
- Create: `services/api/src/perfpilot_api/api/internal_worker.py`
- Create: `services/api/tests/unit/test_event_routing.py`
- Create: `services/api/tests/integration/test_outbox_recovery.py`
- Create: `services/api/tests/integration/test_worker_claim_api.py`
- Create: `contracts/v1/worker/{claim,inputs,verdict,report,completion}.schema.json`

- [ ] **Step 1: Write failing delivery tests**

```python
async def test_dispatcher_routes_event_once(dispatcher, redis_client, outbox_repo) -> None:
    event = await outbox_repo.add_ready(
        "sample_validation_requested", "sample_attempt", SAMPLE_ID
    )
    await dispatcher.run_once()
    messages = await redis_client.xrange("perfpilot:sample-validation")
    assert len(messages) == 1
    assert messages[0][1]["event_id"] == str(event.id)
    assert await outbox_repo.is_published(event.id)


async def test_expired_claim_is_taken_over_without_duplicate_result(
    consumer, expired_claim, inbox_repo
) -> None:
    await consumer.handle(expired_claim.event)
    await consumer.handle(expired_claim.event)
    assert await inbox_repo.processed_count(expired_claim.event.id) == 1


def test_worker_claim_derives_team_and_never_returns_route(
    internal_worker_client, analysis_event, team_a, team_b
) -> None:
    claim = internal_worker_client.post(
        f"/internal/v1/worker/events/{analysis_event.id}/claim",
        json={"consumer_id": "worker-test", "team_id": str(team_b.id)},
    )
    assert claim.status_code == 422
    valid = internal_worker_client.post(
        f"/internal/v1/worker/events/{analysis_event.id}/claim",
        json={"consumer_id": "worker-test"},
    )
    assert valid.status_code == 201
    assert valid.json()["subject_team_id"] == str(team_a.id)
    assert "database" not in json.dumps(valid.json())
    assert "bucket" not in json.dumps(valid.json())
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_event_routing.py \
  services/api/tests/integration/test_outbox_recovery.py \
  services/api/tests/integration/test_worker_claim_api.py -q
```

Expected: FAIL because event routing and claims are absent.

- [ ] **Step 3: Implement the dispatcher and stream groups**

Map event types exactly:

```python
STREAM_BY_EVENT = {
    "analysis_queued": "perfpilot:schedule",
    "sample_validation_requested": "perfpilot:sample-validation",
    "analysis_requested": "perfpilot:analysis",
}
```

After `XADD`, save `published_at`. Unknown events go to a dead-letter record. Consumers insert `(consumer_name, event_id)` inbox state and create or take over a claim in one control transaction. They ACK only processed inbox rows or events whose authoritative state has already advanced.

Expose claim operations only on the private `/internal/v1/worker/...` router. Authenticate a hashed, rotatable Worker service token from a mounted secret, then require the per-claim token on inputs, verdict, report, completion, and renewal. The claim request contains event and consumer IDs only. The API derives subject, task, and team from the control database; input responses contain opaque artifact metadata and short-lived version-bound GET URLs, never a DSN, credential, bucket, or caller-selectable object key. Verdict/report calls validate JSON Schema and persist through `TenantRouter`.

Caddy must not route `/internal/`; only the private container network can reach it. Cap verdict/report JSON, reject unknown fields, redact claim tokens and signed URLs, and rotate the service token with an overlap window.

- [ ] **Step 4: Implement scheduler fairness and reconciler scans**

The scheduler round-robins teams, uses FIFO inside a team, enforces two active and twenty queued device parents by default, and selects only compatible healthy devices. A parent keeps device affinity through all three scenarios. After a device failure, only an unstarted child may move to another compatible device, the new device starts a separate report group, and cross-device root-cause aggregation is forbidden. An RKGallery acceptance parent sets `device_migration_allowed=false` and fails instead of migrating.

The reconciler runs every 30 seconds and republishes unpublished ready outbox rows, repairs unready sample-finalize saga rows through the server-derived tenant route, takes over pending messages with expired claims, resets retryable stalled work, and fails exhausted work with stable codes.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest \
  services/api/tests/unit/test_event_routing.py \
  services/api/tests/integration/test_outbox_recovery.py \
  services/api/tests/integration/test_worker_claim_api.py -q
git add services/api/src/perfpilot_api services/api/tests contracts/v1/worker
git commit -m "feat: add reliable orchestration events"
```

## Task 10: Implement cancellation, deletion, retention, and audit

**Files:**
- Create: `services/api/src/perfpilot_api/services/{cancellation,deletion,retention,audit}.py`
- Create: `services/api/src/perfpilot_api/api/audit.py`
- Create: `services/api/tests/integration/test_lifecycle_cleanup.py`

- [ ] **Step 1: Write failing cleanup tests**

```python
async def test_delete_running_device_job_cancels_then_tombstones(api, running_analysis) -> None:
    response = await api.delete(running_analysis.url)
    assert response.status_code == 202
    state = await api.get_json(running_analysis.url)
    assert state["delete_requested_at"]
    assert state["state"] == "canceled"
    assert state["active_lease"] is None


async def test_audit_never_stores_secret_material(audit_repo) -> None:
    await audit_repo.record(
        "agent_rotated",
        {"authorization": "Bearer secret", "presigned_url": "https://object.invalid/signed"},
    )
    payload = await audit_repo.latest_payload()
    assert "secret" not in json.dumps(payload)
    assert "signed" not in json.dumps(payload)
```

- [ ] **Step 2: Run RED**

```bash
uv run --package perfpilot-api pytest services/api/tests/integration/test_lifecycle_cleanup.py -q
```

Expected: lifecycle DELETE and audit redaction tests fail.

- [ ] **Step 3: Implement cancellation and tombstones**

Device cancellation writes `cancel_requested_at`, revokes the parent lease and all non-terminal sample/Worker claims, and cancels non-terminal children in one transaction. Trace-mode cancellation revokes the parent Worker claim. DELETE is idempotent: non-terminal analyses return `202`; terminal analyses write a tombstone, block new downloads, delete all object versions and tenant rows asynchronously, and retain only content-free control audit data.

- [ ] **Step 4: Implement retention and redaction**

Raw artifacts default to 30 days. Retention skips non-terminal jobs and artifacts referenced by active claims. After deleting every stored object version, retain content-free artifact provenance (kind, bytes, hash, tool relation, created/expired timestamps, and deletion audit) and return `410 artifact_expired` for download; do not claim the report remains re-runnable. Redact passwords, cookies, authorization headers, Agent tokens, DSNs, object keys, and full signed URLs before audit persistence.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run --package perfpilot-api pytest services/api/tests/integration/test_lifecycle_cleanup.py -q
git add services/api/src/perfpilot_api services/api/tests
git commit -m "feat: add audited analysis cleanup"
```

## Task 11: Add containers, CI, and infrastructure failure gates

**Files:**
- Create: `services/api/Dockerfile`
- Create: `infra/compose.yaml`
- Create: `infra/compose.acceptance.yaml`
- Create: `infra/caddy/Caddyfile`
- Create: `infra/scripts/{wait_for_services,inject_faults}.py`
- Create: `infra/scripts/verify_private_host.py`
- Create: `docs/deployment/private-acceptance.md`
- Create: `.github/workflows/platform-ci.yml`
- Create: `services/api/tests/integration/test_cross_tenant_isolation.py`
- Create: `services/api/tests/integration/test_failure_recovery.py`
- Modify: `.gitignore`
- Modify: `README.md`

- [ ] **Step 1: Write cross-tenant and failure tests**

Add tests that prove:

```text
team A session + team B task ID                 → 404 resource_not_found
team A artifact ID + team B download path       → 404 resource_not_found
Redis stopped after outbox commit               → event publishes after restart
API stopped after S3 PUT before finalize        → same upload_id finalizes once
dispatcher stopped after XADD before published  → duplicate delivery, one inbox result
```

- [ ] **Step 2: Run RED against the incomplete stack**

```bash
docker compose -f infra/compose.yaml up -d postgres redis minio
uv run --package perfpilot-api pytest \
  services/api/tests/integration/test_cross_tenant_isolation.py \
  services/api/tests/integration/test_failure_recovery.py -q
```

Expected: FAIL until the service containers, bootstrap hooks, and fault helpers exist.

- [ ] **Step 3: Add the Compose topology**

Bind PostgreSQL, Redis, and MinIO management ports to `127.0.0.1` only. `compose.acceptance.yaml` adds Caddy and exposes only HTTPS API ingress; port 80 is allowed only for ACME redirect/challenge. Give API, Provisioner, dispatcher, scheduler, reconciler, validator, and full Worker separate commands and least-privilege credentials. Mount the secret-store master key from an owner-only container secret and ciphertext storage from a dedicated non-public volume; neither appears in Compose environment values or image layers. Health checks must gate dependent containers.

`verify_private_host.py` performs read-only checks for exact Git SHA, image digests, TLS, proxy-signature rejection/acceptance, private database/Redis/MinIO ports, migrations, bucket versioning, secret-file modes, disk capacity, clock synchronization, and Agent outbound reachability. `private-acceptance.md` lists the exact required environment variable names and rollback sequence without containing values.

- [ ] **Step 4: Add CI**

CI must:

```text
checkout → setup Python 3.12 → setup uv → uv sync --locked
start PostgreSQL/Redis/MinIO → migrate control and two tenant databases
ruff → contract tests → unit tests → integration tests → migration downgrade/upgrade
npm ci → npm run lint → npm test
secret scan → dependency audit → container build
```

Use `astral-sh/setup-uv` pinned to its full action commit. Do not publish images from pull requests.

- [ ] **Step 5: Run the packet verification**

```bash
uv sync --locked --all-packages --dev
uv run ruff check services/api
uv run pytest services/api/tests -q
npm ci
npm run lint
npm test
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 6: Commit and fast-forward push**

```bash
git add .github README.md .gitignore infra docs/deployment/private-acceptance.md services/api/Dockerfile services/api/tests
git commit -m "ci: verify PerfPilot control plane"
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
git status --short
```

Expected: remote and local SHAs match and the worktree is clean.
