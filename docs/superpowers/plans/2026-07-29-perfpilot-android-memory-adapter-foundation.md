# PerfPilot Android Memory Adapter Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the production-facing Android Memory foundation that accepts tenant-scoped multi-Artifact captures, runs the pinned `ai-context` contract through an isolated worker boundary, and persists a validated `android-memory-ai-context-1.2` result.

**Architecture:** PerfPilot creates a `memory_upload` Analysis bound to an existing ApplicationVersion, stores each evidence file independently, and generates one server-owned `memory_capture_manifest` per capture stage. A host stager downloads and verifies the manifest and its referenced Artifacts, then an Android Memory adapter delegates the staged directory to an injected isolated worker backend. The existing engine execution service persists attempts, retries recoverable worker failures, sinks the result before the terminal state, and never provisions a SmartPerfetto workspace for the isolated engine.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 17, httpx, asyncio subprocess APIs, pytest, Ruff, S3-compatible object storage, Android-App-Memory-Analysis `d5514972ced78c3faa7fc17589c1ea9231645056`.

---

## Scope

This plan implements packages 1–4 from the approved design:

1. Manifest, Artifact types, and `memory_upload` mode.
2. Host Stager and isolated Worker Runner boundary.
3. Android Memory Adapter and the real pinned-upstream contract test.
4. Engine execution integration, retry, cancellation, and recovery.

This plan ends when one capture stage produces a durable raw `android-memory-ai-context-1.2` Artifact. HPROF, Panorama, Diff, Report Normalizer, and PerfPilot AI are separate implementation plans because each subsystem has an independent contract and failure policy.

## File map

### New files

- `services/api/migrations/control/versions/0005_memory_upload_mode.py`: expands the control-plane Analysis mode constraint.
- `services/api/migrations/tenant/versions/0004_memory_upload_mode.py`: expands the tenant Analysis mode and stores the tenant-private question.
- `services/api/src/perfpilot_api/engines/android_memory_contracts.py`: canonical Manifest and upstream output models.
- `services/api/src/perfpilot_api/services/memory_analyses.py`: memory Analysis creation, capture validation, and Manifest orchestration.
- `services/api/src/perfpilot_api/services/internal_artifacts.py`: server-owned immutable JSON Artifact writer.
- `services/api/src/perfpilot_api/api/memory_captures.py`: capture creation endpoint.
- `services/api/src/perfpilot_api/engines/android_memory_stager.py`: bounded download, integrity verification, and safe directory materialization.
- `services/api/src/perfpilot_api/engines/android_memory_worker.py`: Worker protocol plus local and hardened OCI implementations.
- `infra/engines/android-memory/Dockerfile`: digest-built, non-root Android Memory runtime image.
- `services/api/src/perfpilot_api/engines/android_memory.py`: EngineAdapter implementation.
- `services/api/src/perfpilot_api/services/memory_executions.py`: tenant-scoped EngineInput claim resolution and execution preparation.
- `services/api/tests/unit/test_android_memory_contracts.py`: Manifest and output contract tests.
- `services/api/tests/unit/test_memory_analysis_service.py`: tenant binding and Manifest service tests.
- `services/api/tests/unit/test_internal_artifacts.py`: immutable internal Artifact writer tests.
- `services/api/tests/unit/test_android_memory_stager.py`: download and filesystem safety tests.
- `services/api/tests/unit/test_android_memory_worker.py`: command, timeout, cancellation, and result-store tests.
- `services/api/tests/unit/test_android_memory_adapter.py`: Adapter state and privacy tests.
- `services/api/tests/unit/test_memory_execution_service.py`: claim resolution and execution preparation tests.
- `services/api/tests/integration/test_memory_analysis_api.py`: authenticated manual memory workflow.
- `services/api/tests/contract/test_android_memory_upstream.py`: real pinned-checkout contract test.
- `services/api/tests/fixtures/android_memory/minimal_meminfo.txt`: stable upstream input.

### Existing files to modify

- `services/api/src/perfpilot_api/db/control/models/jobs.py:33`: allow `memory_upload`.
- `services/api/src/perfpilot_api/db/tenant/models/apps.py:147`: allow `memory_upload` and add `question`.
- `services/api/src/perfpilot_api/services/uploads.py:34`: allow `memory_evidence` and `screenshot`, but reject `memory_capture_manifest` from public uploads.
- `services/api/src/perfpilot_api/api/analyses.py:42`: add the discriminated memory Analysis request and response branch.
- `services/api/src/perfpilot_api/services/analyses.py:212`: add the memory Analysis view and repository creation path without weakening device Analysis invariants.
- `services/api/src/perfpilot_api/engines/contracts.py:17`: add `execution_id` to `SubmitConfig`.
- `services/api/src/perfpilot_api/services/engine_executions.py:673`: branch workspace handling by resource profile and finalize `insufficient_data` results.
- `services/api/src/perfpilot_api/config.py:155`: add bounded Android Memory development settings.
- `services/api/src/perfpilot_api/main.py:145`: inject the memory services and include the capture router.
- `services/api/src/perfpilot_api/engines/__init__.py`: export the new stable contracts.
- `services/api/tests/integration/test_migrations.py`: verify both migration trees and downgrade guards.
- `services/api/tests/unit/test_upload_service.py`: cover the public Artifact allowlist.
- `services/api/tests/unit/test_analysis_service.py`: cover `memory_upload` creation and idempotency.
- `services/api/tests/integration/test_analysis_api.py`: cover the new Analysis request and response.
- `services/api/tests/unit/test_engine_contracts.py`: cover `SubmitConfig.execution_id`.
- `services/api/tests/unit/test_engine_execution_service.py`: cover isolated-worker submission and retry semantics.
- `services/api/tests/unit/test_app.py`: cover dependency injection and production isolation guards.

## Task 1: Add the `memory_upload` persistence contract

**Files:**

- Create: `services/api/migrations/control/versions/0005_memory_upload_mode.py`
- Create: `services/api/migrations/tenant/versions/0004_memory_upload_mode.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/jobs.py:33-39`
- Modify: `services/api/src/perfpilot_api/db/tenant/models/apps.py:147-188`
- Modify: `services/api/tests/integration/test_migrations.py`

- [ ] **Step 1: Write failing migration tests**

Add assertions that both `analysis_mode` checks contain `memory_upload`, that the tenant table has a nullable `question` column, and that each downgrade refuses to remove the mode while matching rows exist:

