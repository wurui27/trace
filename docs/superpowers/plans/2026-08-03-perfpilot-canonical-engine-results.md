# PerfPilot Canonical Engine Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist SmartPerfetto and Android Memory terminal results as deterministic, immutable, tenant-fenced JSON Artifacts without creating public reports or invoking AI.

**Architecture:** A pure canonicalization boundary validates and copies adapter output before any asynchronous dependency call. A dedicated tenant Artifact repository and S3 sink reserve deterministic rows, pin one exact object VersionId, and converge concurrent identical writers. `EngineExecutionService` supplies all provenance from its claimed record and maps typed persistence errors into stable execution outcomes.

**Tech Stack:** Python 3.12, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 17, boto3/botocore, JSON Schema 2020-12, pytest

---

## File map

Create:

- `contracts/v1/engines/canonical-engine-result.schema.json`: closed public artifact schema.
- `contracts/v1/examples/canonical-engine-result.smartperfetto.valid.json`: valid SmartPerfetto envelope.
- `contracts/v1/examples/canonical-engine-result.android-memory.valid.json`: valid Android Memory envelope.
- `services/api/src/perfpilot_api/engines/canonical_results.py`: immutable write request, canonical validation error, deterministic bytes and hashes.
- `services/api/src/perfpilot_api/services/engine_result_artifacts.py`: re-exported validation error, storage errors, tenant repository and immutable S3 sink.
- `services/api/migrations/control/versions/0006_engine_execution_tenant_resource_version.py`: authoritative execution route-version column.
- `services/api/tests/contract/test_canonical_engine_result_contract.py`: schema and pairing tests.
- `services/api/tests/unit/test_canonical_engine_results.py`: pure canonicalization tests.
- `services/api/tests/unit/test_engine_result_artifacts.py`: repository protocol and version-aware S3 sink tests.
- `services/api/tests/integration/test_engine_result_artifact_repository.py`: routed PostgreSQL ownership, concurrency and rollover tests.

Modify:

- `services/api/src/perfpilot_api/engines/smartperfetto_contracts.py`: pure validation of an already-sanitized stable report payload.
- `services/api/src/perfpilot_api/db/control/models/engines.py`: required positive `tenant_resource_version`.
- `services/api/src/perfpilot_api/services/engine_executions.py`: provenance propagation, write request and typed sink mapping.
- `services/api/src/perfpilot_api/services/memory_executions.py`: persist the input authorization's tenant resource version.
- `services/api/src/perfpilot_api/runtime/artifacts.py`: compose the production result sink from the existing router, resolver and S3 client.
- `services/api/tests/integration/test_migrations.py`
- `services/api/tests/integration/test_engine_execution_repository.py`
- `services/api/tests/unit/test_engine_execution_service.py`
- `services/api/tests/unit/test_memory_execution_service.py`
- `services/api/tests/unit/test_artifact_runtime.py`

Do not modify tenant migrations, `Artifact`, `ReportVersion`, `main.py`, public routes, workers, Normalizer or AI code.

### Task 1: Define the closed canonical result contract

**Files:**

- Create: `contracts/v1/engines/canonical-engine-result.schema.json`
- Create: `contracts/v1/examples/canonical-engine-result.smartperfetto.valid.json`
- Create: `contracts/v1/examples/canonical-engine-result.android-memory.valid.json`
- Create: `services/api/tests/contract/test_canonical_engine_result_contract.py`

- [ ] **Step 1: Write failing contract tests**

Load the schema with `jsonschema.Draft202012Validator` and its format checker.
Require both examples to validate. Mutate each example to prove rejection of:

```python
@pytest.mark.parametrize(
    ("engine_id", "source_contract"),
    [
        ("smartperfetto", "android-memory-ai-context-1.2"),
        ("android_memory", "workspace-agent-v1"),
    ],
)
def test_engine_contract_cross_pairing_is_rejected(
    engine_id: str,
    source_contract: str,
) -> None:
    payload = deepcopy(_smart_example())
    payload["engine"]["engine_id"] = engine_id
    payload["engine"]["source_contract"] = source_contract
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)
```

