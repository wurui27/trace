# PerfPilot External Engine Control-Plane Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze the existing Task 7 device-analysis baseline, then add the version lock, typed Adapter boundary, registry, and control-plane persistence required to integrate SmartPerfetto and Android-App-Memory-Analysis without importing either upstream codebase.

**Architecture:** The first packet keeps all upstream engines out of the FastAPI process. Checked-in engine pins are validated before use, Adapter implementations conform to one typed asynchronous protocol, and the control database stores only opaque workspace/run identifiers plus version and state metadata. Raw engine results remain immutable tenant artifacts and are not part of this packet.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, Alembic, PostgreSQL, PyYAML, JSON Schema, pytest, uv.

---

## Delivery boundaries

This is the first independently testable packet of the approved external-engine design. Later packets remain separate because each has its own failure surface and release gate:

1. SmartPerfetto workspace HTTP/SSE Adapter and recovery.
2. Android memory Worker, archive isolation, and context validation.
3. Report Normalizer and platform AI summarizer.
4. Direct Trace-upload API and real Web data.
5. Container isolation, upstream upgrade gates, canary, rollback, and end-to-end acceptance.

Do not add HTTP calls to SmartPerfetto, invoke `tools/ai_context.py`, change the Web, or add AI-provider code in this packet.

## Locked file structure

```text
infra/engines/
├── engine-lock.yaml                      # Reviewed upstream refs, commits, digests, contracts
└── engine-lock.schema.json               # Machine-checkable lock format
services/api/src/perfpilot_api/
├── engines/
│   ├── __init__.py                       # Public internal engine types
│   ├── contracts.py                      # Adapter protocol and immutable value types
│   ├── lock.py                           # Safe lock loader and production digest gate
│   ├── registry.py                       # Duplicate-safe Adapter lookup
│   └── states.py                         # Engine execution state transitions
└── db/control/models/engines.py          # Workspace mapping and execution metadata only
services/api/migrations/control/versions/
└── 0004_external_engine_foundation.py    # Control-plane schema migration
services/api/tests/
├── unit/test_engine_lock.py
├── unit/test_engine_contracts.py
├── unit/test_engine_states.py
└── integration/test_analysis_repository.py # Existing real-PostgreSQL fixture, extended
```

`TeamEngineWorkspace` and `EngineExecution` belong to the control database because the orchestrator must recover work without scanning tenant databases. They may contain only opaque identifiers, hashes, versions, timestamps, and stable error codes. Structured engine output, prompts, evidence, paths, object keys, signed URLs, and customer content are forbidden in these tables.

## Task 1: Close and freeze the existing Task 7 baseline

**Files:**

- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/api/tests/unit/test_analysis_reports.py`
- Modify: `services/api/tests/unit/test_app.py`
- Modify: `services/api/tests/integration/test_analysis_repository.py`
- Modify: `docs/superpowers/plans/2026-07-23-perfpilot-phase1-control-plane.md`
- Verify only: all currently modified and untracked Task 7 files listed by `git status --short`

- [ ] **Step 1: Record the known-green Task 7 baseline**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-task7-closeout-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_state_machines.py \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/unit/test_analysis_reports.py \
  services/api/tests/unit/test_analysis_service.py \
  services/api/tests/unit/test_apk_inspection.py \
  services/api/tests/integration/test_analysis_api.py \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q
```

Expected before adding the new tests: `117 passed, 38 skipped`. PostgreSQL-only tests may skip when `PERFPILOT_TEST_POSTGRES_URL` is absent; the required PostgreSQL gate appears in Step 8.

- [ ] **Step 2: Write failing report consistency and privacy tests**

Import `_copy_public_json` in `services/api/tests/unit/test_analysis_reports.py`, then add:

```python
@pytest.mark.parametrize(
    "value",
    [
        {"downloadUrl": "https://objects.example/private"},
        {"fields": {"note": "authorization=Bearer private-token"}},
        {"fields": {"note": "s3://private-bucket/private-key"}},
    ],
)
def test_public_report_projection_rejects_dynamic_private_data(
    value: dict[str, object],
) -> None:
    with pytest.raises(AnalysisUnavailableError, match="private data"):
        _copy_public_json(value)
```

In `services/api/tests/integration/test_analysis_repository.py`, import `ReportNotAvailableError`, reuse `_persist_metadata_and_stage()`, and add a terminal failure report whose control and tenant copies have the same aggregate state but different stable failure codes:

```python
@pytest.mark.asyncio
async def test_report_read_rejects_control_tenant_child_drift(
    analysis_databases: AnalysisDatabases,
) -> None:
    prepared = await _persist_metadata_and_stage(analysis_databases)
    await analysis_databases.repository.queue_control_scenarios(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
        scenarios=prepared,
        requirements=SchedulingRequirements(
            min_api_level=28,
            supported_abis=("arm64-v8a", "x86_64"),
        ),
        now=NOW + timedelta(minutes=3),
    )
    async with analysis_databases.control_sessions.begin() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)
        children = list(
            (
                await session.scalars(
                    select(ScenarioJob).where(ScenarioJob.analysis_id == ANALYSIS_ID)
                )
            ).all()
        )
        assert job is not None and len(children) == 3
        job.state = "failed"
        job.failure_code = "scenario_failed"
        job.completed_at = NOW
        job.version += 1
        for child in children:
            child.state = "failed"
            child.failure_code = "trace_invalid"
            child.completed_at = NOW
            child.version += 1

    async with analysis_databases.tenant_sessions.begin() as session:
        analysis = await session.get(Analysis, ANALYSIS_ID)
        assert analysis is not None
        analysis.state = "failed"
        analysis.failure_code = "scenario_failed"
        analysis.completed_at = NOW
        analysis.version += 1
        scroll = await session.scalar(
            select(ScenarioResult).where(
                ScenarioResult.analysis_id == ANALYSIS_ID,
                ScenarioResult.scenario_type == "scroll",
            )
        )
        assert scroll is not None
        scroll.state = "failed"
        scroll.failure_code = "different_failure"
        scroll.device_group_reason = "device_unavailable"
        scroll.version += 1
        siblings = list(
            (
                await session.scalars(
                    select(ScenarioResult).where(
                        ScenarioResult.analysis_id == ANALYSIS_ID,
                        ScenarioResult.id != scroll.id,
                    )
                )
            ).all()
        )
        assert len(siblings) == 2
        for sibling in siblings:
            sibling.state = "failed"
            sibling.failure_code = "trace_invalid"
            sibling.device_group_reason = "device_unavailable"
            sibling.version += 1

    with pytest.raises(ReportNotAvailableError, match="not available"):
        await analysis_databases.repository.load_report(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
        )
```