```python
def test_memory_upload_mode_is_present_in_both_databases(
    migration_databases: MigrationDatabases,
) -> None:
    _upgrade("control", migration_databases.control_url)
    _upgrade("tenant", migration_databases.tenant_url)

    control_checks = {
        item["name"]: item["sqltext"]
        for item in inspect(migration_databases.control_engine).get_check_constraints(
            "global_jobs"
        )
    }
    tenant_checks = {
        item["name"]: item["sqltext"]
        for item in inspect(migration_databases.tenant_engine).get_check_constraints(
            "analyses"
        )
    }

    assert "memory_upload" in control_checks["ck_global_jobs_analysis_mode"]
    assert "memory_upload" in tenant_checks["ck_analyses_mode"]
    tenant_columns = {
        item["name"]: item
        for item in inspect(migration_databases.tenant_engine).get_columns("analyses")
    }
    assert tenant_columns["question"]["nullable"] is True
    assert tenant_columns["question"]["type"].length == 2000
```

- [ ] **Step 2: Run the migration test and confirm the red state**

Run:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/integration/test_migrations.py::test_memory_upload_mode_is_present_in_both_databases -q
```

Expected: FAIL because `memory_upload` and `analyses.question` do not exist.

- [ ] **Step 3: Add both Alembic revisions**

The control upgrade must replace the existing constraint:

```python
def upgrade() -> None:
    op.drop_constraint("ck_global_jobs_analysis_mode", "global_jobs", type_="check")
    op.create_check_constraint(
        "ck_global_jobs_analysis_mode",
        "global_jobs",
        "analysis_mode IN ('device', 'trace_upload', 'memory_upload')",
    )
```

The tenant upgrade must add the private question and replace the mode constraint:

```python
def upgrade() -> None:
    op.drop_constraint("ck_analyses_mode", "analyses", type_="check")
    op.create_check_constraint(
        "ck_analyses_mode",
        "analyses",
        "analysis_mode IN ('device', 'trace_upload', 'memory_upload')",
    )
    op.add_column("analyses", sa.Column("question", sa.String(length=2000), nullable=True))
```

Each downgrade must lock the table, reject existing `memory_upload` rows, restore the old constraint, and drop `question` only in the tenant tree. Use the stable message `memory upload downgrade preflight failed`.

- [ ] **Step 4: Align SQLAlchemy models**

Use the same check expressions in `GlobalJob` and `Analysis`, then add:

```python
question: Mapped[str | None] = mapped_column(String(2000))
```

- [ ] **Step 5: Run migration and metadata drift tests**

Run:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/integration/test_migrations.py -q
```

Expected: all migration tests PASS.

- [ ] **Step 6: Commit the persistence contract**

```bash
git add services/api/migrations/control/versions/0005_memory_upload_mode.py services/api/migrations/tenant/versions/0004_memory_upload_mode.py services/api/src/perfpilot_api/db/control/models/jobs.py services/api/src/perfpilot_api/db/tenant/models/apps.py services/api/tests/integration/test_migrations.py
git commit -m "feat: add memory upload analysis mode"
git push -u origin HEAD
```

## Task 2: Define the canonical Capture Manifest and upstream result models

**Files:**

- Create: `services/api/src/perfpilot_api/engines/android_memory_contracts.py`
- Create: `services/api/tests/unit/test_android_memory_contracts.py`
- Modify: `services/api/src/perfpilot_api/engines/__init__.py`

- [ ] **Step 1: Write failing contract tests**

Cover canonical serialization, duplicate Artifact IDs, singleton roles, naive timestamps, extra fields, and output privacy:

```python
def test_manifest_is_canonical_and_hash_stable() -> None:
    manifest = MemoryCaptureManifest.model_validate(
        {
            "schema_version": "1.0",
            "analysis_id": "e2000000-0000-4000-8000-000000000001",
            "capture_id": "e3000000-0000-4000-8000-000000000001",
            "phase": "before",
            "source": "manual_upload",
            "captured_at": None,
            "subject": {"package": "com.example.app", "android_sdk": 37},
            "artifacts": [
                {
                    "artifact_id": "e4000000-0000-4000-8000-000000000001",
                    "role": "meminfo",
                }
            ],
        }
    )

    assert manifest.canonical_bytes() == (
        b'{"analysis_id":"e2000000-0000-4000-8000-000000000001",'
        b'"artifacts":[{"artifact_id":"e4000000-0000-4000-8000-000000000001",'
        b'"role":"meminfo"}],"capture_id":"e3000000-0000-4000-8000-000000000001",'
        b'"phase":"before","schema_version":"1.0","source":"manual_upload",'
        b'"subject":{"android_sdk":37,"package":"com.example.app"}}'
    )
    assert len(manifest.sha256_hex()) == 64
```

```python
def test_upstream_context_rejects_local_paths() -> None:
    payload = valid_context_payload()
    payload["analysis_contract"]["privacy"]["local_paths_included"] = True

    with pytest.raises(ValidationError):
        AndroidMemoryContext.model_validate(payload)
```

- [ ] **Step 2: Run the contract tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_android_memory_contracts.py -q
```

Expected: collection FAIL because `android_memory_contracts` does not exist.

- [ ] **Step 3: Implement the frozen Pydantic models**

Define the full role union from the approved spec and use this validation core:

```python
_SINGLETON_ROLES = frozenset(
    {
        "meminfo",
        "smaps",
        "showmap",
        "hprof",
        "gfxinfo",
        "proc_meminfo",
        "pressure_memory",
        "zram",
        "dmabuf",
        "exit_info",
        "perfetto_trace",
        "native_heap_profile",
        "phase_metadata",
        "device_context",
    }
)


class MemoryCaptureManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    analysis_id: UUID
    capture_id: UUID
    phase: Literal["single", "before", "after", "cooldown"]
    source: Literal["manual_upload", "adb_agent"]
    captured_at: datetime | None = None
    subject: MemorySubject
    artifacts: tuple[MemoryArtifactRef, ...] = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_relationships(self) -> "MemoryCaptureManifest":
        ids = [item.artifact_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest artifact ids must be unique")
        roles = [item.role for item in self.artifacts if item.role in _SINGLETON_ROLES]
        if len(roles) != len(set(roles)):
            raise ValueError("manifest singleton roles must be unique")
        if self.captured_at is not None and self.captured_at.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be UTC")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def sha256_hex(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
```

`AndroidMemoryContext` must require `context_type="android-memory-ai-context"`, `schema_version="1.2"`, generator name/version `android-memory-ai`/`1.2.0`, and `local_paths_included=False`. Keep unknown upstream fields, because minor additions within the pinned schema must survive raw result persistence.

- [ ] **Step 4: Run the contract tests**

Run the command from Step 2.

Expected: all Android Memory contract tests PASS.

- [ ] **Step 5: Commit the contracts**

```bash
git add services/api/src/perfpilot_api/engines/android_memory_contracts.py services/api/src/perfpilot_api/engines/__init__.py services/api/tests/unit/test_android_memory_contracts.py
git commit -m "feat: define Android memory contracts"
git push -u origin HEAD
```

## Task 3: Open only the intended public Artifact types

**Files:**

- Modify: `services/api/src/perfpilot_api/services/uploads.py:34-44`
- Modify: `services/api/tests/unit/test_upload_service.py`
- Modify: `services/api/tests/integration/test_upload_api.py`

- [ ] **Step 1: Write failing allowlist tests**

Add parameterized tests for `memory_evidence` and `screenshot`, plus a rejection test for the server-only kind:

```python
@pytest.mark.parametrize("artifact_kind", ["memory_evidence", "screenshot"])
@pytest.mark.asyncio
async def test_memory_input_kinds_can_reserve_uploads(artifact_kind: str) -> None:
    service, repository, store = upload_service()

    slot = await service.create_slot(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key=f"memory-{artifact_kind}",
        artifact_kind=artifact_kind,
        mime="application/octet-stream",
        size=128,
        sha256_b64=SHA256_B64,
    )

    assert slot.artifact_kind == artifact_kind
    assert repository.reserved_descriptor.artifact_kind == artifact_kind
    assert store.put_authorizations == 1


@pytest.mark.asyncio
async def test_public_upload_rejects_server_manifest_kind() -> None:
    service, _, _ = upload_service()

    with pytest.raises(UploadInvalidRequestError):
        await service.create_slot(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            idempotency_key="server-manifest",
            artifact_kind="memory_capture_manifest",
            mime="application/json",
            size=128,
            sha256_b64=SHA256_B64,
        )
```

- [ ] **Step 2: Run the focused upload tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_upload_service.py services/api/tests/integration/test_upload_api.py -q
```

Expected: the acceptance cases FAIL with `UploadInvalidRequestError`.

- [ ] **Step 3: Extend the public allowlist minimally**

The final constant must contain the current values plus only these new values:

```python
_UPLOADABLE_KINDS = frozenset(
    {
        "apk",
        "capture_manifest",
        "log",
        "mapping",
        "memory_evidence",
        "native_symbols",
        "screenshot",
        "source_archive",
        "trace",
    }
)
```

Do not add `memory_capture_manifest`; Task 5 writes it through the internal Artifact path.

- [ ] **Step 4: Run the focused upload tests**

Run the command from Step 2.

Expected: all focused upload tests PASS.

- [ ] **Step 5: Commit the allowlist**

```bash
git add services/api/src/perfpilot_api/services/uploads.py services/api/tests/unit/test_upload_service.py services/api/tests/integration/test_upload_api.py
git commit -m "feat: accept Android memory evidence uploads"
git push -u origin HEAD
```

## Task 4: Create tenant-bound manual memory Analyses

**Files:**

- Modify: `services/api/src/perfpilot_api/services/analyses.py:212-249,647-860`
- Modify: `services/api/src/perfpilot_api/api/analyses.py:31-64,207-287`
- Modify: `services/api/tests/unit/test_analysis_service.py`
- Modify: `services/api/tests/integration/test_analysis_api.py`

- [ ] **Step 1: Write failing service tests**

Test creation, idempotent replay, application-version ownership, and queue isolation:

```python
@pytest.mark.asyncio
async def test_create_memory_analysis_binds_existing_application_version() -> None:
    service, repository = memory_analysis_service()

    view = await service.create_memory_analysis(
        team_id=TEAM_ID,
        requested_by_user_id=USER_ID,
        idempotency_key="memory-analysis-1",
        application_version_id=APPLICATION_VERSION_ID,
        question="退出页面后内存没有下降",
    )

    assert view.analysis_mode == "memory_upload"
    assert view.application_version_id == APPLICATION_VERSION_ID
    assert view.question == "退出页面后内存没有下降"
    assert view.state == "created"
    assert repository.created_modes == ["memory_upload"]
```

Add a negative test where the tenant repository cannot find `APPLICATION_VERSION_ID`; expect `AnalysisNotFoundError` without exposing another team.

- [ ] **Step 2: Run focused service tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_analysis_service.py -q
```

Expected: FAIL because `create_memory_analysis` and the memory view do not exist.

- [ ] **Step 3: Add the service and repository creation path**

Add a mode-neutral reservation helper, but preserve device-only quota accounting. The new public method must have this exact boundary:

```python
async def create_memory_analysis(
    self,
    *,
    team_id: UUID,
    requested_by_user_id: UUID,
    idempotency_key: str,
    application_version_id: UUID,
    question: str | None,
) -> MemoryAnalysisView:
    normalized_question = question.strip() if question is not None else None
    if normalized_question == "":
        normalized_question = None
    if normalized_question is not None and len(normalized_question) > 2_000:
        raise AnalysisInvalidRequestError("analysis request is invalid")
    request_hash = canonical_memory_analysis_request_hash(
        application_version_id=application_version_id,
        question=normalized_question,
    )
    return await self._repository.create_memory_analysis(
        team_id=team_id,
        requested_by_user_id=requested_by_user_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        candidate_analysis_id=self._uuid_source(),
        application_version_id=application_version_id,
        question=normalized_question,
        now=self._clock(),
    )
```

The SQLAlchemy repository must create the control `GlobalJob` with `analysis_mode="memory_upload"` and the tenant `Analysis` with the selected ApplicationVersion in their own transactions. On replay, it must verify mode, team, idempotency key, request hash, and tenant parent. It must not create ScenarioJob or APK upload rows.

- [ ] **Step 4: Add the discriminated API request**

Preserve the current device model and add:

```python
class CreateMemoryAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    analysis_mode: Literal["memory_upload"]
    application_version_id: UUID
    question: str | None = Field(default=None, max_length=2000)


CreateAnalysisRequest = Annotated[
    CreateDeviceAnalysisRequest | CreateMemoryAnalysisRequest,
    Field(discriminator="analysis_mode"),
]
```

Branch in `create_analysis`: call the existing device path for `device`, and call `create_memory_analysis` for `memory_upload`. The memory response must return `apk_upload=None`, `scenarios=[]`, the selected `application_version_id`, and the tenant-private question. Keep `cache-control: no-store`.

- [ ] **Step 5: Add authenticated API tests**

Use the existing proxy signature, session, CSRF, and team fixtures. Assert `201`, `analysis_mode="memory_upload"`, no upload URL in the response, idempotent replay, and `404 resource_not_found` for another team's ApplicationVersion.

- [ ] **Step 6: Run service and API tests**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_analysis_service.py services/api/tests/integration/test_analysis_api.py -q
```

Expected: all focused Analysis tests PASS.

- [ ] **Step 7: Commit manual memory Analysis creation**

```bash
git add services/api/src/perfpilot_api/services/analyses.py services/api/src/perfpilot_api/api/analyses.py services/api/tests/unit/test_analysis_service.py services/api/tests/integration/test_analysis_api.py
git commit -m "feat: create manual memory analyses"
git push -u origin HEAD
```

## Task 5: Generate a server-owned immutable Manifest Artifact

**Files:**

- Create: `services/api/src/perfpilot_api/services/internal_artifacts.py`
- Create: `services/api/src/perfpilot_api/services/memory_analyses.py`
- Create: `services/api/src/perfpilot_api/api/memory_captures.py`
- Create: `services/api/tests/unit/test_internal_artifacts.py`
- Create: `services/api/tests/unit/test_memory_analysis_service.py`
- Create: `services/api/tests/integration/test_memory_analysis_api.py`
- Modify: `services/api/src/perfpilot_api/main.py:145-297`

- [ ] **Step 1: Write failing Manifest ownership tests**

The service test must prove that every referenced Artifact belongs to the authenticated tenant and Analysis:

```python
@pytest.mark.asyncio
async def test_create_capture_rebinds_artifacts_and_writes_canonical_manifest() -> None:
    repository = FakeMemoryCaptureRepository(
        analysis=memory_analysis(),
        artifacts=(finalized_artifact(ARTIFACT_ID, kind="memory_evidence"),),
    )
    sink = FakeInternalArtifactSink()
    service = MemoryCaptureService(repository=repository, manifest_sink=sink, uuid_source=lambda: CAPTURE_ID)

    created = await service.create_capture(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        phase="single",
        source="manual_upload",
        captured_at=None,
        subject=MemorySubject(package="com.example.app", android_sdk=37),
        artifacts=(MemoryArtifactRef(artifact_id=ARTIFACT_ID, role="meminfo"),),
    )

    assert created.manifest.capture_id == CAPTURE_ID
    assert created.artifact_id == manifest_artifact_id(CAPTURE_ID)
    assert sink.payload == created.manifest.canonical_bytes()
    assert b"bucket" not in sink.payload
    assert b"object_key" not in sink.payload
```

Add tests for another Analysis, expired/unfinalized Artifact, duplicate IDs, disallowed Artifact kind, package mismatch recording, and `memory_capture_manifest` public upload rejection.

The capture is addressed without a new mutable capture table: `capture_id` deterministically yields the Manifest Artifact ID. A replay must load that Artifact, parse its canonical bytes, and verify that its embedded `analysis_id` and `capture_id` still match the authenticated route.

- [ ] **Step 2: Run new tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_internal_artifacts.py services/api/tests/unit/test_memory_analysis_service.py -q
```

Expected: collection FAIL because the services do not exist.

- [ ] **Step 3: Implement the internal JSON Artifact sink**

Define a narrow protocol and deterministic ID:

```python
_MEMORY_MANIFEST_NAMESPACE = UUID("3fce5d93-30fd-5ac5-9f62-c1a89f78cd83")


def manifest_artifact_id(capture_id: UUID) -> UUID:
    return uuid5(_MEMORY_MANIFEST_NAMESPACE, str(capture_id))


class InternalArtifactSink(Protocol):
    async def write_json(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        artifact_id: UUID,
        artifact_kind: Literal["memory_capture_manifest"],
        payload: bytes,
    ) -> UUID:
        raise NotImplementedError
```

The S3 implementation must compute SHA-256, write to `raw/analyses/{analysis_id}/internal/memory_capture_manifest/{artifact_id}`, require an immutable version ID, and finalize the tenant Artifact only after S3 confirms checksum, size, and MIME `application/json`. Replaying the same ID and bytes returns the same Artifact; different bytes raise a redacted idempotency conflict.

- [ ] **Step 4: Implement tenant-scoped capture validation**

`MemoryCaptureService.create_capture` must load the memory Analysis and all referenced Artifact rows through the routed tenant session, build `MemoryCaptureManifest`, write it through `InternalArtifactSink`, and return:

```python
@dataclass(frozen=True, slots=True)
class CreatedMemoryCapture:
    artifact_id: UUID
    manifest: MemoryCaptureManifest
    manifest_sha256: str
```

Return the same not-found error for an absent Artifact, another Analysis, another tenant, deleted data, and expired data. Do not include object metadata in exception text or repr.

- [ ] **Step 5: Add the capture endpoint**

Create `POST /v1/teams/{team_id}/analyses/{analysis_id}/memory-captures` with this strict request shape:

```python
class CreateMemoryCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    phase: Literal["single", "before", "after", "cooldown"]
    source: Literal["manual_upload"]
    captured_at: datetime | None = None
    subject: MemorySubject
    artifacts: tuple[MemoryArtifactRef, ...] = Field(min_length=1, max_length=2048)
```

The authenticated user can only use `source=manual_upload`; the future Agent claim API owns `adb_agent`. Return `201`, `capture_id`, `manifest_artifact_id`, `manifest_sha256`, and `state="created"`, with `cache-control: no-store`.

- [ ] **Step 6: Run unit and authenticated integration tests**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_internal_artifacts.py services/api/tests/unit/test_memory_analysis_service.py services/api/tests/integration/test_memory_analysis_api.py -q
```

Expected: all Manifest persistence and API tests PASS.

- [ ] **Step 7: Commit Manifest generation**

```bash
git add services/api/src/perfpilot_api/services/internal_artifacts.py services/api/src/perfpilot_api/services/memory_analyses.py services/api/src/perfpilot_api/api/memory_captures.py services/api/src/perfpilot_api/main.py services/api/tests/unit/test_internal_artifacts.py services/api/tests/unit/test_memory_analysis_service.py services/api/tests/integration/test_memory_analysis_api.py
git commit -m "feat: create tenant memory capture manifests"
git push -u origin HEAD
```

## Task 6: Materialize verified inputs in the Host Stager

**Files:**

- Create: `services/api/src/perfpilot_api/engines/android_memory_stager.py`
- Create: `services/api/tests/unit/test_android_memory_stager.py`

- [ ] **Step 1: Write failing staging tests**

Use `httpx.MockTransport` and a temporary root. Cover a successful manifest-first download, size mismatch, hash mismatch, redirects, total bytes, 2,048-file bound, duplicate inputs, missing references, and safe generated paths:

```python
@pytest.mark.asyncio
async def test_stager_materializes_only_manifest_references(tmp_path: Path) -> None:
    manifest_input, meminfo_input = engine_inputs()
    stager = AndroidMemoryStager(
        client=httpx.AsyncClient(transport=artifact_transport(), follow_redirects=False),
        workspace_root=tmp_path,
        max_files=2048,
        max_file_bytes=5 * 1024**3,
        max_total_bytes=8 * 1024**3,
    )

    staged = await stager.stage(
        run_id="memory-run-1",
        inputs=(manifest_input, meminfo_input),
    )
    try:
        files = sorted(path.relative_to(staged.input_dir).as_posix() for path in staged.input_dir.rglob("*") if path.is_file())
    finally:
        await staged.cleanup()

    assert files == [f"meminfo/meminfo-{meminfo_input.artifact_id}.txt"]
    assert staged.manifest.phase == "single"
```

- [ ] **Step 2: Run the Stager tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_android_memory_stager.py -q
```

Expected: collection FAIL because `AndroidMemoryStager` does not exist.

- [ ] **Step 3: Implement bounded streaming download**

Use one private method for the manifest and evidence paths:

```python
async def _download(self, source: EngineInput, destination: BinaryIO) -> None:
    digest = hashlib.sha256()
    size = 0
    response = await self._client.send(
        self._client.build_request("GET", source.download_url.get_secret_value()),
        stream=True,
        follow_redirects=False,
    )
    try:
        if response.status_code < 200 or response.status_code > 299:
            raise staging_error("download_failed", retryable=True)
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > source.size_bytes or size > self._max_file_bytes:
                raise staging_error("input_limit_exceeded")
            digest.update(chunk)
            destination.write(chunk)
    finally:
        await response.aclose()
    if size != source.size_bytes or not hmac.compare_digest(
        digest.digest(), base64.b64decode(source.sha256_b64, validate=True)
    ):
        raise staging_error("integrity_mismatch")
```

Parse the downloaded `memory_capture_manifest` with `MemoryCaptureManifest`, match every referenced UUID against exactly one supplied EngineInput, and ignore no extra input silently: reject unreferenced inputs with `manifest_invalid`. Generate paths only from role and UUID. Create regular files with exclusive mode and do not unpack archives.

Return a `StagedMemoryInput` containing the parsed Manifest, `input_dir`, and an idempotent async `cleanup()` callback. The Adapter transfers ownership to the Worker after `start` succeeds; the Worker invokes `cleanup()` when the process reaches a terminal state. If staging or Worker start fails, the Adapter invokes `cleanup()` itself. This ownership rule keeps the input directory alive for asynchronous execution without leaking it after completion.

- [ ] **Step 4: Run the Stager tests**

Run the command from Step 2.

Expected: all Stager tests PASS and no temporary execution directory remains.

- [ ] **Step 5: Commit the Host Stager**

```bash
git add services/api/src/perfpilot_api/engines/android_memory_stager.py services/api/tests/unit/test_android_memory_stager.py
git commit -m "feat: stage verified Android memory inputs"
git push -u origin HEAD
```

## Task 7: Add the isolated Worker backend boundary

**Files:**

- Create: `services/api/src/perfpilot_api/engines/android_memory_worker.py`
- Create: `services/api/tests/unit/test_android_memory_worker.py`
- Create: `infra/engines/android-memory/Dockerfile`
- Modify: `services/api/src/perfpilot_api/config.py:155-192`
- Modify: `services/api/tests/unit/test_security.py`

- [ ] **Step 1: Write failing Worker tests**

Cover the exact local argv, exact hardened OCI argv, no shell, output limit, exit `0/1/2`, timeout, cancellation, stderr redaction, and completed-run replay:

```python
@pytest.mark.asyncio
async def test_worker_uses_fixed_argv_and_strict_mode(tmp_path: Path) -> None:
    process_factory = FakeProcessFactory(exit_code=2, output=valid_context_bytes("insufficient"))
    worker = LocalAndroidMemoryWorker(
        python_binary=Path("/usr/bin/python3"),
        repository_root=Path("/opt/android-memory"),
        run_root=tmp_path,
        runtime_commit="d5514972ced78c3faa7fc17589c1ea9231645056",
        process_factory=process_factory,
        max_output_bytes=32 * 1024**2,
    )
    staged = fake_staged_input(tmp_path / "input")

    await worker.start(
        run_id="memory-run-1",
        staged=staged,
        question="Native memory keeps growing; $(touch /tmp/forbidden)",
        timeout_seconds=900,
    )
    await process_factory.completed.wait()
    result = await worker.result("memory-run-1")

    assert process_factory.argv == (
        "/usr/bin/python3",
        "/opt/android-memory/tools/ai_context.py",
        "--dump-dir",
        str(staged.input_dir),
        "--question",
        "Native memory keeps growing; $(touch /tmp/forbidden)",
        "--format",
        "json",
        "--strict",
        "--output",
        str(tmp_path / "memory-run-1" / "context.json"),
    )
    assert result.exit_code == 2
    assert staged.cleaned is True
```

- [ ] **Step 2: Run Worker tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_android_memory_worker.py -q
```

Expected: collection FAIL because the Worker module does not exist.

- [ ] **Step 3: Define the injected production boundary**

Use this protocol so the API process never assumes that a local subprocess is production isolation:

```python
@dataclass(frozen=True, slots=True)
class MemoryWorkerResult:
    exit_code: int
    payload: bytes | None


class AndroidMemoryWorker(Protocol):
    isolation: Literal["local", "oci"]

    async def start(
        self,
        *,
        run_id: str,
        staged: StagedMemoryInput,
        question: str | None,
        timeout_seconds: int,
    ) -> None:
        raise NotImplementedError

    async def status(self, run_id: str) -> Literal["running", "completed", "failed", "canceled", "lost"]:
        raise NotImplementedError

    async def result(self, run_id: str) -> MemoryWorkerResult:
        raise NotImplementedError

    async def cancel(self, run_id: str) -> None:
        raise NotImplementedError
```

`LocalAndroidMemoryWorker` sets `isolation="local"`. It exists only for tests and development, uses `asyncio.create_subprocess_exec`, bounds stderr without returning it, writes state atomically under `run_root`, replays completed results by `run_id`, and calls `staged.cleanup()` in the process task's `finally` block.

`OciAndroidMemoryWorker` sets `isolation="oci"` and uses the locked image digest. Construct its command as an argv tuple with no shell:

```python
argv = (
    str(container_runtime),
    "run",
    "--rm",
    "--name",
    run_id,
    "--network",
    "none",
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    str(pids_limit),
    "--memory",
    str(memory_bytes),
    "--cpus",
    str(cpu_limit),
    "--mount",
    f"type=bind,src={staged.input_dir},dst=/work/input,readonly",
    "--mount",
    f"type=bind,src={output_dir},dst=/work/output",
    "--tmpfs",
    f"/tmp:rw,noexec,nosuid,nodev,size={tmpfs_bytes}",
    image_reference,
    "--dump-dir",
    "/work/input",
    "--question",
    question or "",
    "--format",
    "json",
    "--strict",
    "--output",
    "/work/output/context.json",
)
```

On cancellation, invoke the runtime's `kill` command with the validated deterministic run ID, then await the original process. On restart, use runtime `inspect` to distinguish a live container from a lost run. Never pass environment variables, host networking, a writable root, privileged mode, or the Docker socket into the container.

Create the image file with a digest-supplied base and the upstream checkout as build context:

```dockerfile
ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

RUN groupadd --system --gid 65532 perfpilot && \
    useradd --system --uid 65532 --gid 65532 --no-create-home perfpilot
WORKDIR /opt/android-memory
COPY --chown=65532:65532 . /opt/android-memory
USER 65532:65532
ENTRYPOINT ["python3", "tools/ai_context.py"]
```

The release command must pass a base reference containing `@sha256:` and record the resulting Android Memory image digest in `engine-lock.yaml`. Do not build from an unpinned base.

- [ ] **Step 4: Add bounded development settings**

Add frozen settings for `android_memory_enabled`, absolute checkout path, absolute Python binary, absolute run root, 2,048 files, 5 GiB per file, 8 GiB total input, 32 MiB output, and 900-second timeout. In production, enabling Android Memory with the local backend configuration must raise the redacted production configuration error.

```python
android_memory_enabled: bool = False
android_memory_checkout_root: Path = Path("/Users/ray/Android-App-Memory-Analysis")
android_memory_python_binary: Path = Path("/usr/local/bin/python3")
android_memory_run_root: Path = Path(".perfpilot/android-memory-runs")
android_memory_container_runtime: Path = Path("/usr/bin/docker")
android_memory_max_files: int = Field(default=2048, ge=1, le=2048)
android_memory_max_file_bytes: int = Field(default=5 * 1024**3, gt=0)
android_memory_max_total_bytes: int = Field(default=8 * 1024**3, gt=0)
android_memory_max_output_bytes: int = Field(default=32 * 1024**2, gt=0)
android_memory_timeout_seconds: int = Field(default=900, ge=1, le=3600)
android_memory_cpu_limit: float = Field(default=4.0, gt=0, le=64, allow_inf_nan=False)
android_memory_memory_bytes: int = Field(default=8 * 1024**3, gt=0)
android_memory_pids_limit: int = Field(default=128, ge=16, le=4096)
android_memory_tmpfs_bytes: int = Field(default=1024**3, gt=0)
```

Validate that total bytes are not smaller than per-file bytes. When the feature is enabled, require the checkout, Python binary, and run root to be absolute, non-root paths. Do not include their values in production validation errors.

- [ ] **Step 5: Run Worker and settings tests**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_android_memory_worker.py services/api/tests/unit/test_security.py -q
```

Expected: all Worker and settings tests PASS.

- [ ] **Step 6: Commit the Worker boundary**

```bash
git add services/api/src/perfpilot_api/engines/android_memory_worker.py services/api/src/perfpilot_api/config.py services/api/tests/unit/test_android_memory_worker.py services/api/tests/unit/test_security.py infra/engines/android-memory/Dockerfile
git commit -m "feat: add isolated Android memory worker boundary"
git push -u origin HEAD
```

## Task 8: Implement the Android Memory Adapter

**Files:**

- Create: `services/api/src/perfpilot_api/engines/android_memory.py`
- Create: `services/api/tests/unit/test_android_memory_adapter.py`
- Modify: `services/api/src/perfpilot_api/engines/contracts.py:47-74`
- Modify: `services/api/src/perfpilot_api/engines/__init__.py`
- Modify: `services/api/tests/unit/test_engine_contracts.py`

- [ ] **Step 1: Write failing Adapter tests**

Cover descriptor values, input validation, deterministic run IDs, progress events, status, exit-code mapping, output contract, privacy scan, fetch, and cancel:

```python
@pytest.mark.asyncio
async def test_exit_two_preserves_context_as_insufficient_data() -> None:
    worker = FakeMemoryWorker(exit_code=2, payload=valid_context_bytes("insufficient"))
    adapter = AndroidMemoryAdapter(stager=FakeStager(), worker=worker, max_timeout_seconds=900)

    run_ref = await adapter.submit(
        engine_inputs(),
        SubmitConfig(
            execution_id=EXECUTION_ID,
            analysis_id=ANALYSIS_ID,
            profile="auto",
            question="为什么退出后没有释放？",
            external_workspace_id=None,
            timeout_seconds=900,
        ),
    )
    status = await adapter.status(run_ref)
    result = await adapter.fetch_result(run_ref)

    assert status.state == "insufficient_data"
    assert result.state == "insufficient_data"
    assert result.contract == "android-memory-ai-context-1.2"
    assert result.payload["context_type"] == "android-memory-ai-context"
```

Add negative payloads containing `/work/input`, `file://`, `X-Amz-Signature`, `object_key`, and database URLs; expect `privacy_violation` without the marker in repr or exception text.

- [ ] **Step 2: Run Adapter tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_android_memory_adapter.py services/api/tests/unit/test_engine_contracts.py -q
```

Expected: collection FAIL because `AndroidMemoryAdapter` and `SubmitConfig.execution_id` do not exist.

- [ ] **Step 3: Extend SubmitConfig with execution authority**

Add `execution_id` before `analysis_id`:

```python
@dataclass(frozen=True, slots=True)
class SubmitConfig:
    execution_id: UUID
    analysis_id: UUID
    profile: AnalysisProfile
    question: str | None
    external_workspace_id: str | None
    timeout_seconds: int
```

Update every existing SmartPerfetto fixture and orchestration call to pass the EngineExecution ID. SmartPerfetto must not send this value upstream.

- [ ] **Step 4: Implement the Adapter descriptor and lifecycle**

Use this descriptor:

```python
descriptor = AdapterDescriptor(
    engine_id="android_memory",
    adapter_version="1.0.0",
    profiles=frozenset({"auto"}),
    required_inputs=frozenset({"memory_capture_manifest"}),
    optional_inputs=frozenset({"memory_evidence", "capture_manifest", "log", "screenshot", "trace"}),
    accepted_contracts=frozenset({"android-memory-ai-context-1.2"}),
    default_timeout_seconds=900,
    resource_profile="isolated_worker",
    stable_error_codes=frozenset(
        {
            "missing_input",
            "manifest_invalid",
            "download_failed",
            "integrity_mismatch",
            "input_limit_exceeded",
            "worker_unavailable",
            "engine_timeout",
            "engine_failed",
            "invalid_output",
            "incompatible_contract",
            "privacy_violation",
        }
    ),
)
```

Derive the opaque run ID as `memory-<execution UUID hex>`. `submit` must stage inputs, call `worker.start`, and return a run ref with no workspace or session. `stream` returns bounded synthetic progress events from Worker status. `fetch_result` accepts exit `0` or `2`, parses `AndroidMemoryContext`, runs the privacy marker scan, and returns an `EngineResult`. Every other exit code maps to `engine_failed`; lost runs map to retryable `worker_unavailable`.

- [ ] **Step 5: Run Adapter and engine contract tests**

Run the command from Step 2.

Expected: all Adapter and shared contract tests PASS.

- [ ] **Step 6: Commit the Adapter**

```bash
git add services/api/src/perfpilot_api/engines/android_memory.py services/api/src/perfpilot_api/engines/contracts.py services/api/src/perfpilot_api/engines/__init__.py services/api/tests/unit/test_android_memory_adapter.py services/api/tests/unit/test_engine_contracts.py services/api/tests/unit/test_smartperfetto_adapter.py services/api/tests/unit/test_engine_execution_service.py
git commit -m "feat: add Android memory adapter"
git push -u origin HEAD
```

## Task 9: Integrate isolated execution, recovery, and the real upstream contract

**Files:**

- Modify: `services/api/src/perfpilot_api/services/engine_executions.py:673-1180`
- Create: `services/api/src/perfpilot_api/services/memory_executions.py`
- Modify: `services/api/src/perfpilot_api/main.py:145-297`
- Modify: `services/api/tests/unit/test_engine_execution_service.py`
- Create: `services/api/tests/unit/test_memory_execution_service.py`
- Modify: `services/api/tests/unit/test_app.py`
- Modify: `services/api/tests/integration/test_engine_execution_repository.py`
- Create: `services/api/tests/contract/test_android_memory_upstream.py`
- Create: `services/api/tests/fixtures/android_memory/minimal_meminfo.txt`

- [ ] **Step 1: Write failing orchestration tests**

Prove that isolated engines skip workspace provisioning, `insufficient_data` still passes through the result sink, and recoverable Worker loss creates a new attempt:

```python
@pytest.mark.asyncio
async def test_isolated_engine_skips_workspace_and_sinks_insufficient_result() -> None:
    adapter = FakeAndroidMemoryAdapter(result_state="insufficient_data")
    workspace = FakeWorkspaceService()
    service, repository, sink = execution_service(adapter=adapter, workspace=workspace)
    repository.record = _record(engine_id="android_memory")

    submitted = await service.submit_attempt(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
        inputs=memory_inputs(),
        profile="auto",
        question=None,
        timeout_seconds=900,
    )
    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=submitted.id,
    )

    assert workspace.calls == []
    assert sink.writes == 1
    assert outcome.state == "insufficient_data"