Also test an unknown root, engine, attempt and result key; `failed` state; zero
versions/attempts; malformed UUID, commit, image digest and hashes; and a
non-object payload.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/contract/test_canonical_engine_result_contract.py -q
```

Expected: fail because the schema and examples do not exist.

- [ ] **Step 3: Add the Draft 2020-12 schema**

The root, `engine`, `attempt` and `result` objects use
`additionalProperties: false`. Require every field shown in the approved
design. Apply these exact constraints:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://perfpilot.dev/contracts/v1/engines/canonical-engine-result.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "result_type", "artifact_id", "analysis_id",
    "execution_id", "tenant_resource_version", "engine", "attempt", "result"
  ],
  "properties": {
    "schema_version": {"const": "1.0"},
    "result_type": {"const": "canonical-engine-result"},
    "artifact_id": {"type": "string", "format": "uuid"},
    "analysis_id": {"type": "string", "format": "uuid"},
    "execution_id": {"type": "string", "format": "uuid"},
    "tenant_resource_version": {"type": "integer", "minimum": 1},
    "engine": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "engine_id", "adapter_version", "source_contract",
        "source_commit_sha", "image_digest"
      ],
      "properties": {
        "engine_id": {"enum": ["smartperfetto", "android_memory"]},
        "adapter_version": {
          "type": "string", "minLength": 1, "maxLength": 32,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._+-]*$"
        },
        "source_contract": {
          "enum": ["workspace-agent-v1", "android-memory-ai-context-1.2"]
        },
        "source_commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "image_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
      }
    },
    "attempt": {
      "type": "object",
      "additionalProperties": false,
      "required": ["number", "input_manifest_hash", "config_hash"],
      "properties": {
        "number": {"type": "integer", "minimum": 1},
        "input_manifest_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "config_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
      }
    },
    "result": {
      "type": "object",
      "additionalProperties": false,
      "required": ["state", "payload_sha256", "payload"],
      "properties": {
        "state": {"enum": ["completed", "insufficient_data"]},
        "payload_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "payload": {"type": "object"}
      }
    }
  },
  "allOf": [
    {
      "if": {"properties": {"engine": {"properties": {"engine_id": {"const": "smartperfetto"}}}}},
      "then": {"properties": {"engine": {"properties": {"source_contract": {"const": "workspace-agent-v1"}}}}}
    },
    {
      "if": {"properties": {"engine": {"properties": {"engine_id": {"const": "android_memory"}}}}},
      "then": {"properties": {"engine": {"properties": {"source_contract": {"const": "android-memory-ai-context-1.2"}}}}}
    }
  ]
}
```

- [ ] **Step 4: Add byte-accurate examples**

Use deterministic artifact IDs for execution IDs ending in `0101` and `0102`:

```text
c79e45ad-fcb4-5b16-a327-f3aae70eebbc
5f9cc3e2-5d41-5db9-8964-38dc5cd819b4
```

The SmartPerfetto payload is exactly:

```json
{"reportId":"report-1","report":{"reportId":"report-1","summary":{"conclusion":"Main thread blocked"}}}
```

Its canonical payload hash is
`07b9aa68bf4d16936ee0d6f7b0234db1cc1d2e1e6b80e7c0a6fa8963289723b3`.

The Android example contains the required 1.2 generator, support fields and
both actual-false privacy flags. Its canonical payload hash is
`3960e8b9c6487d4c1a61d45fcbad511c048d721ff76047dac45ba89802eb7907`.

- [ ] **Step 5: Run contract tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/contract/test_canonical_engine_result_contract.py -q
.venv/bin/ruff check services/api/tests/contract/test_canonical_engine_result_contract.py
git add contracts/v1/engines contracts/v1/examples/canonical-engine-result.* \
  services/api/tests/contract/test_canonical_engine_result_contract.py
git commit -m "feat: define canonical engine result contract"
```

Expected: all contract tests pass and Ruff reports no errors.

### Task 2: Build deterministic canonical bytes

**Files:**

- Create: `services/api/src/perfpilot_api/engines/canonical_results.py`
- Create: `services/api/tests/unit/test_canonical_engine_results.py`
- Modify: `services/api/src/perfpilot_api/engines/smartperfetto_contracts.py`

- [ ] **Step 1: Write failing value-object and canonicalization tests**

Define helpers that produce one valid write per engine. Tests must assert:

- dict insertion order produces identical bytes and hashes;
- non-ASCII text remains UTF-8;
- `payload_sha256_hex` hashes only canonical payload bytes;
- `request_hash_hex` hashes the complete envelope bytes;
- `checksum_sha256_b64` is the complete-envelope checksum;
- mutating the original payload after canonicalization cannot change returned bytes;
- the document contains no team ID, execution version, timestamps or storage data;
- deterministic artifact identity is enforced.

Reject wrong engine/contract pairs, invalid terminal states, provenance formats,
zero versions, wrong artifact ID, bytes, non-string keys, NaN/Infinity, cyclic
structures, depth over 64, more than 200,000 nodes, collections over 50,000
items, keys over 1,024 characters, strings over 1 MiB, a canonical envelope over
2 MiB, sensitive keys, credentials, signed URLs, object-store URIs, database
URLs, `file://`, absolute POSIX/Windows paths and `../` traversal.