- [ ] **Step 3: Write the failing production isolation test**

Add this test to `services/api/tests/unit/test_app.py`. Reuse `_production_settings()` and the existing fake engine/runtime style in that file.

```python
def test_production_refuses_owned_in_process_apk_inspector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        async def dispose(self) -> None:
            return None

    class FakeArtifactRuntime:
        upload_service = object()
        apk_inspector = object()
        tenant_router = object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(main, "create_control_engine", lambda _: FakeEngine())
    monkeypatch.setattr(main, "create_control_session_factory", lambda _: object())

    async def build_artifact_runtime(**_: object) -> FakeArtifactRuntime:
        return FakeArtifactRuntime()

    monkeypatch.setattr(main, "build_artifact_runtime", build_artifact_runtime)
    app = create_app(
        testing=False,
        settings_override=_production_settings(),
        auth_service=object(),  # type: ignore[arg-type]
        admin_team_service=object(),  # type: ignore[arg-type]
        replay_store=object(),  # type: ignore[arg-type]
        proxy_client_identity_required=False,
    )

    with pytest.raises(RuntimeError, match="externally isolated"):
        with TestClient(app):
            pass
```

- [ ] **Step 4: Run RED**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-task7-closeout-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_analysis_reports.py \
  services/api/tests/unit/test_app.py \
  services/api/tests/integration/test_analysis_repository.py -q
```

Expected: the production test fails because the API selects `owned_artifact_runtime.apk_inspector`; the repository test fails because `load_report()` reads no control children. The privacy cases should pass if the interrupted reviewer fix is already present; a test already green is retained as regression coverage.

- [ ] **Step 5: Make report reads use the same control/tenant consistency gate as availability**

In `SQLAlchemyAnalysisRepository.load_report()`, load the authoritative control children in the same control session as the parent:

```python
children = list(
    (
        await session.scalars(
            select(ScenarioJob).where(ScenarioJob.analysis_id == analysis_id)
        )
    ).all()
)
```

After loading tenant scenarios and report versions, reject any drift before assembling public JSON:

```python
if not _report_is_available(
    children,
    scenarios,
    versions,
    parent_state=job.state,
):
    raise ReportNotAvailableError("analysis report is not available")
return _assemble_report(job=job, scenarios=scenarios, versions=versions)
```

Do not derive child identity or state solely from tenant rows. Do not return a partial report when the control and tenant copies disagree.

- [ ] **Step 6: Fail closed when production has no externally isolated APK inspector**

Replace the inspector selection inside `create_app()`'s lifespan with:

```python
if settings.app_env == "production":
    resolved_inspector = apk_inspector
else:
    resolved_inspector = apk_inspector or owned_artifact_runtime.apk_inspector
if resolved_inspector is None:
    raise RuntimeError("An externally isolated APK inspector is unavailable")
```

The injected production object is a future remote Worker client implementing the existing `ApkInspector` protocol. The FastAPI production process must never fall back to `owned_artifact_runtime.apk_inspector`.

Update Task 11 of `docs/superpowers/plans/2026-07-23-perfpilot-phase1-control-plane.md` with this exact deployment requirement:

```text
The production APK inspector runs as a non-root, no-network Worker with a read-only root
filesystem, a per-claim writable directory, no database or object-store credentials,
bounded CPU/memory/PID/disk/time, and a version-bound input URL. The API receives only
validated manifest metadata through the private claim API. Production startup fails if
the remote inspector client is not configured; in-process apkanalyzer is test/development only.
```

- [ ] **Step 7: Run focused GREEN**

Run the Step 4 command again.

Expected: all selected tests pass; PostgreSQL integration tests skip only when the PostgreSQL test URL is absent.

- [ ] **Step 8: Run the real PostgreSQL and full API regression gates**

Run:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-task7-closeout-pg \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q

env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-task7-closeout-all \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider services/api/tests -q
```

Expected: both commands pass with no skipped PostgreSQL tests in the first command.

- [ ] **Step 9: Commit Task 7 without staging plan-session artifacts**

Run:

```bash
git diff --check
git status --short
git add \
  contracts/v1/analyses \
  contracts/v1/examples/analysis-report.partial.valid.json \
  contracts/v1/reports \
  services/api/migrations/control/versions/0003_analysis_orchestration.py \
  services/api/migrations/tenant/versions/0003_analysis_orchestration.py \
  services/api/pyproject.toml \
  services/api/src/perfpilot_api \
  services/api/tests \
  uv.lock \
  docs/superpowers/plans/2026-07-23-perfpilot-phase1-control-plane.md
git diff --cached --check
git commit -m "feat: add device analysis orchestration"
git push origin HEAD:main
```