```

Add repository tests that permit only the valid mode/engine pairs (`trace_upload` with `smartperfetto`, `memory_upload` with `android_memory`), reject cross-pair allocation, accept a memory run reference without workspace or session IDs, and preserve deterministic retry attempt numbering.

- [ ] **Step 2: Run focused orchestration tests and confirm the red state**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_engine_execution_service.py services/api/tests/integration/test_engine_execution_repository.py -q
```

Expected: FAIL because submission always provisions a workspace and `insufficient_data` does not finalize through the sink.

- [ ] **Step 3: Branch orchestration by resource profile**

In `submit_attempt`, resolve a workspace only for `network_service`:

```python
external_workspace_id: str | None = None
if adapter.descriptor.resource_profile == "network_service":
    workspace = await self._workspace_service.ensure_workspace(team_id=team_id)
    if workspace.external_workspace_id is None:
        raise EngineExecutionOwnershipError("workspace is not active")
    external_workspace_id = workspace.external_workspace_id

run_ref = await adapter.submit(
    inputs,
    SubmitConfig(
        execution_id=record.id,
        analysis_id=analysis_id,
        profile=profile,
        question=question,
        external_workspace_id=external_workspace_id,
        timeout_seconds=timeout_seconds,
    ),
)
```

Treat `completed` and `insufficient_data` status identically until `_finalize` fetches and sinks the result. Permit new attempts for `capacity_exceeded`, `worker_unavailable`, and `engine_timeout`; keep integrity, schema, and privacy failures terminal.