- [ ] **Step 2: Verify canonicalization tests are RED**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_canonical_engine_results.py -q
```

Expected: collection fails because `canonical_results.py` does not exist.

- [ ] **Step 3: Add the stable SmartPerfetto payload validator**

Export this pure boundary from `smartperfetto_contracts.py`:

```python
def validate_sanitized_report_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != {"reportId", "report"}:
        raise ValueError("report contract invalid")
    report_id = payload.get("reportId")
    report = payload.get("report")
    if not isinstance(report_id, str) or not isinstance(report, Mapping):
        raise ValueError("report contract invalid")
    if report.get("reportId") != report_id:
        raise ValueError("report contract invalid")
    sanitized = _sanitize_nested(report, depth=0)
    if not isinstance(sanitized, dict) or sanitized != report:
        raise ValueError("report contract invalid")
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_SANITIZED_REPORT_BYTES:
        raise ValueError("report contract invalid")
    return {"reportId": report_id, "report": sanitized}
```

Validate `report_id` with the same opaque-ID pattern and reject `reportError`
from a stable payload so external error text cannot enter the artifact.

- [ ] **Step 4: Implement immutable request and canonical result types**

Use these public shapes:

```python
class EngineResultValidationError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("engine result is invalid")


@dataclass(frozen=True, slots=True)
class EngineResultWrite:
    team_id: UUID
    analysis_id: UUID
    execution_id: UUID
    expected_execution_version: int
    tenant_resource_version: int
    artifact_id: UUID
    engine_id: Literal["smartperfetto", "android_memory"]
    adapter_version: str
    engine_commit_sha: str
    engine_image_digest: str
    attempt_number: int
    input_manifest_hash: str
    config_hash: str
    result: EngineResult = field(repr=False)


@dataclass(frozen=True, slots=True)
class CanonicalEngineResult:
    document: dict[str, object] = field(repr=False)
    canonical_bytes: bytes = field(repr=False)
    payload_sha256_hex: str
    request_hash_hex: str
    checksum_sha256_b64: str = field(repr=False)
```

Move the result UUID namespace and `result_artifact_id(execution_id)` into this
module. `engine_executions.py` will later import and re-export it.

- [ ] **Step 5: Implement fail-closed traversal and serialization**

`canonicalize_engine_result(request)` must finish traversal, contract
revalidation, defensive copying and both
`json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)`
calls before returning. It performs no I/O and has no `await`.

For Android Memory, call
`AndroidMemoryContext.model_validate(payload, strict=True).model_dump(mode="json")`
and require the copied value to preserve both privacy flags as `False`. For
SmartPerfetto, call `validate_sanitized_report_payload()`.

Serialize payload and envelope with UTF-8, sorted keys and compact separators.
Reject any envelope larger than `2 * 1024 * 1024` bytes. Raise only
`EngineResultValidationError("engine result is invalid")`, with no original
exception text or payload in `repr`.

- [ ] **Step 6: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_canonical_engine_results.py \
  services/api/tests/unit/test_smartperfetto_contracts.py -q
.venv/bin/ruff check \
  services/api/src/perfpilot_api/engines/canonical_results.py \
  services/api/src/perfpilot_api/engines/smartperfetto_contracts.py \
  services/api/tests/unit/test_canonical_engine_results.py
git add services/api/src/perfpilot_api/engines/canonical_results.py \
  services/api/src/perfpilot_api/engines/smartperfetto_contracts.py \
  services/api/tests/unit/test_canonical_engine_results.py
git commit -m "feat: canonicalize engine result bytes"
```

### Task 3: Persist tenant resource provenance on executions

**Files:**