Expected: `.superpowers/` remains untracked and unstaged. The push is fast-forward. Record the pushed SHA before Task 2.

## Task 2: Add the validated engine version lock

**Files:**

- Create: `infra/engines/engine-lock.yaml`
- Create: `infra/engines/engine-lock.schema.json`
- Create: `services/api/src/perfpilot_api/engines/__init__.py`
- Create: `services/api/src/perfpilot_api/engines/lock.py`
- Create: `services/api/tests/unit/test_engine_lock.py`
- Modify: `services/api/pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Write the lock schema and failing loader tests**

Create `infra/engines/engine-lock.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://perfpilot.internal/infra/engine-lock.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "engines"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "engines": {
      "type": "object",
      "additionalProperties": false,
      "required": ["smartperfetto", "android_memory"],
      "properties": {
        "smartperfetto": {"$ref": "#/$defs/smartPin"},
        "android_memory": {"$ref": "#/$defs/memoryPin"}
      }
    }
  },
  "$defs": {
    "basePin": {
      "type": "object",
      "required": ["source", "commit", "image_digest"],
      "properties": {
        "source": {"type": "string", "pattern": "^https://github\\.com/[^/]+/[^/]+\\.git$"},
        "ref": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
        "commit": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
        "image_digest": {
          "type": ["string", "null"],
          "pattern": "^sha256:[a-f0-9]{64}$"
        }
      }
    },
    "smartPin": {
      "allOf": [
        {"$ref": "#/$defs/basePin"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["source", "ref", "commit", "image_digest", "api_contract"],
          "properties": {
            "source": true,
            "ref": true,
            "commit": true,
            "image_digest": true,
            "api_contract": {"const": "workspace-agent-v1"}
          }
        }
      ]
    },
    "memoryPin": {
      "allOf": [
        {"$ref": "#/$defs/basePin"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["source", "ref", "commit", "image_digest", "output_contract"],
          "properties": {
            "source": true,
            "ref": true,
            "commit": true,
            "image_digest": true,
            "output_contract": {"const": "android-memory-ai-context-1.2"}
          }
        }
      ]
    }
  }
}
```

Create `services/api/tests/unit/test_engine_lock.py`:

```python
from pathlib import Path

import pytest

from perfpilot_api.engines.lock import EngineLockError, load_engine_lock


ROOT = Path(__file__).parents[4]
LOCK = ROOT / "infra/engines/engine-lock.yaml"
SCHEMA = ROOT / "infra/engines/engine-lock.schema.json"


def test_checked_in_engine_lock_loads_exact_reviewed_commits() -> None:
    lock = load_engine_lock(LOCK, schema_path=SCHEMA, require_image_digests=False)

    assert lock.smartperfetto.commit == "1508f99788bfcf18cc861e4bf4f8b472e84240c3"
    assert lock.smartperfetto.contract == "workspace-agent-v1"
    assert lock.android_memory.commit == "d5514972ced78c3faa7fc17589c1ea9231645056"
    assert lock.android_memory.contract == "android-memory-ai-context-1.2"


def test_production_rejects_null_image_digests() -> None:
    with pytest.raises(EngineLockError, match="image digest"):
        load_engine_lock(LOCK, schema_path=SCHEMA, require_image_digests=True)


def test_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    candidate = tmp_path / "engine-lock.yaml"
    candidate.write_text(
        LOCK.read_text(encoding="utf-8") + "\nunknown: true\n",
        encoding="utf-8",
    )
    with pytest.raises(EngineLockError, match="invalid"):
        load_engine_lock(candidate, schema_path=SCHEMA, require_image_digests=False)
```

- [ ] **Step 2: Run RED**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-lock-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider services/api/tests/unit/test_engine_lock.py -q
```

Expected: FAIL because `perfpilot_api.engines.lock` does not exist.

- [ ] **Step 3: Add the reviewed lock file**

Create `infra/engines/engine-lock.yaml`:

```yaml
schema_version: "1.0"
engines:
  smartperfetto:
    source: https://github.com/Gracker/SmartPerfetto.git
    ref: v1.0.38
    commit: 1508f99788bfcf18cc861e4bf4f8b472e84240c3
    image_digest: null
    api_contract: workspace-agent-v1
  android_memory:
    source: https://github.com/Gracker/Android-App-Memory-Analysis.git
    ref: null
    commit: d5514972ced78c3faa7fc17589c1ea9231645056
    image_digest: null
    output_contract: android-memory-ai-context-1.2
```

- [ ] **Step 4: Implement the safe loader**

Add `PyYAML>=6.0.2,<7` to `services/api/pyproject.toml`, refresh `uv.lock`, and create `services/api/src/perfpilot_api/engines/lock.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


class EngineLockError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EnginePin:
    source: str
    ref: str | None
    commit: str
    image_digest: str | None
    contract: str


@dataclass(frozen=True, slots=True)
class EngineLock:
    schema_version: str
    smartperfetto: EnginePin
    android_memory: EnginePin


def _pin(value: object, *, contract_key: str) -> EnginePin:
    if not isinstance(value, dict):
        raise EngineLockError("engine lock is invalid")
    return EnginePin(
        source=str(value["source"]),
        ref=value["ref"] if isinstance(value.get("ref"), str) else None,
        commit=str(value["commit"]),
        image_digest=(
            value["image_digest"] if isinstance(value.get("image_digest"), str) else None
        ),
        contract=str(value[contract_key]),
    )


def load_engine_lock(
    path: Path,
    *,
    schema_path: Path,
    require_image_digests: bool,
) -> EngineLock:
    try:
        candidate = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(candidate)
        if not isinstance(candidate, dict) or not isinstance(candidate["engines"], dict):
            raise EngineLockError("engine lock is invalid")
        lock = EngineLock(
            schema_version=str(candidate["schema_version"]),
            smartperfetto=_pin(
                candidate["engines"]["smartperfetto"],
                contract_key="api_contract",
            ),
            android_memory=_pin(
                candidate["engines"]["android_memory"],
                contract_key="output_contract",
            ),
        )
    except EngineLockError:
        raise
    except Exception:
        raise EngineLockError("engine lock is invalid") from None
    if require_image_digests and any(
        pin.image_digest is None for pin in (lock.smartperfetto, lock.android_memory)
    ):
        raise EngineLockError("production engine image digest is required")
    return lock
```