Update `SQLAlchemyEngineExecutionRepository.allocate_attempt` so the Analysis mode and engine ID must match the two explicit pairs above. Replace the SmartPerfetto-only submitted-reference validator with a resource-profile-aware validator: `network_service` still requires validated workspace, session, and run IDs; `isolated_worker` requires only the deterministic opaque run ID and stores workspace/session as `NULL`. Apply the same ownership rule in observation, finalization, cancellation, and recovery paths.

- [ ] **Step 4: Add the composition boundary**

Replace the SmartPerfetto-only builder with a general builder that accepts optional adapters and always uses an explicit registry. In production, if Android Memory is enabled and the injected Worker is absent or `isolation != "oci"`, application startup must fail with `An externally isolated Android Memory worker is unavailable`. Development may construct `LocalAndroidMemoryWorker` from absolute settings.

Do not set a fake `engine-lock.yaml` image digest. Unit tests may use an in-memory `EngineLock` with a test digest; production remains closed until release automation supplies the real digest.

- [ ] **Step 5: Resolve tenant inputs and prepare a real execution**

Add a coordinator that loads the server Manifest and every referenced Artifact through the routed tenant repository, requests short-lived download claims, and passes only public metadata plus `SecretStr` URLs to `EngineExecutionService`:

```python
@dataclass(frozen=True, slots=True)
class PreparedMemoryExecution:
    execution: EngineExecutionRecord
    inputs: tuple[EngineInput, ...]
    question: str | None


class MemoryExecutionService:
    async def prepare(
        self,
        *,
        team_id: UUID,
        analysis_id: UUID,
        capture_id: UUID,
    ) -> PreparedMemoryExecution:
        capture = await self._repository.load_capture(
            team_id=team_id,
            analysis_id=analysis_id,
            capture_id=capture_id,
        )
        if capture.analysis_mode != "memory_upload":
            raise MemoryExecutionNotFoundError("memory capture was not found")
        inputs = tuple(
            await self._claim_input(team_id, analysis_id, artifact)
            for artifact in (capture.manifest_artifact, *capture.evidence_artifacts)
        )
        execution = await self._engine_service.create_attempt(
            team_id=team_id,
            analysis_id=analysis_id,
            engine_id="android_memory",
            input_manifest_hash=capture.manifest.sha256_hex(),
            config_hash=canonical_memory_config_hash(
                capture_id=capture_id,
                question=capture.question,
                timeout_seconds=self._timeout_seconds,
            ),
        )
        return PreparedMemoryExecution(execution, inputs, capture.question)
```