- Create: `services/api/migrations/control/versions/0006_engine_execution_tenant_resource_version.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/engines.py`
- Modify: `services/api/src/perfpilot_api/services/engine_executions.py`
- Modify: `services/api/src/perfpilot_api/services/memory_executions.py`
- Modify: `services/api/tests/integration/test_migrations.py`
- Modify: `services/api/tests/integration/test_engine_execution_repository.py`
- Modify: `services/api/tests/unit/test_engine_execution_service.py`
- Modify: `services/api/tests/unit/test_memory_execution_service.py`

- [ ] **Step 1: Write failing migration and provenance tests**

Require `engine_executions.tenant_resource_version` to be non-null, positive and
present in the ORM record. Add migration tests proving:

- upgrade from 0005 succeeds when the execution table is empty;
- upgrade takes the new exact schema to one Alembic head;
- upgrade from 0005 refuses when any legacy execution exists;
- downgrade refuses when any execution exists and otherwise drops the column;
- no server default fabricates a route version.

Change all execution fixtures to include `tenant_resource_version=1` (or `7`
for memory preparation). Add a repository test proving attempt 2 retains the
same version as attempt 1.

- [ ] **Step 2: Verify migration/provenance tests are RED**

Run with a temporary PostgreSQL admin URL:

```bash
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/pytest -p no:cacheprovider \
  services/api/tests/integration/test_migrations.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/unit/test_memory_execution_service.py -q
```

Expected: failures for the missing column, record field and create-attempt
argument.

- [ ] **Step 3: Add the safe control migration**

Use revision `0006_engine_tenant_version`, down revision
`0005_memory_upload_mode`. The shortened revision stays within Alembic's default
32-character `version_num` column without mutating Alembic's internal schema.
Upgrade and downgrade both execute:

```sql
LOCK TABLE engine_executions IN ACCESS EXCLUSIVE MODE
```

If `SELECT 1 FROM engine_executions LIMIT 1` returns a row, raise
`RuntimeError("engine execution tenant version migration preflight failed")`.
For an empty table, add a non-null integer column with no server default and
the check `tenant_resource_version > 0`. Downgrade drops the check and column
only after the same empty-table preflight.

- [ ] **Step 4: Propagate the field through records and retries**

Add `tenant_resource_version: int` to `EngineExecutionSeed` and
`EngineExecutionRecord`, persist it in `allocate_attempt()`, return it from
`_record()`, and copy it unchanged in `reserve_retry()`.

Extend `EngineExecutionService.create_attempt()` and
`MemoryAttemptService.create_attempt()` with a required positive
`tenant_resource_version`. In `MemoryExecutionService.prepare()`, pass
`capture.tenant_resource_version`, the same version used for every download
authorization.

- [ ] **Step 5: Run tests and commit**

```bash
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/pytest -p no:cacheprovider \
  services/api/tests/integration/test_migrations.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/unit/test_memory_execution_service.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add services/api/migrations/control/versions/0006_engine_execution_tenant_resource_version.py \
  services/api/src/perfpilot_api/db/control/models/engines.py \
  services/api/src/perfpilot_api/services/engine_executions.py \
  services/api/src/perfpilot_api/services/memory_executions.py \
  services/api/tests/integration/test_migrations.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/unit/test_memory_execution_service.py
git commit -m "feat: fence engine executions by tenant version"
```

### Task 4: Reserve and finalize tenant result Artifacts

**Files:**

- Create: `services/api/src/perfpilot_api/services/engine_result_artifacts.py`
- Create: `services/api/tests/integration/test_engine_result_artifact_repository.py`

- [ ] **Step 1: Write failing routed PostgreSQL tests**

Use two independent tenant databases and a mutable router. Prove:

- Android Memory can reserve only a `memory_upload` Analysis;
- SmartPerfetto can reserve only a `trace_upload` Analysis;
- the same team/analysis/artifact/request converges on one pending row;
- another team route, missing Analysis or cross-mode pairing is a conflict;
- an existing row with another owner, upload ID, idempotency key, request hash,
  MIME, length, checksum or object key is a conflict;
- finalization is CAS-protected and pins one exact VersionId;
- routed resource-version mismatch fails before insert/update;
- concurrent identical reserve/finalize calls converge.

- [ ] **Step 2: Verify repository tests are RED**

```bash
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/pytest -p no:cacheprovider \
  services/api/tests/integration/test_engine_result_artifact_repository.py -q
```

Expected: import failure for the missing repository.

- [ ] **Step 3: Define stable errors and repository types**