Export `EngineLock`, `EngineLockError`, `EnginePin`, and `load_engine_lock` from `engines/__init__.py`.

- [ ] **Step 5: Run GREEN and dependency checks**

Run:

```bash
/Users/ray/Library/Python/3.12/bin/uv lock --offline
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-lock-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider services/api/tests/unit/test_engine_lock.py -q
```

Expected: `3 passed`; `uv.lock` contains the resolved PyYAML package without unrelated upgrades.

- [ ] **Step 6: Commit and push the lock contract**

Run:

```bash
git diff --check
git add \
  infra/engines/engine-lock.yaml \
  infra/engines/engine-lock.schema.json \
  services/api/pyproject.toml \
  services/api/src/perfpilot_api/engines/__init__.py \
  services/api/src/perfpilot_api/engines/lock.py \
  services/api/tests/unit/test_engine_lock.py \
  uv.lock
git diff --cached --check
git commit -m "feat: lock external analysis engine versions"
git push origin HEAD:main
```

Expected: fast-forward push. Do not replace either null digest with an invented value; production remains intentionally blocked until signed images exist.

## Task 3: Define the Adapter protocol, registry, and state machine

**Files:**

- Create: `services/api/src/perfpilot_api/engines/contracts.py`
- Create: `services/api/src/perfpilot_api/engines/registry.py`
- Create: `services/api/src/perfpilot_api/engines/states.py`
- Modify: `services/api/src/perfpilot_api/engines/__init__.py`
- Create: `services/api/tests/unit/test_engine_contracts.py`
- Create: `services/api/tests/unit/test_engine_states.py`

- [ ] **Step 1: Write failing contract and registry tests**

Create `services/api/tests/unit/test_engine_contracts.py`:

```python
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.engines.contracts import (
    AdapterDescriptor,
    EngineEvent,
    EngineInput,
    EngineResult,
    EngineRunRef,
    SubmitConfig,
)
from perfpilot_api.engines.registry import AdapterRegistry, AdapterRegistryError


class FakeAdapter:
    descriptor = AdapterDescriptor(
        engine_id="smartperfetto",
        adapter_version="1.0.0",
        profiles=frozenset({"auto", "startup", "scroll"}),
        required_inputs=frozenset({"trace"}),
        optional_inputs=frozenset(),
        accepted_contracts=frozenset({"workspace-agent-v1"}),
        default_timeout_seconds=1800,
        resource_profile="network_service",
        stable_error_codes=frozenset(
            {"capacity_exceeded", "engine_timeout", "engine_unavailable"}
        ),
    )

    async def submit(self, inputs, config):
        return EngineRunRef("smartperfetto", "session-1", "run-1", None)

    async def stream(self, run_ref, cursor):
        return (
            EngineEvent("event-1", "running", 25, "trace_indexed", datetime.now(UTC)),
        )

    async def fetch_result(self, run_ref):
        return EngineResult("workspace-agent-v1", "completed", {"report": {}})

    async def cancel(self, run_ref):
        return "canceled"


def test_registry_returns_only_registered_adapter() -> None:
    adapter = FakeAdapter()
    registry = AdapterRegistry((adapter,))
    assert registry.require("smartperfetto") is adapter
    with pytest.raises(AdapterRegistryError, match="not registered"):
        registry.require("android_memory")


def test_registry_rejects_duplicate_engine_ids() -> None:
    with pytest.raises(AdapterRegistryError, match="duplicate"):
        AdapterRegistry((FakeAdapter(), FakeAdapter()))


def test_engine_input_carries_only_ephemeral_location_and_public_metadata() -> None:
    value = EngineInput(
        artifact_id=UUID("40000000-0000-4000-8000-000000000001"),
        kind="trace",
        mime="application/octet-stream",
        size_bytes=1024,
        sha256_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        download_url=SecretStr("https://claim.internal/artifacts/opaque"),
    )
    assert value.kind == "trace"
    assert "bucket" not in type(value).__dataclass_fields__
    assert "object_key" not in type(value).__dataclass_fields__
    assert "claim.internal" not in repr(value)
```

- [ ] **Step 2: Write failing transition tests**

Create `services/api/tests/unit/test_engine_states.py`:

```python
import pytest

from perfpilot_api.engines.states import (
    EngineExecutionState,
    InvalidEngineTransition,
    transition_engine_state,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("pending", "running"),
        ("running", "awaiting_user"),
        ("awaiting_user", "running"),
        ("running", "completed"),
        ("running", "insufficient_data"),
        ("running", "failed"),
        ("running", "canceled"),
    ],
)
def test_valid_engine_transitions(current: str, target: str) -> None:
    assert transition_engine_state(current, target) is EngineExecutionState(target)


@pytest.mark.parametrize("terminal", ["completed", "insufficient_data", "failed", "canceled"])
def test_terminal_engine_state_cannot_reopen(terminal: str) -> None:
    with pytest.raises(InvalidEngineTransition):
        transition_engine_state(terminal, "running")
```