`load_capture` must derive the Manifest Artifact ID from `capture_id`, load its immutable bytes from the tenant-routed Artifact store, parse them, verify the embedded Analysis/Capture IDs and canonical hash, and then resolve exactly the referenced Artifact rows. It must accept only a `memory_upload` Analysis in this package; the future `memory_cycle`/SampleAttempt coordinator will reuse the Manifest model through a separate route. `_claim_input` must build `EngineInput` from tenant-authoritative `artifact_kind`, MIME, size, checksum, and `UploadService.download`. It must never accept any client-supplied URL or object location. Tests must prove another tenant, another Analysis, a non-memory Analysis, deleted Artifact, manifest hash mismatch, and claim expiry all fail with stable redacted errors.

The execution worker calls `prepare`, then `submit_attempt`, and follows returned `EngineRetryDirective` values while calling `step`. Do not run the 15-minute analysis inside a FastAPI request or `BackgroundTasks`; durable worker scheduling remains the owner of this internal service.

- [ ] **Step 6: Add a real pinned-upstream contract test**

Write the fixture exactly as:

```text
** MEMINFO in pid 1234 [com.example.app] **
 App Summary
                       Pss(KB)
                        ------
           Java Heap:     1024
         Native Heap:     2048
               TOTAL:     4096       TOTAL SWAP PSS: 0
```