Import and re-export `EngineResultValidationError` from
`canonical_results.py`. Define the storage errors locally, and expose only
these redacted public errors:

```python
class EngineResultArtifactError(RuntimeError):
    pass


class EngineResultConflictError(EngineResultArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("engine result integrity conflict")


class EngineResultUnavailableError(EngineResultArtifactError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("engine result service is unavailable")
```

`EngineResultArtifactRecord` includes artifact/analysis/upload identity,
idempotency and request hashes, artifact kind, MIME, size, checksum, object key,
state, expiry, row version and immutable storage VersionId. Mark object key,
checksum and VersionId `repr=False`.

- [ ] **Step 4: Implement the repository**

`SQLAlchemyEngineResultArtifactRepository` opens only
`TenantRouter.session(tenant.team_id)` and requires
`session.info["tenant_resource_version"] == tenant.resource_version` on every
operation. `reserve()` first proves Analysis mode matches engine ID, then uses
PostgreSQL `insert(Artifact).on_conflict_do_nothing(index_elements=(Artifact.id,))`,
reloads the deterministic row,
and returns it for full comparison by the sink.

Insert exact metadata:

```text
artifact_kind = engine_result
upload_id = artifact_id
idempotency_key = internal:engine_result:<execution_id>
object_key = raw/analyses/<analysis_id>/internal/engine-results/<artifact_id>.json
state = pending
version = 1
expires_at = now + 30 days
```

Add `require_resource_version(tenant)` for explicit object-boundary fences,
`finalize(tenant, analysis_id, artifact_id, expected_version, storage_version_id, now,
expires_at)` for CAS, and `reload(tenant, analysis_id, artifact_id)` for the
concurrent-winner path.

- [ ] **Step 5: Run tests and commit**

```bash
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/pytest -p no:cacheprovider \
  services/api/tests/integration/test_engine_result_artifact_repository.py -q
.venv/bin/ruff check \
  services/api/src/perfpilot_api/services/engine_result_artifacts.py \
  services/api/tests/integration/test_engine_result_artifact_repository.py
git add services/api/src/perfpilot_api/services/engine_result_artifacts.py \
  services/api/tests/integration/test_engine_result_artifact_repository.py
git commit -m "feat: reserve canonical engine result artifacts"
```

### Task 5: Pin one immutable S3 object version

**Files:**

- Modify: `services/api/src/perfpilot_api/services/engine_result_artifacts.py`
- Create: `services/api/tests/unit/test_engine_result_artifacts.py`

- [ ] **Step 1: Write failing sink tests**

Use a version-aware fake repository/client and botocore `Stubber`. Cover:

- validation completes before resolver, DB or S3 calls;
- resolver team/version/bucket mismatch;
- every explicit resource-version fence (after reserve, before PUT, after HEAD,
  after concurrent reload and before return);
- exact PUT body/MIME/base64 checksum;
- empty, `null`, control-character or missing PUT VersionId;
- PUT checksum mismatch;
- HEAD always includes the PUT VersionId and rejects another VersionId, checksum,
  MIME, length, or a delete marker;
- a finalized identical row reads only its exact VersionId and compares bytes;
- pending crash repair finalizes a new verified version;
- concurrent identical writers return the winning artifact/version after reading
  the winner's exact bytes;
- conflicting bytes or metadata never succeed;
- GET body closes on success and failure;
- cancellation, `KeyboardInterrupt` and `SystemExit` propagate;
- dependency exception text, bucket, key, VersionId and payload never appear in
  raised error text or repr.

- [ ] **Step 2: Run sink tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_result_artifacts.py -q
```

Expected: fail because `S3EngineResultSink` does not exist.

- [ ] **Step 3: Implement the sink flow**

`S3EngineResultSink.write(request)` synchronously calls
`canonicalize_engine_result(request)` before its first `await`. It then:

1. resolves the exact `TenantBucket`;
2. reserves and fully compares the Artifact row;
3. fences the route before object I/O;
4. returns a verified finalized identical object when present;
5. PUTs canonical bytes with `application/json` and `ChecksumSHA256`;
6. requires the receipt checksum and a safe non-empty VersionId;
7. HEADs that exact VersionId with `ChecksumMode="ENABLED"`;
8. fences again and CAS-finalizes the row;
9. when another writer won, reloads and GETs the winner's exact VersionId,
   verifies metadata, closes the body and compares bytes with
   `hmac.compare_digest`;
10. performs the final route fence and returns only `request.artifact_id`.

No log or error contains object coordinates, VersionId or payload. Unknown
dependency exceptions become `EngineResultUnavailableError` without chaining.
Identity/metadata divergence becomes `EngineResultConflictError`.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_result_artifacts.py \
  services/api/tests/integration/test_engine_result_artifact_repository.py -q
.venv/bin/ruff check \
  services/api/src/perfpilot_api/services/engine_result_artifacts.py \
  services/api/tests/unit/test_engine_result_artifacts.py
git add services/api/src/perfpilot_api/services/engine_result_artifacts.py \
  services/api/tests/unit/test_engine_result_artifacts.py
git commit -m "feat: persist immutable engine result versions"
```