- [ ] **Step 3: Run RED**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-contracts-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_states.py -q
```

Expected: collection fails because the modules do not exist.

- [ ] **Step 4: Implement immutable Adapter values and protocol**

Create `services/api/src/perfpilot_api/engines/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import SecretStr


AnalysisProfile = Literal["auto", "startup", "scroll"]
ResourceProfile = Literal["network_service", "isolated_worker"]
ExecutionStateValue = Literal[
    "pending",
    "running",
    "awaiting_user",
    "completed",
    "insufficient_data",
    "failed",
    "canceled",
]
EngineTerminalStateValue = Literal[
    "completed",
    "insufficient_data",
    "failed",
    "canceled",
]


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    engine_id: str
    adapter_version: str
    profiles: frozenset[AnalysisProfile]
    required_inputs: frozenset[str]
    optional_inputs: frozenset[str]
    accepted_contracts: frozenset[str]
    default_timeout_seconds: int
    resource_profile: ResourceProfile
    stable_error_codes: frozenset[str]


@dataclass(frozen=True, slots=True)
class EngineInput:
    artifact_id: UUID
    kind: str
    mime: str
    size_bytes: int
    sha256_b64: str
    download_url: SecretStr


@dataclass(frozen=True, slots=True)
class SubmitConfig:
    analysis_id: UUID
    profile: AnalysisProfile
    question: str | None
    external_workspace_id: str | None
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class EngineRunRef:
    engine_id: str
    external_session_id: str | None
    external_run_id: str | None
    cursor: str | None


@dataclass(frozen=True, slots=True)
class EngineEvent:
    event_id: str
    state: ExecutionStateValue
    progress_percent: int | None
    message_code: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class EngineResult:
    contract: str
    state: EngineTerminalStateValue
    payload: dict[str, object]


class EngineAdapter(Protocol):
    descriptor: AdapterDescriptor

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef: ...

    async def stream(
        self,
        run_ref: EngineRunRef,
        cursor: str | None,
    ) -> tuple[EngineEvent, ...]: ...

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult: ...

    async def cancel(self, run_ref: EngineRunRef) -> EngineTerminalStateValue: ...
```

The `download_url` is ephemeral and must never be logged or persisted. Later claim code constructs it from a server-owned artifact record.

- [ ] **Step 5: Implement duplicate-safe registry and transitions**

Create `services/api/src/perfpilot_api/engines/registry.py`:

```python
from __future__ import annotations

from collections.abc import Iterable

from perfpilot_api.engines.contracts import EngineAdapter


class AdapterRegistryError(RuntimeError):
    pass


class AdapterRegistry:
    def __init__(self, adapters: Iterable[EngineAdapter]) -> None:
        self._adapters: dict[str, EngineAdapter] = {}
        for adapter in adapters:
            engine_id = adapter.descriptor.engine_id
            if engine_id in self._adapters:
                raise AdapterRegistryError(f"duplicate engine adapter: {engine_id}")
            self._adapters[engine_id] = adapter

    def require(self, engine_id: str) -> EngineAdapter:
        adapter = self._adapters.get(engine_id)
        if adapter is None:
            raise AdapterRegistryError(f"engine adapter is not registered: {engine_id}")
        return adapter
```

Create `services/api/src/perfpilot_api/engines/states.py`:

```python
from enum import StrEnum


class EngineExecutionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    COMPLETED = "completed"
    INSUFFICIENT_DATA = "insufficient_data"
    FAILED = "failed"
    CANCELED = "canceled"


class InvalidEngineTransition(RuntimeError):
    pass


_ALLOWED = {
    EngineExecutionState.PENDING: {
        EngineExecutionState.RUNNING,
        EngineExecutionState.FAILED,
        EngineExecutionState.CANCELED,
    },
    EngineExecutionState.RUNNING: {
        EngineExecutionState.AWAITING_USER,
        EngineExecutionState.COMPLETED,
        EngineExecutionState.INSUFFICIENT_DATA,
        EngineExecutionState.FAILED,
        EngineExecutionState.CANCELED,
    },
    EngineExecutionState.AWAITING_USER: {
        EngineExecutionState.RUNNING,
        EngineExecutionState.FAILED,
        EngineExecutionState.CANCELED,
    },
    EngineExecutionState.COMPLETED: set(),
    EngineExecutionState.INSUFFICIENT_DATA: set(),
    EngineExecutionState.FAILED: set(),
    EngineExecutionState.CANCELED: set(),
}


def transition_engine_state(current: str, target: str) -> EngineExecutionState:
    source = EngineExecutionState(current)
    destination = EngineExecutionState(target)
    if destination not in _ALLOWED[source]:
        raise InvalidEngineTransition(f"invalid engine transition: {source} -> {destination}")
    return destination
```

Replace `engines/__init__.py` with the complete internal surface:

```python
from perfpilot_api.engines.contracts import (
    AdapterDescriptor,
    AnalysisProfile,
    EngineAdapter,
    EngineEvent,
    EngineInput,
    EngineResult,
    EngineRunRef,
    EngineTerminalStateValue,
    ExecutionStateValue,
    ResourceProfile,
    SubmitConfig,
)
from perfpilot_api.engines.lock import (
    EngineLock,
    EngineLockError,
    EnginePin,
    load_engine_lock,
)
from perfpilot_api.engines.registry import AdapterRegistry, AdapterRegistryError
from perfpilot_api.engines.states import (
    EngineExecutionState,
    InvalidEngineTransition,
    transition_engine_state,
)