The contract test reads `PERFPILOT_ANDROID_MEMORY_ROOT`, verifies `git rev-parse HEAD` equals the lock commit, copies the fixture into a temporary input directory, invokes the real Worker with `--strict`, and validates the resulting context. Skip only when the environment variable is absent; when it is present, a commit mismatch is a failure.

- [ ] **Step 7: Run focused orchestration and real contract tests**

Run:

```bash
env PERFPILOT_ANDROID_MEMORY_ROOT=/Users/ray/Android-App-Memory-Analysis PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/unit/test_engine_execution_service.py services/api/tests/unit/test_memory_execution_service.py services/api/tests/integration/test_engine_execution_repository.py services/api/tests/contract/test_android_memory_upstream.py -q
```

Expected: all focused tests PASS and the real result reports schema `1.2`, generator `android-memory-ai` version `1.2.0`, and no local paths.

- [ ] **Step 8: Run the complete verification suite**

Run API tests:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests -q
```

Expected: all non-environment tests PASS; only documented external-service tests skip.

Run PostgreSQL tests:

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://ray@127.0.0.1:55439/postgres PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api pytest -p no:cacheprovider services/api/tests/integration -q
```

Expected: all PostgreSQL integration tests PASS.

Run Ruff:

```bash
env PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/perfpilot-memory-plan /Users/ray/Library/Python/3.12/bin/uv run --offline --package perfpilot-api ruff check services/api/src services/api/tests
```