### Task 6: Route finalization through the authoritative write request

**Files:**

- Modify: `services/api/src/perfpilot_api/services/engine_executions.py`
- Modify: `services/api/tests/unit/test_engine_execution_service.py`

- [ ] **Step 1: Write failing orchestration tests**

Change `FakeSink.write()` to accept one `EngineResultWrite`. Assert every field
comes from the claimed `EngineExecutionRecord`; the adapter may supply only
`EngineResult.contract`, terminal state and payload.

Add exact error mapping tests:

```python
@pytest.mark.parametrize(
    ("error", "stable_code", "expected_state", "retry_mode"),
    [
        (EngineResultValidationError(), "invalid_output", "failed", None),
        (
            EngineResultConflictError(),
            "result_integrity_mismatch",
            "failed",
            None,
        ),
        (
            EngineResultUnavailableError(),
            "result_persistence_failed",
            "running",
            "reconnect",
        ),
    ],
)
async def test_result_sink_errors_map_to_stable_outcomes(
    error: Exception,
    stable_code: str,
    expected_state: str,
    retry_mode: str | None,
) -> None:
    service, repository, _workspaces, _adapter, sink = _isolated_service()
    repository.record = replace(
        repository.record,
        state="running",
        external_run_id=f"memory-{EXECUTION_ID.hex}",
        started_at=NOW,
        raw_result_artifact_id=result_artifact_id(EXECUTION_ID),
        version=3,
    )
    sink.failure = error

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

    assert outcome.state == expected_state
    assert repository.record.stable_error_code == stable_code
    assert (None if outcome.retry is None else outcome.retry.mode) == retry_mode
```

Also prove an unavailable error becomes terminal only after the existing global
deadline, a wrong returned artifact UUID maps to
`result_integrity_mismatch`, control-flow exceptions propagate, and a crash
after sink success but before terminal CAS retries the same artifact ID and
byte-identical request.

- [ ] **Step 2: Verify orchestration tests are RED**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_execution_service.py -q
```

Expected: failures because the sink still receives keyword fragments and all
sink exceptions use the generic persistence retry path.

- [ ] **Step 3: Construct the request and map typed errors**

In `_finalize()`, construct:

```python
write = EngineResultWrite(
    team_id=claimed.team_id,
    analysis_id=claimed.analysis_id,
    execution_id=claimed.id,
    expected_execution_version=claimed.version,
    tenant_resource_version=claimed.tenant_resource_version,
    artifact_id=claimed.raw_result_artifact_id,
    engine_id=claimed.engine_id,
    adapter_version=claimed.adapter_version,
    engine_commit_sha=claimed.engine_commit_sha,
    engine_image_digest=claimed.engine_image_digest,
    attempt_number=claimed.attempt_number,
    input_manifest_hash=claimed.input_manifest_hash,
    config_hash=claimed.config_hash,
    result=result,
)
```

Change `EngineResultSink.write` to `write(request: EngineResultWrite) -> UUID`.
Map validation and conflict errors directly to terminal repository failures.
Keep unavailable/unknown dependency failures on the existing bounded
`result_persistence_failed` reconnect/deadline path. Treat a different returned
UUID as `EngineResultConflictError`, not a generic `ValueError`. Re-export
`result_artifact_id` from this module for existing callers.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/unit/test_memory_execution_service.py -q
.venv/bin/ruff check \
  services/api/src/perfpilot_api/services/engine_executions.py \
  services/api/tests/unit/test_engine_execution_service.py
git add services/api/src/perfpilot_api/services/engine_executions.py \
  services/api/tests/unit/test_engine_execution_service.py
git commit -m "feat: sink authoritative engine result writes"
```