__all__ = [
    "AdapterDescriptor",
    "AdapterRegistry",
    "AdapterRegistryError",
    "AnalysisProfile",
    "EngineAdapter",
    "EngineEvent",
    "EngineExecutionState",
    "EngineInput",
    "EngineLock",
    "EngineLockError",
    "EnginePin",
    "EngineResult",
    "EngineRunRef",
    "EngineTerminalStateValue",
    "ExecutionStateValue",
    "InvalidEngineTransition",
    "ResourceProfile",
    "SubmitConfig",
    "load_engine_lock",
    "transition_engine_state",
]
```

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-contracts-green \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_states.py -q
git diff --check
git add \
  services/api/src/perfpilot_api/engines \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_states.py
git diff --cached --check
git commit -m "feat: define external engine adapter protocol"
git push origin HEAD:main
```

Expected: all focused tests pass and the push is fast-forward.

## Task 4: Persist team workspaces and engine executions

**Files:**

- Create: `services/api/src/perfpilot_api/db/control/models/engines.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/__init__.py`
- Create: `services/api/migrations/control/versions/0004_external_engine_foundation.py`
- Modify: `services/api/tests/integration/test_migrations.py`
- Modify: `services/api/tests/integration/test_analysis_repository.py`

- [ ] **Step 1: Write failing migration assertions**

Extend the control table expectations in `services/api/tests/integration/test_migrations.py` with:

```python
"team_engine_workspaces",
"engine_executions",
```

Add assertions for these columns and constraints:

```python
assert {
    "id",
    "team_id",
    "engine_id",
    "external_workspace_id",
    "state",
    "version",
    "created_at",
    "updated_at",
}.issubset(control_columns["team_engine_workspaces"])

assert {
    "id",
    "analysis_id",
    "team_id",
    "engine_id",
    "attempt_number",
    "adapter_version",
    "engine_commit_sha",
    "engine_image_digest",
    "input_manifest_hash",
    "config_hash",
    "external_workspace_id",
    "external_session_id",
    "external_run_id",
    "state",
    "last_event_cursor",
    "stable_error_code",
    "started_at",
    "completed_at",
    "raw_result_artifact_id",
    "normalized_report_version_id",
    "version",
    "created_at",
    "updated_at",
}.issubset(control_columns["engine_executions"])
```

- [ ] **Step 2: Write failing tenant-alignment and uniqueness tests**

Extend `services/api/tests/integration/test_analysis_repository.py`. Import `EngineExecution` and `TeamEngineWorkspace` from `perfpilot_api.db.control.models`, then add:

```python
@pytest.mark.asyncio
async def test_engine_execution_requires_analysis_team_alignment(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)
    with pytest.raises(IntegrityError):
        async with analysis_databases.control_sessions.begin() as session:
            session.add(
                EngineExecution(
                    analysis_id=ANALYSIS_ID,
                    team_id=OTHER_TEAM_ID,
                    engine_id="smartperfetto",
                    attempt_number=1,
                    adapter_version="1.0.0",
                    engine_commit_sha="1" * 40,
                    engine_image_digest="sha256:" + "2" * 64,
                    input_manifest_hash="3" * 64,
                    config_hash="4" * 64,
                    state="pending",
                )
            )
            await session.flush()


@pytest.mark.asyncio
async def test_one_team_has_one_workspace_per_engine(
    analysis_databases: AnalysisDatabases,
) -> None:
    with pytest.raises(IntegrityError):
        async with analysis_databases.control_sessions.begin() as session:
            session.add_all(
                [
                    TeamEngineWorkspace(
                        team_id=TEAM_ID,
                        engine_id="smartperfetto",
                        external_workspace_id="opaque-workspace-1",
                        state="active",
                    ),
                    TeamEngineWorkspace(
                        team_id=TEAM_ID,
                        engine_id="smartperfetto",
                        external_workspace_id="opaque-workspace-2",
                        state="active",
                    ),
                ]
            )
            await session.flush()
```

Add validation and attempt-uniqueness coverage:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "unknown"},
        {"engine_commit_sha": "short"},
        {"engine_image_digest": "smartperfetto:v1.0.38"},
        {"input_manifest_hash": "short"},
        {"config_hash": "short"},
    ],
)
async def test_engine_execution_rejects_invalid_authority_fields(
    analysis_databases: AnalysisDatabases,
    overrides: dict[str, object],
) -> None:
    await _complete_creation(analysis_databases)
    values: dict[str, object] = {
        "analysis_id": ANALYSIS_ID,
        "team_id": TEAM_ID,
        "engine_id": "smartperfetto",
        "attempt_number": 1,
        "adapter_version": "1.0.0",
        "engine_commit_sha": "1" * 40,
        "engine_image_digest": "sha256:" + "2" * 64,
        "input_manifest_hash": "3" * 64,
        "config_hash": "4" * 64,
        "state": "pending",
    }
    values.update(overrides)
    with pytest.raises(IntegrityError):
        async with analysis_databases.control_sessions.begin() as session:
            session.add(EngineExecution(**values))
            await session.flush()


@pytest.mark.asyncio
async def test_engine_execution_attempt_is_unique(
    analysis_databases: AnalysisDatabases,
) -> None:
    await _complete_creation(analysis_databases)
    common = {
        "analysis_id": ANALYSIS_ID,
        "team_id": TEAM_ID,
        "engine_id": "smartperfetto",
        "attempt_number": 1,
        "adapter_version": "1.0.0",
        "engine_commit_sha": "1" * 40,
        "engine_image_digest": "sha256:" + "2" * 64,
        "input_manifest_hash": "3" * 64,
        "config_hash": "4" * 64,
        "state": "pending",
    }
    with pytest.raises(IntegrityError):
        async with analysis_databases.control_sessions.begin() as session:
            session.add_all((EngineExecution(**common), EngineExecution(**common)))
            await session.flush()