Expected: `All checks passed!`

- [ ] **Step 9: Commit the orchestration packet**

```bash
git add services/api/src/perfpilot_api/services/engine_executions.py services/api/src/perfpilot_api/services/memory_executions.py services/api/src/perfpilot_api/main.py services/api/tests/unit/test_engine_execution_service.py services/api/tests/unit/test_memory_execution_service.py services/api/tests/unit/test_app.py services/api/tests/integration/test_engine_execution_repository.py services/api/tests/contract/test_android_memory_upstream.py services/api/tests/fixtures/android_memory/minimal_meminfo.txt
git commit -m "feat: orchestrate Android memory executions"
git push -u origin HEAD
```

- [ ] **Step 10: Verify the remote branch matches the verified local HEAD**

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/feature/perfpilot-android-memory-adapter
```

Expected: both commands print the same commit SHA.

## Final acceptance checklist

- [ ] `memory_upload` is valid in both databases and remains bound to an existing ApplicationVersion.
- [ ] Public uploads accept `memory_evidence` and `screenshot` but reject `memory_capture_manifest`.
- [ ] The server rebuilds every Manifest from tenant-authoritative Artifact rows.
- [ ] A Manifest represents exactly one stage and never contains storage locations or server paths.
- [ ] Host staging verifies byte count and SHA-256 and never extracts archives.
- [ ] Production wiring requires an externally isolated Worker.
- [ ] Exit `2` persists a result and ends in `insufficient_data`.
- [ ] Worker loss and timeout use bounded new attempts; schema, integrity, and privacy failures do not retry.
- [ ] The real pinned checkout produces the accepted schema with local paths disabled.
- [ ] Full API, PostgreSQL, Ruff, privacy, and tenant-isolation tests pass.
- [ ] Each task has one focused commit and the remote feature branch contains all commits.