### Task 7: Compose the production ResultSink boundary

**Files:**

- Modify: `services/api/src/perfpilot_api/runtime/artifacts.py`
- Modify: `services/api/tests/unit/test_artifact_runtime.py`

- [ ] **Step 1: Write failing runtime composition tests**

Monkeypatch `SQLAlchemyEngineResultArtifactRepository` and
`S3EngineResultSink`. Assert the runtime constructs them from the exact same
`TenantRouter`, `SQLAlchemyTenantBucketResolver` and S3 client already used by
uploads and APK inspection. Assert `runtime.engine_result_sink` exposes that
real sink when local APK inspection is enabled or omitted.

Keep cleanup-order, cancellation and redaction tests unchanged except for the
new required dataclass field.

- [ ] **Step 2: Verify runtime tests are RED**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_artifact_runtime.py -q
```

Expected: failures because `ArtifactRuntime` has no result sink.

- [ ] **Step 3: Compose the sink**

Add `engine_result_sink: S3EngineResultSink` to `ArtifactRuntime`. In
`build_artifact_runtime()`, create one bucket resolver, then construct:

```python
engine_result_sink = S3EngineResultSink(
    repository=SQLAlchemyEngineResultArtifactRepository(tenant_router=tenant_router),
    bucket_resolver=bucket_resolver,
    client=s3_client,
)
```

Do not modify `main.py`: `infra/engines/engine-lock.yaml` still has null image
digests, so the approved production condition “real sink plus production engine
lock” is not satisfied.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  services/api/tests/unit/test_artifact_runtime.py -q
.venv/bin/ruff check \
  services/api/src/perfpilot_api/runtime/artifacts.py \
  services/api/tests/unit/test_artifact_runtime.py
git add services/api/src/perfpilot_api/runtime/artifacts.py \
  services/api/tests/unit/test_artifact_runtime.py
git commit -m "feat: compose canonical engine result sink"
```

### Task 8: Verify the complete delivery and open the PR

**Files:**

- Modify only files required by failures attributable to Tasks 1-7.

- [ ] **Step 1: Run focused contract, unit and PostgreSQL suites**

```bash
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/pytest -p no:cacheprovider \
  services/api/tests/contract/test_canonical_engine_result_contract.py \
  services/api/tests/unit/test_canonical_engine_results.py \
  services/api/tests/unit/test_engine_result_artifacts.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/unit/test_memory_execution_service.py \
  services/api/tests/unit/test_artifact_runtime.py \
  services/api/tests/integration/test_engine_result_artifact_repository.py \
  services/api/tests/integration/test_engine_execution_repository.py \
  services/api/tests/integration/test_migrations.py -q
```

Expected: all selected tests pass with no PostgreSQL skip.

- [ ] **Step 2: Run the complete API and Web gates**

Start isolated PostgreSQL and Redis instances, pin
`PERFPILOT_ANDROID_MEMORY_ROOT` to commit
`d5514972ced78c3faa7fc17589c1ea9231645056`, and run:

```bash
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
PERFPILOT_REQUIRE_REDIS_TESTS=1 \
PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/pytest -p no:cacheprovider services/api/tests -q
.venv/bin/ruff check services/api/src services/api/tests
npm ci
npm run lint
npm test
```

Expected: no service/upstream skips; API, Ruff, Web unit/build/SSR checks all
pass. Record any third-party deprecation or vulnerability warning separately.

- [ ] **Step 3: Perform two-stage review**

Dispatch an independent specification reviewer against the approved design and
this plan. After approval, dispatch an independent code-quality/security
reviewer focused on tenant fencing, mutable payload races, S3 exact-version
semantics, typed errors and migration safety. Resolve every Critical or
Important finding with a new failing test before changing production code.

- [ ] **Step 4: Push and create a Draft Pull Request**

```bash
git status --short
git diff --check origin/main...HEAD
git push -u origin feature/perfpilot-canonical-engine-results
gh pr create --draft \
  --base main \
  --head feature/perfpilot-canonical-engine-results \
  --title "feat: persist canonical engine results" \
  --body-file /private/tmp/perfpilot-canonical-results-pr-body.md
```

Expected: remote head equals local HEAD, the PR is mergeable, and required
`python-quality`, `python-tests`, `web` and `ci-gate` checks start for that exact
commit. Do not merge until all checks and final branch review pass.