```

Add this downgrade preflight test to `test_migrations.py`; the existing empty round-trip test continues to prove that a data-free downgrade succeeds:

```python
def test_control_engine_downgrade_refuses_to_drop_workspace_metadata(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    team_id = UUID("91000000-0000-4000-8000-000000000011")
    workspace_id = UUID("92000000-0000-4000-8000-000000000011")
    with migration_databases.control_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO teams (id, name, state) VALUES (:id, 'Engine', 'active')"),
            {"id": team_id},
        )
        connection.execute(
            text(
                "INSERT INTO team_engine_workspaces "
                "(id, team_id, engine_id, external_workspace_id, state) "
                "VALUES (:id, :team_id, 'smartperfetto', 'opaque-workspace', 'active')"
            ),
            {"id": workspace_id, "team_id": team_id},
        )

    with pytest.raises(RuntimeError, match="engine metadata must be exported"):
        command.downgrade(
            _alembic_config("control", migration_databases.control_url),
            "0003_analysis_orchestration",
        )

    assert "team_engine_workspaces" in inspect(
        migration_databases.control_engine
    ).get_table_names()
```

- [ ] **Step 3: Run RED**

Run:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-models-red \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q
```

Expected: FAIL because the models and revision `0004_external_engine_foundation` do not exist.

- [ ] **Step 4: Implement the two control-plane models**

Create `services/api/src/perfpilot_api/db/control/models/engines.py`. Both models use the existing `UUIDPrimaryKeyMixin`, `TimestampMixin`, and `VersionedMixin`:

```python
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from perfpilot_api.db.base import (
    ControlBase,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
)


class TeamEngineWorkspace(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, ControlBase):
    __tablename__ = "team_engine_workspaces"
    __table_args__ = (
        UniqueConstraint("team_id", "engine_id", name="uq_team_engine_workspaces_team_engine"),
        UniqueConstraint(
            "engine_id",
            "external_workspace_id",
            name="uq_team_engine_workspaces_external",
        ),
        CheckConstraint(
            "state IN ('provisioning', 'active', 'deleting', 'deleted', 'failed')",
            name="ck_team_engine_workspaces_state",
        ),
        CheckConstraint("version > 0", name="ck_team_engine_workspaces_version"),
    )

    team_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_workspace_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class EngineExecution(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, ControlBase):
    __tablename__ = "engine_executions"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "engine_id",
            "attempt_number",
            name="uq_engine_executions_analysis_engine_attempt",
        ),
        ForeignKeyConstraint(
            ("analysis_id", "team_id"),
            ("global_jobs.id", "global_jobs.team_id"),
            ondelete="CASCADE",
            name="fk_engine_executions_analysis_team",
        ),
        CheckConstraint("attempt_number > 0", name="ck_engine_executions_attempt"),
        CheckConstraint(
            "state IN ('pending', 'running', 'awaiting_user', 'completed', "
            "'insufficient_data', 'failed', 'canceled')",
            name="ck_engine_executions_state",
        ),
        CheckConstraint(
            "engine_commit_sha ~ '^[a-f0-9]{40}$'",
            name="ck_engine_executions_commit",
        ),
        CheckConstraint(
            "engine_image_digest ~ '^sha256:[a-f0-9]{64}$'",
            name="ck_engine_executions_digest",
        ),
        CheckConstraint(
            "input_manifest_hash ~ '^[a-f0-9]{64}$' AND "
            "config_hash ~ '^[a-f0-9]{64}$'",
            name="ck_engine_executions_hashes",
        ),
        CheckConstraint("version > 0", name="ck_engine_executions_version"),
        Index("ix_engine_executions_state_created", "state", "created_at"),
        Index("ix_engine_executions_team_analysis", "team_id", "analysis_id"),
    )

    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    team_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    engine_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    input_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_workspace_id: Mapped[str | None] = mapped_column(String(255))
    external_session_id: Mapped[str | None] = mapped_column(String(255))
    external_run_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    last_event_cursor: Mapped[str | None] = mapped_column(String(255))
    stable_error_code: Mapped[str | None] = mapped_column(String(96))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_result_artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    normalized_report_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
```

Add a named `UniqueConstraint("id", "team_id", name="uq_global_jobs_id_team")` to `GlobalJob.__table_args__` so PostgreSQL can enforce the composite engine-execution foreign key. Export both new models from `db/control/models/__init__.py`.

- [ ] **Step 5: Create the matching Alembic revision**

Create `0004_external_engine_foundation.py`:

```python
"""Add external engine workspace and execution authority.

Revision ID: 0004_external_engine_foundation
Revises: 0003_analysis_orchestration
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_external_engine_foundation"
down_revision: str | None = "0003_analysis_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _record_columns() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    ]


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_global_jobs_id_team",
        "global_jobs",
        ["id", "team_id"],
    )
    op.create_table(
        "team_engine_workspaces",
        *_record_columns(),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine_id", sa.String(length=64), nullable=False),
        sa.Column("external_workspace_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "state IN ('provisioning', 'active', 'deleting', 'deleted', 'failed')",
            name="ck_team_engine_workspaces_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_team_engine_workspaces_version"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_team_engine_workspaces_team",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_team_engine_workspaces"),
        sa.UniqueConstraint(
            "team_id",
            "engine_id",
            name="uq_team_engine_workspaces_team_engine",
        ),
        sa.UniqueConstraint(
            "engine_id",
            "external_workspace_id",
            name="uq_team_engine_workspaces_external",
        ),
    )
    op.create_table(
        "engine_executions",
        *_record_columns(),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("engine_commit_sha", sa.String(length=40), nullable=False),
        sa.Column("engine_image_digest", sa.String(length=71), nullable=False),
        sa.Column("input_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("external_workspace_id", sa.String(length=255), nullable=True),
        sa.Column("external_session_id", sa.String(length=255), nullable=True),
        sa.Column("external_run_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("last_event_cursor", sa.String(length=255), nullable=True),
        sa.Column("stable_error_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_result_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "normalized_report_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_engine_executions_attempt"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'awaiting_user', 'completed', "
            "'insufficient_data', 'failed', 'canceled')",
            name="ck_engine_executions_state",
        ),
        sa.CheckConstraint(
            "engine_commit_sha ~ '^[a-f0-9]{40}$'",
            name="ck_engine_executions_commit",
        ),
        sa.CheckConstraint(
            "engine_image_digest ~ '^sha256:[a-f0-9]{64}$'",
            name="ck_engine_executions_digest",
        ),
        sa.CheckConstraint(
            "input_manifest_hash ~ '^[a-f0-9]{64}$' AND "
            "config_hash ~ '^[a-f0-9]{64}$'",
            name="ck_engine_executions_hashes",
        ),
        sa.CheckConstraint("version > 0", name="ck_engine_executions_version"),
        sa.ForeignKeyConstraint(
            ["analysis_id", "team_id"],
            ["global_jobs.id", "global_jobs.team_id"],
            name="fk_engine_executions_analysis_team",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_engine_executions"),
        sa.UniqueConstraint(
            "analysis_id",
            "engine_id",
            "attempt_number",
            name="uq_engine_executions_analysis_engine_attempt",
        ),
    )
    op.create_index(
        "ix_engine_executions_state_created",
        "engine_executions",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_engine_executions_team_analysis",
        "engine_executions",
        ["team_id", "analysis_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM engine_executions "
            "UNION ALL SELECT 1 FROM team_engine_workspaces LIMIT 1"
        )
    ) is not None:
        raise RuntimeError(
            "external engine downgrade preflight failed: engine metadata must be exported"
        )
    op.drop_index("ix_engine_executions_team_analysis", table_name="engine_executions")
    op.drop_index("ix_engine_executions_state_created", table_name="engine_executions")
    op.drop_table("engine_executions")
    op.drop_table("team_engine_workspaces")
    op.drop_constraint("uq_global_jobs_id_team", "global_jobs", type_="unique")
```

- [ ] **Step 6: Run GREEN**

Run the Step 3 command again.

Expected: all migration and model tests pass against real PostgreSQL with no skips.

- [ ] **Step 7: Commit and push persistence foundation**

Run:

```bash
git diff --check
git add \
  services/api/migrations/control/versions/0004_external_engine_foundation.py \
  services/api/src/perfpilot_api/db/control/models/engines.py \
  services/api/src/perfpilot_api/db/control/models/jobs.py \
  services/api/src/perfpilot_api/db/control/models/__init__.py \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py
git diff --cached --check
git commit -m "feat: persist external engine executions"
git push origin HEAD:main
```

Expected: fast-forward push. No engine raw result or customer content is present in the control database.

## Task 5: Run the packet gate and record the next boundary

**Files:**

- Modify only if a command is wrong: `docs/superpowers/plans/2026-07-28-perfpilot-engine-control-plane-foundation.md`
- Do not create an empty source commit.

- [ ] **Step 1: Run formatting and focused tests**

Run:

```bash
git diff --check
env PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-foundation-focused \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_lock.py \
  services/api/tests/unit/test_engine_contracts.py \
  services/api/tests/unit/test_engine_states.py -q
```

Expected: all focused unit tests pass.

- [ ] **Step 2: Run PostgreSQL migrations and model tests**

Run:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-foundation-pg \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider \
  services/api/tests/integration/test_analysis_repository.py \
  services/api/tests/integration/test_migrations.py -q
```

Expected: pass with no skips.

- [ ] **Step 3: Run the full backend regression**

Run:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  UV_CACHE_DIR=/private/tmp/perfpilot-engine-foundation-all \
  /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api \
  pytest -p no:cacheprovider services/api/tests -q
```

Expected: the full suite passes. Any unrelated failure stops the packet; do not weaken or skip it.

- [ ] **Step 4: Verify the exact remote SHA**

Run:

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
git status --short
```

Expected: ancestor check exits `0`, push reports up to date, SHAs match, and only `.superpowers/` may remain untracked. Do not stage `.superpowers/`.

- [ ] **Step 5: Prepare the next implementation plan**

The next plan is `docs/superpowers/plans/2026-07-28-perfpilot-smartperfetto-adapter.md`. It begins only after this packet is green and pushed. Its first RED tests cover workspace provisioning, Trace upload, analyze submission, `Last-Event-ID` SSE replay, status recovery, cancel, report fetch, quota mapping, and opaque-ID tenant ownership.

## Packet acceptance

The packet is complete only when all of these are true:

- Task 7 is one isolated, tested, pushed commit.
- Production FastAPI cannot execute the owned in-process APK inspector.
- The checked-in engine lock accepts only reviewed commits and valid digests.
- Production mode rejects null image digests.
- Duplicate Adapter IDs and illegal execution transitions fail deterministically.
- PostgreSQL prevents cross-team execution rows and duplicate workspace mappings.
- The control database contains no raw engine payloads or customer evidence.
- All focused, migration, PostgreSQL, and full API tests pass.
- Every source-changing task is independently committed and fast-forward pushed to `origin/main`.
