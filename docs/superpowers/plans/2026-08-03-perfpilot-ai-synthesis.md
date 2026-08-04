# PerfPilot AI Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn each authoritative SmartPerfetto canonical result into an immutable, tenant-isolated `AnalysisReport 1.1` whose measurements come only from SmartPerfetto and whose explanation, prioritization, optimization advice, and retest plan come from a strictly bounded OpenAI-compatible synthesis step.

**Architecture:** Keep SmartPerfetto, deterministic normalization, AI synthesis, and report publication as separate recoverable stages. The control database stores only execution metadata and non-sensitive audit fields; version-bound canonical input, AI projection, validated synthesis, and public reports stay in the selected tenant's storage. A durable coordinator and leased report worker use deterministic IDs, checksums, and compare-and-swap writes so retries cannot overwrite or duplicate a report.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, Alembic, PostgreSQL 17, S3-compatible versioned storage, httpx, JSON Schema 2020-12, OpenAI-compatible Chat Completions, React 19, TypeScript 5.9, Vinext/Cloudflare Worker, pytest, Vitest.

---

## File responsibilities

- `contracts/v1/ai/*.schema.json`: closed contracts for the private AI projection and provider output.
- `contracts/v1/reports/normalized-trace-report.schema.json`: deterministic internal core report with no provider prose, clock, or report version.
- `contracts/v1/reports/analysis-report.schema.json`: backward-compatible public `AnalysisReport 1.0|1.1` contract.
- `services/api/src/perfpilot_api/reports/contracts.py`: cached JSON Schema validators and canonical JSON serialization.
- `services/api/src/perfpilot_api/reports/normalizer.py`: pure SmartPerfetto-to-core normalization and stable UUIDv5 assignment.
- `services/api/src/perfpilot_api/reports/projection.py`: pure, bounded, privacy-safe AI projection builder.
- `services/api/src/perfpilot_api/reports/writer.py`: immutable analysis-level `ReportVersion` publication and read validation.
- `services/api/src/perfpilot_api/ai/openai_compatible.py`: one bounded `chat/completions` request with no tools, redirects, or raw logging.
- `services/api/src/perfpilot_api/ai/synthesis.py`: structural, reference, numeric, actionability, and privacy validation of provider output.
- `services/api/src/perfpilot_api/ai/prompts/perfpilot-synthesis-v1.txt`: versioned system instruction loaded as a package resource.
- `services/api/src/perfpilot_api/services/canonical_result_reader.py`: tenant-version- and S3-VersionId-bound canonical artifact reader.
- `services/api/src/perfpilot_api/services/synthesis_artifacts.py`: immutable private JSON artifacts for projection and validated synthesis.
- `services/api/src/perfpilot_api/services/synthesis_executions.py`: control-plane generations, invocation audit, CAS recovery, and manual rerun reservations.
- `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`: durable event claims, leases, coordinator, retries, and pipeline advancement.
- `services/api/src/perfpilot_api/workers/synthesis_runtime.py`: production-only dependency composition and secret-safe cleanup.
- `services/api/src/perfpilot_api/services/trace_executions.py`: keep the parent in `analyzing` while synthesis is required and expose the SmartPerfetto stage.
- `services/api/src/perfpilot_api/services/analyses.py`: report selection, stage projection, rerun authorization, and terminal parent remediation.
- `services/api/src/perfpilot_api/api/analyses.py`: stable HTTP validation and error mapping only.
- `app/lib/perfpilot-api.ts`: closed browser types and API methods for status, reports, and synthesis reruns.
- `app/components/analysis-progress.tsx`: four real stages and polling lifecycle.
- `app/components/analysis-report.tsx`: concise report rendering with no static fallback.

## Invariants used by every task

- AI never receives raw Trace, HPROF, log, source, attachment content, object coordinates, URLs, external SmartPerfetto IDs, conversation history, query history, analysis notes, credentials, or provider errors.
- SmartPerfetto is the only source of metrics, thresholds, findings, severity, status, confidence, and evidence. AI may only select, explain, prioritize, recommend, and define a bounded retest plan against existing IDs.
- Every object-store read supplies the persisted `VersionId` and verifies MIME, length, SHA-256, artifact identity, analysis identity, execution identity, and tenant resource version.
- Projection bytes are at most 256 KiB; provider response bytes are at most 128 KiB; one synthesis generation has a 120-second wall-clock deadline and at most two provider attempts.
- Invalid provider output is never persisted. A validated candidate is persisted before its artifact ID, checksum, and one `report_generated_at` are bound in control state.
- Device report assembly remains on `AnalysisReport 1.0`; only new Trace reports use `1.1` and require a `synthesis` section.
- Every completed task below ends with its own commit and `git push` on `feature/perfpilot-ai-synthesis`.

### Task 1: Freeze the normalized, projection, synthesis, and public report contracts

**Files:**
- Create: `contracts/v1/reports/normalized-trace-report.schema.json`
- Create: `contracts/v1/ai/analysis-projection.schema.json`
- Create: `contracts/v1/ai/synthesis-output.schema.json`
- Modify: `contracts/v1/reports/analysis-report.schema.json`
- Create: `contracts/v1/examples/normalized-trace-report.valid.json`
- Create: `contracts/v1/examples/analysis-projection.valid.json`
- Create: `contracts/v1/examples/synthesis-output.valid.json`
- Create: `contracts/v1/examples/analysis-report.trace-ai.valid.json`
- Create: `services/api/src/perfpilot_api/reports/__init__.py`
- Create: `services/api/src/perfpilot_api/reports/contracts.py`
- Create: `services/api/tests/contract/test_ai_report_contracts.py`
- Modify: `services/api/tests/contract/test_contract_examples.py`
- Modify: `services/api/tests/unit/test_analysis_reports.py`

- [ ] **Step 1: Write RED contract tests for the three private documents**

Add cached Draft 2020-12 test validators and assert the checked-in examples pass. Mutate each example to prove `additionalProperties: false`, exact `schema_version`, finite numbers, canonical UUIDs, text limits, unique references, and maximum array lengths are enforced.

```python
@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("reports/normalized-trace-report.schema.json", "normalized-trace-report.valid.json"),
        ("ai/analysis-projection.schema.json", "analysis-projection.valid.json"),
        ("ai/synthesis-output.schema.json", "synthesis-output.valid.json"),
    ],
)
def test_ai_pipeline_examples_are_closed_and_valid(
    schema_name: str,
    example_name: str,
) -> None:
    validator = _validator(schema_name)
    example = _example(example_name)
    validator.validate(example)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**example, "unexpected": True})
```

- [ ] **Step 2: Run the contract tests and verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/contract/test_ai_report_contracts.py -q
```

Expected: collection or file loading fails because the new schemas and examples do not exist.

- [ ] **Step 3: Define the deterministic core and projection schemas**

Use the same closed `metric`, `finding`, `evidence`, `trace_health`, `trace_capabilities`, and public provenance definitions already established in `analysis-bundle.schema.json`. The normalized core has exactly these top-level fields and no time-dependent values:

```json
{
  "schema_version": "1.0",
  "analysis_id": "82000000-0000-4000-8000-000000000001",
  "analysis_mode": "trace_upload",
  "core_state": "complete",
  "scenario_reports": [],
  "limitations": [],
  "provenance": {
    "engine_id": "smartperfetto",
    "adapter_version": "1.0.0",
    "engine_commit_sha": "1111111111111111111111111111111111111111",
    "engine_image_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "source_contract": "workspace-agent-v1",
    "result_contract_version": "1.0.0",
    "canonical_artifact_id": "85000000-0000-4000-8000-000000000001",
    "canonical_sha256_b64": "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
    "normalizer_version": "smartperfetto-normalizer-1"
  }
}
```

The projection repeats only public IDs and bounded facts, sorts arrays by stable ID, carries the authoritative `analysis_profile` and normalized `question`, and has no generated timestamp, provider, attempt, storage, or external ID fields.

- [ ] **Step 4: Define the strict synthesis schema**

Require exactly `schema_version`, `executive_summary`, `top_findings`, `recommendations`, `retest_plan`, and `limitations`. Encode the two retest branches with `oneOf` so their references and outcome enums cannot mix:

```json
{
  "oneOf": [
    {
      "properties": {
        "mode": {"const": "verify_metric"},
        "metric_ids": {"type": "array", "minItems": 1, "uniqueItems": true},
        "limitation_ids": {"type": "array", "maxItems": 0},
        "success_condition": {"enum": ["meet_existing_threshold", "improve_from_baseline"]},
        "failure_condition": {"const": "threshold_missed"}
      }
    },
    {
      "properties": {
        "mode": {"const": "collect_evidence"},
        "metric_ids": {"type": "array", "maxItems": 0},
        "limitation_ids": {"type": "array", "minItems": 1, "uniqueItems": true},
        "success_condition": {"const": "evidence_collected"},
        "failure_condition": {"const": "evidence_missing"}
      }
    }
  ]
}
```

Set the approved limits: summary 2,000 characters, top findings 5, recommendations 10, retest items 5, limitations 20, and every narrative field 2,000 characters.

- [ ] **Step 5: Make `AnalysisReport 1.1` backward compatible and unambiguous**

Keep every current 1.0 fixture valid. Add a version branch with these exact rules:

- `1.0` forbids `synthesis` and retains current device/legacy Trace behavior.
- `1.1` requires `analysis_mode=trace_upload`, one to three unique scenarios in `startup`, `scroll`, `memory_cycle` order, and a required `synthesis` object.
- `synthesis.state=completed` requires the validated output and full non-secret provenance.
- `synthesis.state=failed` requires a stable failure code and requires `output` and synthesis artifact ID to be null.
- `partially_completed` is valid when either a core scenario is partial or synthesis failed.

- [ ] **Step 6: Add one canonical validation boundary**

Implement cached loading and canonical JSON serialization in `reports/contracts.py`; error strings must not contain the invalid document.

```python
class ReportContractError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("report contract is invalid")


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ReportContractError from None


def validate_contract(name: ContractName, value: object) -> dict[str, object]:
    copied = json.loads(canonical_json_bytes(value))
    try:
        _validator(name).validate(copied)
    except ValidationError:
        raise ReportContractError from None
    if not isinstance(copied, dict):
        raise ReportContractError
    return copied
```

- [ ] **Step 7: Run GREEN, lint, commit, and push**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/contract/test_ai_report_contracts.py \
  services/api/tests/contract/test_contract_examples.py \
  services/api/tests/unit/test_analysis_reports.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/reports services/api/tests/contract \
  services/api/tests/unit/test_analysis_reports.py
git diff --check
git add contracts/v1 services/api/src/perfpilot_api/reports services/api/tests
git commit -m "feat: define PerfPilot AI report contracts"
git push
```

Expected: all focused tests pass, Ruff reports no errors, and the pushed branch contains the first implementation commit after the specification and plan commits.

### Task 2: Add control audit records and tenant report columns

**Files:**
- Create: `services/api/src/perfpilot_api/db/control/models/synthesis.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/__init__.py`
- Modify: `services/api/src/perfpilot_api/db/control/models/events.py`
- Modify: `services/api/src/perfpilot_api/db/tenant/models/reports.py`
- Create: `services/api/migrations/control/versions/0008_ai_synthesis.py`
- Create: `services/api/migrations/tenant/versions/0007_analysis_report_versions.py`
- Modify: `services/api/tests/integration/test_migrations.py`

- [ ] **Step 1: Write RED migration inventory and constraint tests**

Add `synthesis_executions` and `ai_invocations` to `CONTROL_TABLES`, and add nullable positive `subject_version` to the existing outbox inventory so new synthesis events can bind the exact authority version without adding a payload. Assert forbidden content columns such as `prompt`, `question`, `payload`, `response`, `endpoint`, `credential_reference`, `object_key`, and `external_error` do not exist. Assert both new tables are rejected when IDs, generations, attempts, states, timestamps, or token totals are inconsistent.

For tenant `report_versions`, assert these three valid shapes and all invalid hybrids:

```python
scenario_content = {
    "scenario_result_id": scenario_id,
    "bundle": bundle,
    "bundle_sha256_b64": checksum,
    "report": None,
    "report_sha256_b64": None,
}
analysis_content = {
    "scenario_result_id": None,
    "bundle": None,
    "bundle_sha256_b64": None,
    "report": report,
    "report_sha256_b64": checksum,
}
metadata_only = {
    "scenario_result_id": None,
    "bundle": None,
    "bundle_sha256_b64": None,
    "report": None,
    "report_sha256_b64": None,
}
```

- [ ] **Step 2: Run migration tests and verify RED**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/integration/test_migrations.py -q
```

Expected: table inventory and new report-column assertions fail.

- [ ] **Step 3: Implement the control models and migration**

Create `SynthesisExecution` with the approved metadata fields and `AIInvocation` with one row per provider attempt. Use `ForeignKeyConstraint(("analysis_id", "team_id"), ("global_jobs.id", "global_jobs.team_id"))`, `ON DELETE CASCADE`, positive version/generation/resource constraints, stable-code patterns, nonnegative token/latency constraints, and these uniqueness rules:

```python
UniqueConstraint(
    "analysis_id",
    "source_execution_id",
    "generation",
    name="uq_synthesis_executions_source_generation",
)
UniqueConstraint(
    "synthesis_execution_id",
    "attempt_number",
    name="uq_ai_invocations_execution_attempt",
)
```

The synthesis row has exactly: IDs for itself/team/analysis/source execution; tenant resource version and generation; state/version; request fingerprint; normalizer and report-worker versions; projection checksum and optional projection artifact ID; provider protocol/name/model; prompt version/checksum; attempt count; optional prompt/completion/total tokens and latency; optional stable error; optional candidate artifact/checksum; optional report time/version ID; and started/completed timestamps. The invocation row has exactly: IDs for itself/synthesis/team/analysis; attempt number; request fingerprint; provider protocol/name/model; prompt version; state; optional token/latency/error values; and started/completed timestamps. Invocation state is `running|succeeded|failed`, attempt number is 1 or 2, and token totals must add up when all three values are present.

Add partial outbox uniqueness indexes for `engine_result_ready` by source execution and `analysis_synthesis_requested` by synthesis execution. Store event subjects as the relevant execution UUID, not artifact coordinates. Existing event types retain `subject_version=NULL`; both new event types require an application-validated positive subject version.

- [ ] **Step 4: Implement tenant report storage without rewriting old rows**

Add nullable `report`, `report_sha256_b64`, `ai_projection_artifact_id`, and `ai_synthesis_artifact_id`. Replace the bundle constraint with an explicit mutually exclusive content constraint:

```sql
NOT (bundle IS NOT NULL AND report IS NOT NULL)
AND ((bundle IS NULL) = (bundle_sha256_b64 IS NULL))
AND ((report IS NULL) = (report_sha256_b64 IS NULL))
AND (bundle IS NULL OR scenario_result_id IS NOT NULL)
AND (report IS NULL OR scenario_result_id IS NULL)
```

Do not backfill JSON. Downgrade must lock `report_versions` and fail before dropping columns when any new report content or AI artifact reference exists.

- [ ] **Step 5: Run upgrade, downgrade, ORM parity, and GREEN**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/integration/test_migrations.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/db \
  services/api/migrations \
  services/api/tests/integration/test_migrations.py
git diff --check
git add services/api/src/perfpilot_api/db services/api/migrations services/api/tests/integration/test_migrations.py
git commit -m "feat: persist AI synthesis execution metadata"
git push
```

Expected: both migration trees reach their new heads, autogenerate comparison is empty, downgrade preflights are tested, and all existing metadata-only rows remain valid.

### Task 3: Read exact canonical bytes and normalize SmartPerfetto facts deterministically

**Files:**
- Create: `services/api/src/perfpilot_api/services/canonical_result_reader.py`
- Create: `services/api/src/perfpilot_api/reports/normalizer.py`
- Create: `services/api/tests/fixtures/canonical_results/smartperfetto-result-contract-1.0.0.json`
- Create: `services/api/tests/unit/test_canonical_result_reader.py`
- Create: `services/api/tests/unit/test_smartperfetto_report_normalizer.py`
- Create: `services/api/tests/integration/test_canonical_result_reader_s3.py`

- [ ] **Step 1: Write RED reader tests around the exact storage boundary**

Create a finalized `engine_result` record and assert the reader calls S3 with all four immutable selectors:

```python
assert client.calls == [{
    "Bucket": "tenant-private-a",
    "Key": f"raw/analyses/{ANALYSIS_ID}/internal/engine-results/{ARTIFACT_ID}.json",
    "VersionId": "immutable-engine-result-v1",
    "ChecksumMode": "ENABLED",
}]
```

Reject a changed tenant resource version, non-finalized artifact, wrong analysis, wrong artifact kind, missing VersionId, delete marker, wrong MIME, incorrect length, checksum drift, body overrun, non-canonical JSON bytes, envelope identity mismatch, and cross-team guesses. Errors expose only `canonical result is unavailable` or `canonical result integrity failure`.

- [ ] **Step 2: Write RED pure-normalizer tests**

Build one fixture with `resultContract.version=1.0.0`, startup and scroll data envelopes, diagnostics, actions, `evidence_contract@1`, `claim_verifier@1`, and `identity_contract@1`. Assert:

- identical semantic input with different object insertion order yields identical core bytes;
- IDs remain stable and arrays are sorted by UUID string;
- only verified or explicitly partial claim references become evidence;
- missing measurement values become `insufficient_data`, never zero;
- diagnostic severity/status/confidence maps through fixed tables;
- unknown result contract versions, non-finite numbers, duplicate source IDs, and unsupported envelope shapes fail closed;
- conversation, query history, analysis notes, echoed query, source/tool call IDs, report/session/workspace/run IDs, and arbitrary findings never enter the core.

- [ ] **Step 3: Run both test files and verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_canonical_result_reader.py \
  services/api/tests/unit/test_smartperfetto_report_normalizer.py -q
```

Expected: imports fail because the reader and normalizer do not exist.

- [ ] **Step 4: Implement a version-bound reader**

Return a frozen value object that hides bytes and storage metadata from `repr`:

```python
@dataclass(frozen=True, slots=True)
class LoadedCanonicalResult:
    team_id: UUID
    analysis_id: UUID
    execution_id: UUID
    artifact_id: UUID
    tenant_resource_version: int
    sha256_b64: str = field(repr=False)
    document: dict[str, object] = field(repr=False)
    canonical_bytes: bytes = field(repr=False)
```

Resolve the tenant by team, require the execution's pinned resource version, reload the tenant artifact through that route, then `get_object` with the persisted key and `VersionId`. Read at most `record.size_bytes + 1`, close the body in `finally`, verify metadata and checksum with `hmac.compare_digest`, validate `canonical-engine-result.schema.json`, and require `canonical_json_bytes(document) == body`.

- [ ] **Step 5: Implement the pure SmartPerfetto normalizer**

Use fixed mappings and namespaces checked into code, not provider prose:

```python
_SEVERITY = {
    "critical": "critical",
    "high": "critical",
    "warning": "warning",
    "medium": "warning",
    "low": "informational",
    "info": "informational",
}
_SCENARIO_ORDER = {"startup": 0, "scroll": 1, "memory_cycle": 2}
_NORMALIZER_VERSION = "smartperfetto-normalizer-1"


def _stable_id(kind: str, analysis_id: UUID, source_id: str) -> UUID:
    return uuid5(_NORMALIZED_REPORT_NAMESPACE, f"{analysis_id}:{kind}:{source_id}")
```

Require the canonical engine/source pair `smartperfetto` + `workspace-agent-v1`, result state `completed|insufficient_data`, and `resultContract.version == "1.0.0"`. Accept measurements only from supported typed data-envelope columns with explicit unit and evidence identity. Use `claimVerificationResult` to cap confidence; unverified source diagnostics may only create limitations. Preserve canonical artifact checksum and engine provenance, but never external upstream IDs.

- [ ] **Step 6: Validate the finished core against its schema and byte stability**

The public entry point returns both a copied document and canonical bytes:

```python
@dataclass(frozen=True, slots=True)
class NormalizedTraceReport:
    document: dict[str, object] = field(repr=False)
    canonical_bytes: bytes = field(repr=False)
    sha256_b64: str = field(repr=False)


def normalize_smartperfetto_result(
    source: LoadedCanonicalResult,
) -> NormalizedTraceReport:
    document = _build_core(source)
    validated = validate_contract("normalized-trace-report", document)
    payload = canonical_json_bytes(validated)
    return NormalizedTraceReport(validated, payload, sha256_b64(payload))
```

- [ ] **Step 7: Run unit, S3 integration, lint, commit, and push**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_canonical_result_reader.py \
  services/api/tests/unit/test_smartperfetto_report_normalizer.py \
  services/api/tests/integration/test_canonical_result_reader_s3.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/services/canonical_result_reader.py \
  services/api/src/perfpilot_api/reports/normalizer.py \
  services/api/tests/unit/test_canonical_result_reader.py \
  services/api/tests/unit/test_smartperfetto_report_normalizer.py \
  services/api/tests/integration/test_canonical_result_reader_s3.py
git diff --check
git add services/api/src/perfpilot_api/services/canonical_result_reader.py \
  services/api/src/perfpilot_api/reports/normalizer.py \
  services/api/tests
git commit -m "feat: normalize canonical SmartPerfetto results"
git push
```

Expected: the exact same canonical input always produces the exact same normalized bytes and IDs, and every storage mismatch fails before normalization.

### Task 4: Build and persist the private AI projection and validated synthesis artifacts

**Files:**
- Create: `services/api/src/perfpilot_api/reports/privacy.py`
- Create: `services/api/src/perfpilot_api/reports/projection.py`
- Create: `services/api/src/perfpilot_api/services/synthesis_artifacts.py`
- Create: `services/api/tests/unit/test_ai_projection.py`
- Create: `services/api/tests/unit/test_synthesis_artifacts.py`
- Create: `services/api/tests/integration/test_synthesis_artifact_repository.py`

- [ ] **Step 1: Write RED projection allowlist and privacy tests**

Pass a normalized core with safe measurements and inject markers into every excluded source field. Assert the projection contains only the authoritative question, source contract metadata, public scenario/metric/finding/evidence/limitation IDs, measurement values, units, thresholds, and bounded summaries.

Parameterize strings containing signed HTTP URLs, user-info URLs, database URLs, `s3://`/`gs://` URIs, bearer/basic credentials, credential assignments, PEM private-key markers, POSIX paths, Windows absolute paths, percent-encoded paths, and traversal segments. Each must raise a redacted `ProjectionPrivacyError` before persistence.

```python
@pytest.mark.parametrize(
    "private_value",
    [
        "https://objects.invalid/a?X-Amz-Signature=secret",
        "postgresql://user:secret@db.invalid/app",
        "s3://private-bucket/customer/trace",
        "Bearer private-token-value",
        "/srv/private/customer.trace",
        r"C:\\private\\customer.trace",
        "%2Fsrv%2Fprivate%2Fcustomer.trace",
        "../private/customer.trace",
    ],
)
def test_projection_rejects_private_strings(private_value: str) -> None:
    with pytest.raises(ProjectionPrivacyError, match="projection contains private data"):
        build_ai_projection(_core_with_summary(private_value), analysis_profile="auto", question=None)
```

- [ ] **Step 2: Write RED artifact identity and immutability tests**

Require deterministic projection identity from canonical artifact ID plus normalizer version, and deterministic synthesis identity from synthesis execution ID plus validated candidate checksum. Test reserve/finalize/reload, exact VersionId verification, cross-team isolation, concurrent identical writes converging, and concurrent differing bytes producing an integrity conflict.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_synthesis_artifacts.py -q
```

Expected: both modules are missing.

- [ ] **Step 4: Implement the recursive privacy scanner and deterministic projection**

Scan keys and values after NFKC normalization and up to eight percent-decode passes. Reject cycles, non-JSON types, non-finite numbers, more than 64 levels, more than 200,000 nodes, and overlong strings. Do not redact and continue; fail the whole projection so no accidental partial disclosure is sent.

```python
def build_ai_projection(
    core: NormalizedTraceReport,
    *,
    analysis_profile: Literal["auto", "startup", "scroll"],
    question: str | None,
    max_bytes: int = 256 * 1024,
) -> AIProjection:
    normalized_question = normalize_authoritative_question(question)
    document = _project_allowlisted_core(core.document, analysis_profile, normalized_question)
    reject_private_json(document)
    validated = validate_contract("analysis-projection", document)
    payload = canonical_json_bytes(validated)
    if len(payload) > max_bytes:
        raise ProjectionSizeError("AI projection exceeds the configured limit")
    return AIProjection(validated, payload, sha256_b64(payload))
```

Normalize question with `str.strip()`, reject empty post-trim input, and cap Python character count at 2,000. Treat the question as an isolated `question` value; do not copy the SmartPerfetto echoed query.

- [ ] **Step 5: Implement a dedicated immutable artifact store**

Support only `ai_projection` and `ai_synthesis_result`, both tenant-owned analysis artifacts with `application/json`, positive size, canonical SHA-256, fixed retention, deterministic object keys, and internal idempotency keys:

```python
def projection_artifact_id(canonical_artifact_id: UUID, normalizer_version: str) -> UUID:
    return uuid5(_PROJECTION_NAMESPACE, f"{canonical_artifact_id}:{normalizer_version}")


def synthesis_artifact_id(synthesis_execution_id: UUID, checksum: str) -> UUID:
    return uuid5(_SYNTHESIS_NAMESPACE, f"{synthesis_execution_id}:{checksum}")


def artifact_key(analysis_id: UUID, artifact_id: UUID, kind: ArtifactKind) -> str:
    return f"raw/analyses/{analysis_id}/internal/{kind.replace('_', '-')}/{artifact_id}.json"
```

Follow the existing engine-result two-phase write: route and fence, reserve tenant row, put bytes, verify returned version/checksum, finalize by CAS, then exact-version readback. Never return bucket, key, or VersionId in a public value object.

- [ ] **Step 6: Run GREEN, integration, lint, commit, and push**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_synthesis_artifacts.py \
  services/api/tests/integration/test_synthesis_artifact_repository.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/reports/privacy.py \
  services/api/src/perfpilot_api/reports/projection.py \
  services/api/src/perfpilot_api/services/synthesis_artifacts.py \
  services/api/tests/unit/test_ai_projection.py \
  services/api/tests/unit/test_synthesis_artifacts.py \
  services/api/tests/integration/test_synthesis_artifact_repository.py
git diff --check
git add services/api/src/perfpilot_api/reports \
  services/api/src/perfpilot_api/services/synthesis_artifacts.py \
  services/api/tests
git commit -m "feat: persist privacy-safe AI projections"
git push
```

Expected: only allowlisted, bounded projection bytes can be persisted, and immutable artifact retries converge on one exact S3 version.

### Task 5: Add secure AI settings, versioned prompt loading, and the OpenAI-compatible adapter

**Files:**
- Modify: `services/api/src/perfpilot_api/config.py`
- Create: `services/api/src/perfpilot_api/ai/__init__.py`
- Create: `services/api/src/perfpilot_api/ai/prompts/__init__.py`
- Create: `services/api/src/perfpilot_api/ai/prompts/perfpilot-synthesis-v1.txt`
- Create: `services/api/src/perfpilot_api/ai/prompt.py`
- Create: `services/api/src/perfpilot_api/ai/openai_compatible.py`
- Modify: `services/api/tests/unit/test_app.py`
- Create: `services/api/tests/unit/test_ai_prompt.py`
- Create: `services/api/tests/unit/test_openai_compatible_provider.py`

- [ ] **Step 1: Write RED configuration tests**

Add the approved settings with secure defaults: AI disabled, development base URL `http://127.0.0.1:4010/v1/`, provider `development-fake`, model `fake-json-model`, a development-only secret reference, current SmartPerfetto-style timeout defaults, 256 KiB projection limit, and 128 KiB response limit.

For production with AI enabled, reject HTTP, missing trailing API root semantics, username/password, query, fragment, loopback, localhost, unspecified, link-local, multicast, and development secret reference. Preserve a valid path such as `/openai/v1/`. Redact all invalid inputs from `ValidationError` text and repr.

- [ ] **Step 2: Write RED prompt and provider protocol tests**

Assert the prompt resource has a fixed version and SHA-256, explicitly treats the user question as untrusted data, forbids creation of facts/IDs/tools, and instructs the model to return only the synthesis schema.

With `httpx.MockTransport`, assert exactly one request to `<base-root>/chat/completions` with:

```python
assert request_json == {
    "model": "provider-model-1",
    "stream": False,
    "temperature": 0,
    "messages": [
        {"role": "system", "content": prompt.system_instruction},
        {"role": "user", "content": projection.canonical_bytes.decode("utf-8")},
    ],
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "perfpilot_synthesis_1_0",
            "strict": True,
            "schema": synthesis_schema,
        },
    },
}
```

Assert there are no `tools`, `functions`, files, URLs, remote MCP, extra messages, or provider-specific fields.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_app.py \
  services/api/tests/unit/test_ai_prompt.py \
  services/api/tests/unit/test_openai_compatible_provider.py -q
```

Expected: new settings and AI package imports fail.

- [ ] **Step 4: Implement production URL validation and settings**

Add:

```python
ai_enabled: bool = False
ai_base_url: SecretStr = SecretStr("http://127.0.0.1:4010/v1/")
ai_provider_name: str = Field(default="development-fake", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ai_model: str = Field(default="fake-json-model", min_length=1, max_length=128)
ai_credential_reference: SecretStr = SecretStr("development-only-ai-credential-reference")
ai_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=120, allow_inf_nan=False)
ai_read_timeout_seconds: float = Field(default=60.0, gt=0, le=120, allow_inf_nan=False)
ai_write_timeout_seconds: float = Field(default=30.0, gt=0, le=120, allow_inf_nan=False)
ai_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=120, allow_inf_nan=False)
ai_max_projection_bytes: int = Field(default=256 * 1024, ge=1024, le=256 * 1024, strict=True)
ai_max_response_bytes: int = Field(default=128 * 1024, ge=1024, le=128 * 1024, strict=True)
```

Resolve the host with `ipaddress` when literal. Treat DNS egress allowlisting as a deployment control and document it in Task 11; configuration validation must still reject literal unsafe ranges. Production worker startup additionally requires an allowlisted provider hostname from `PERFPILOT_AI_EGRESS_HOSTS` containing the normalized base URL host.

- [ ] **Step 5: Implement the prompt loader and bounded adapter**

Load bytes through `importlib.resources`, require UTF-8, nonempty text, a maximum of 32 KiB, and calculate SHA-256 from the exact bytes. The provider adapter receives a resolved `SecretStr` token but never stores it on a public record or includes it in repr.

Use `client.stream()` with method `POST`, the validated endpoint, authorization headers, and the exact JSON request shown above. Reject redirects and non-200 responses, accumulate at most `max_response_bytes + 1`, decode strict UTF-8, and validate the outer OpenAI-compatible envelope. Accept exactly one choice with `finish_reason="stop"`, string `message.content`, no refusal, and no tool calls. Return candidate JSON bytes plus nonnegative usage and latency only.

Map failures to stable codes:

| Condition | Code | Retryable |
| --- | --- | --- |
| connect/read/write/pool timeout | `ai_timeout` | yes |
| 429 | `ai_rate_limited` | yes |
| 5xx | `ai_provider_unavailable` | yes |
| 401/403 | `ai_authentication_failed` | no |
| redirect/protocol/oversize/UTF-8/envelope | `ai_protocol_invalid` | no |

- [ ] **Step 6: Run GREEN, lint, commit, and push**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_app.py \
  services/api/tests/unit/test_ai_prompt.py \
  services/api/tests/unit/test_openai_compatible_provider.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/config.py \
  services/api/src/perfpilot_api/ai \
  services/api/tests/unit/test_app.py \
  services/api/tests/unit/test_ai_prompt.py \
  services/api/tests/unit/test_openai_compatible_provider.py
git diff --check
git add services/api/src/perfpilot_api/config.py \
  services/api/src/perfpilot_api/ai \
  services/api/tests/unit
git commit -m "feat: add OpenAI-compatible synthesis provider"
git push
```

Expected: fake transport tests cover all accepted and rejected response shapes without any network request or real credential.

### Task 6: Validate AI semantics, references, numbers, and privacy

**Files:**
- Create: `services/api/src/perfpilot_api/ai/synthesis.py`
- Create: `services/api/tests/unit/test_ai_synthesis_validator.py`

- [ ] **Step 1: Write RED semantic validation tests**

Start from the valid projection and synthesis fixtures, then mutate one rule at a time. Reject:

- unknown finding, evidence, metric, scenario, or limitation references;
- a top finding whose evidence does not support that finding;
- a recommendation with no actionable `confirmed|suspected` finding or no evidence;
- a recommendation against `insufficient_data|invalid_capture`;
- a verify-metric retest with no metric, a metric outside the scenario, or a new numeric target;
- a collect-evidence retest with no existing limitation;
- any numeric literal not present in the projection measurement/threshold whitelist;
- IDs supplied for recommendation or retest records;
- any private string caught by `reject_private_json`.

```python
with pytest.raises(SynthesisValidationError) as caught:
    validate_synthesis_output(
        projection=projection,
        candidate={
            **valid_candidate,
            "recommendations": [{
                "priority": "p0",
                "title": "Fix it",
                "action": "Reduce to 16 ms",
                "expected_effect": "Meet 16 ms",
                "finding_ids": [str(INSUFFICIENT_FINDING_ID)],
                "evidence_ids": [],
            }],
        },
    )
assert str(caught.value) == "AI synthesis output is invalid"
assert "16 ms" not in repr(caught.value)
```

- [ ] **Step 2: Verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_ai_synthesis_validator.py -q
```

Expected: validator import fails.

- [ ] **Step 3: Implement schema-first and projection-indexed validation**

Build an immutable index from projection IDs and relationships. Schema validation must run before semantic traversal. Parse candidate bytes as exactly one JSON document using strict UTF-8 and reject leading/trailing non-whitespace, duplicate JSON keys, `NaN`, `Infinity`, and oversized input.

```python
def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SynthesisValidationError
        result[key] = value
    return result


def parse_candidate(payload: bytes, max_bytes: int) -> dict[str, object]:
    if not 1 <= len(payload) <= max_bytes:
        raise SynthesisValidationError
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(SynthesisValidationError()),
        )
    except (UnicodeError, json.JSONDecodeError, SynthesisValidationError):
        raise SynthesisValidationError from None
    return validate_contract("synthesis-output", value)
```

- [ ] **Step 4: Enforce relation and numeric whitelists**

Index `finding -> evidence`, `scenario -> metrics`, actionable finding status, and the exact decimal spelling of every projection metric and threshold. Walk only narrative fields (`executive_summary`, user impact, titles, actions, expected effects, retest steps, and limitation explanations) with a numeric-token regex; reference UUIDs, schema versions, and enums are structural fields and are not scanned as prose. Every narrative numeric token must match the projection whitelist. This deliberately rejects model-created percentages, durations, counts, and target values even when they look reasonable.

Return canonical validated bytes and a copied document. Do not add IDs yet; Report Writer owns server-generated recommendation/retest identities.

- [ ] **Step 5: Run GREEN, lint, commit, and push**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_ai_synthesis_validator.py \
  services/api/tests/unit/test_ai_projection.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/ai/synthesis.py \
  services/api/tests/unit/test_ai_synthesis_validator.py
git diff --check
git add services/api/src/perfpilot_api/ai/synthesis.py \
  services/api/tests/unit/test_ai_synthesis_validator.py
git commit -m "feat: validate AI synthesis against measured facts"
git push
```

Expected: every AI statement remains tied to projection facts, and no invalid candidate is exposed outside the validator.

### Task 7: Create durable synthesis generations, invocation audit, and event coordination

**Files:**
- Create: `services/api/src/perfpilot_api/services/synthesis_executions.py`
- Modify: `services/api/src/perfpilot_api/services/engine_executions.py`
- Modify: `services/api/src/perfpilot_api/services/trace_executions.py`
- Create: `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`
- Create: `services/api/tests/unit/test_synthesis_execution_service.py`
- Modify: `services/api/tests/unit/test_engine_execution_service.py`
- Modify: `services/api/tests/unit/test_trace_execution_service.py`
- Create: `services/api/tests/integration/test_synthesis_execution_repository.py`
- Modify: `services/api/tests/integration/test_trace_orchestrator.py`

- [ ] **Step 1: Write RED finalization-event tests**

When AI synthesis is enabled, finalizing a `completed|insufficient_data` SmartPerfetto execution must atomically bind the canonical artifact and insert one deterministic `engine_result_ready` outbox event containing only team, analysis, source execution ID, event type, and `subject_version` equal to the exact post-finalization execution version. Replaying finalization returns the same execution and event.

When AI is explicitly disabled in development or test composition, retain the current direct terminal parent projection and do not create a 1.1 report. Production composition may not enable SmartPerfetto synthesis with AI disabled.

- [ ] **Step 2: Write RED generation and invocation repository tests**

Cover automatic generation 1, manual next-generation allocation, same-key replay, changed-request idempotency conflict, latest authoritative SmartPerfetto attempt enforcement, tenant resource version pinning, projection binding, invocation attempt 1 and 2, candidate binding, report timestamp binding, report binding, cancellation, lease loss, and concurrent callers converging.

Assert control rows never contain the question, endpoint, credential reference, prompt text, projection body, provider response, or external error.

- [ ] **Step 3: Verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_synthesis_execution_service.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/unit/test_trace_execution_service.py -q
```

Expected: generation service is missing and the current Trace service still terminalizes the parent as soon as SmartPerfetto completes.

- [ ] **Step 4: Publish `engine_result_ready` in the engine finalization transaction**

Add deterministic event identity and a repository argument `schedule_synthesis: bool`. Validate that only SmartPerfetto Trace executions can request synthesis. Insert with `ON CONFLICT DO NOTHING`, then reload and compare every authoritative field before committing.

```python
def engine_result_ready_event_id(execution_id: UUID) -> UUID:
    return uuid5(_SYNTHESIS_EVENT_NAMESPACE, f"engine_result_ready:{execution_id}")
```

Configure `EngineExecutionService` from settings. In `TraceExecutionService.advance()`, keep the parent `analyzing` after successful/insufficient SmartPerfetto completion when synthesis is scheduled; continue to use current terminal behavior when the feature is explicitly off.

- [ ] **Step 5: Implement generation allocation and request fingerprints**

The fingerprint is canonical JSON over exactly the approved non-secret fields:

```python
fingerprint_input = {
    "canonical_sha256_b64": canonical_checksum,
    "tenant_resource_version": source.tenant_resource_version,
    "question_sha256": sha256_hex(normalized_question.encode("utf-8")),
    "normalizer_version": NORMALIZER_VERSION,
    "projection_contract_version": "1.0",
    "report_contract_version": "1.1",
    "prompt_template_version": prompt.version,
    "prompt_template_sha256_b64": prompt.sha256_b64,
    "report_worker_image_digest": worker_image_digest,
    "provider_protocol": "chat-completions-json-schema-v1",
    "provider_name": provider_name,
    "model": model,
    "inference_config_hash": inference_config_hash,
    "generation": generation,
}
```

Credentials and credential rotation are absent. Manual reruns use the existing `IdempotencyKey` table with operation `create_synthesis_run`, team scope, canonical request hash, 30-day expiry, and `response_resource_id=synthesis_execution_id`.

- [ ] **Step 6: Implement separate coordinator and work events**

The coordinator claims only `engine_result_ready`, verifies its `subject_version` against the latest source attempt and bound canonical artifact, allocates or reloads automatic generation 1, writes one `analysis_synthesis_requested` outbox event with the synthesis record version, and completes the source event. It never reads tenant artifact bytes.

The synthesis work queue claims only `analysis_synthesis_requested`, uses the existing renewable `WorkerClaim` shape, and rejects mismatched team/analysis/subject rows. Expired claims can be reclaimed; one active global-job claim prevents Trace and synthesis workers from writing the same parent concurrently.

- [ ] **Step 7: Run repository and orchestrator GREEN**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_synthesis_execution_service.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/unit/test_trace_execution_service.py \
  services/api/tests/integration/test_synthesis_execution_repository.py \
  services/api/tests/integration/test_trace_orchestrator.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/services/engine_executions.py \
  services/api/src/perfpilot_api/services/trace_executions.py \
  services/api/src/perfpilot_api/services/synthesis_executions.py \
  services/api/src/perfpilot_api/workers/synthesis_orchestrator.py \
  services/api/tests
git diff --check
git add services/api/src/perfpilot_api/services \
  services/api/src/perfpilot_api/workers/synthesis_orchestrator.py \
  services/api/tests
git commit -m "feat: coordinate durable AI synthesis generations"
git push
```

Expected: one canonical engine result creates one automatic generation, duplicate events are harmless, and the parent remains `analyzing` until the report stage owns terminalization.

### Task 8: Run the recoverable pipeline and publish immutable `AnalysisReport 1.1`

**Files:**
- Create: `services/api/src/perfpilot_api/reports/writer.py`
- Modify: `services/api/src/perfpilot_api/services/synthesis_executions.py`
- Modify: `services/api/src/perfpilot_api/workers/synthesis_orchestrator.py`
- Create: `services/api/src/perfpilot_api/workers/synthesis_runtime.py`
- Modify: `services/api/src/perfpilot_api/workers/__init__.py`
- Modify: `services/api/src/perfpilot_api/domain/transitions.py`
- Modify: `services/api/pyproject.toml`
- Create: `services/api/tests/unit/test_analysis_report_writer.py`
- Create: `services/api/tests/unit/test_synthesis_worker.py`
- Create: `services/api/tests/unit/test_synthesis_worker_runtime.py`
- Create: `services/api/tests/integration/test_analysis_report_repository.py`
- Create: `services/api/tests/integration/test_synthesis_orchestrator.py`

- [ ] **Step 1: Write RED report-writer tests**

Given deterministic core, a bound projection artifact, and either a validated candidate or stable synthesis failure, assert the writer creates one analysis-level report row with:

- `schema_version="1.1"` and ordered Trace scenario reports;
- server-generated stable recommendation/retest IDs;
- the exact control-bound `generated_at` and generation;
- source canonical, projection, optional synthesis artifact, normalizer, prompt, provider, model, worker image, usage, and latency provenance;
- canonical report bytes and matching `report_sha256_b64`;
- `source_artifact_id` set to the canonical engine result.

Assert an existing row with the same deterministic report ID and checksum is an idempotent success, while different bytes for the same identity raise `ReportIntegrityError` and never overwrite.

- [ ] **Step 2: Write RED crash-window and retry tests**

Exercise the worker as a state machine with injected crashes after each durable boundary:

1. canonical read;
2. projection artifact write;
3. projection binding;
4. provider response;
5. validated candidate artifact write;
6. candidate/checksum/report-time binding;
7. tenant report insert;
8. engine report-version binding;
9. synthesis completion;
10. parent terminalization.

On recovery, assert the worker does not call the provider after step 6, does not create a second report, and uses the identical `report_generated_at`. A crash after the provider responds but before the validated artifact is durable may call the provider again; the test documents this billing limitation while still proving one report result.

- [ ] **Step 3: Write RED failure classification tests**

Test at most two attempts. Retry only timeout, 429, 5xx, and invalid candidate output. The second invalid-output request contains only a stable code such as `ai_output_invalid`, never the first candidate. Do not retry authentication/configuration errors. After AI exhaustion, write a valid core report with `synthesis.state="failed"`, set the parent `partially_completed`, and keep the failure text stable and content-free.

- [ ] **Step 4: Verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_analysis_report_writer.py \
  services/api/tests/unit/test_synthesis_worker.py \
  services/api/tests/unit/test_synthesis_worker_runtime.py -q
```

Expected: writer/runtime modules are missing and orchestration has no pipeline advancer.

- [ ] **Step 5: Implement the report composer and tenant transaction**

Use deterministic identities derived from the synthesis execution and stable list position:

```python
def public_item_id(synthesis_execution_id: UUID, kind: str, index: int) -> UUID:
    return uuid5(_PUBLIC_REPORT_ITEM_NAMESPACE, f"{synthesis_execution_id}:{kind}:{index}")


def report_version_id(synthesis_execution_id: UUID) -> UUID:
    return uuid5(_REPORT_VERSION_NAMESPACE, str(synthesis_execution_id))
```

Construct output by copying validated core and synthesis documents, adding IDs and provenance, then validating `analysis-report` before opening the insert transaction. Inside the routed tenant transaction, lock the owning `Analysis` row first, select `next_report_version=max(existing)+1`, insert by deterministic ID, and compare content/checksum on conflict. Locking the parent serializes the empty-table case that row-range locking cannot protect. Preserve scenario-level and metadata-only rows.

- [ ] **Step 6: Implement worker advancement as explicit durable phases**

`SynthesisPipeline.advance()` follows authoritative `SynthesisExecution` state instead of local memory:

```python
if execution.projection_artifact_id is None:
    return await self._normalize_and_bind_projection(execution)
if execution.candidate_artifact_id is None and execution.stable_error_code is None:
    return await self._invoke_validate_and_bind_candidate(execution)
if execution.report_generated_at is None:
    return await self._bind_report_time(execution)
if execution.report_version_id is None:
    return await self._write_and_bind_report(execution)
return await self._complete_and_project_parent(execution)
```

Each call performs one bounded unit of work and returns `pending|running|succeeded|failed|canceled` plus an optional retry delay. Recheck cancellation and lease ownership before provider calls and every write.

- [ ] **Step 7: Bind candidate before report time and finish by CAS**

On provider success: validate output, write `ai_synthesis_result`, then atomically bind candidate artifact ID, checksum, usage, latency, and one aware UTC `report_generated_at`. On failure exhaustion: atomically bind stable failure code and the same report time without a candidate.

After tenant publication, CAS-advance `EngineExecution.normalized_report_version_id` from the prior generation's report ID (or null for generation 1) to the new report ID. Mark `SynthesisExecution.succeeded` when synthesis completed, or `SynthesisExecution.failed` when the published report carries `synthesis.state="failed"`. Missing/invalid core and report-integrity failures also end as failed but have no new report ID. In every case, a terminal queue item is completed rather than retried indefinitely.

- [ ] **Step 8: Add terminal and remediation projections**

Project both control and tenant parent with version-checked writes:

- complete core + completed synthesis + no other partial cause -> `completed`;
- insufficient core or failed synthesis with a valid report -> `partially_completed`;
- no credible core or report integrity failure -> `failed`;
- cancellation -> `canceled`.

Add a dedicated remediation function that permits only `partially_completed -> completed` when the old report's sole partial cause is failed synthesis and the new report is complete. It must not enter the general `transition()` table. A completed parent remains completed throughout manual reruns.

- [ ] **Step 9: Compose the production runtime**

Add `perfpilot-synthesis-worker = "perfpilot_api.workers.synthesis_runtime:main"`. Require production, AI enabled, valid engine lock with image digests, `PERFPILOT_SYNTHESIS_WORKER_ID`, `PERFPILOT_REPORT_WORKER_IMAGE_DIGEST`, `PERFPILOT_AI_CREDENTIAL_FILE`, and an egress allowlist containing the provider host. Reuse owner-only mounted-file secret loading, separate bounded artifact/provider clients, no redirects, `trust_env=False`, certificate verification, and reverse-order cleanup.

- [ ] **Step 10: Run GREEN, integration, lint, commit, and push**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_analysis_report_writer.py \
  services/api/tests/unit/test_synthesis_worker.py \
  services/api/tests/unit/test_synthesis_worker_runtime.py \
  services/api/tests/integration/test_analysis_report_repository.py \
  services/api/tests/integration/test_synthesis_orchestrator.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/reports/writer.py \
  services/api/src/perfpilot_api/services/synthesis_executions.py \
  services/api/src/perfpilot_api/workers \
  services/api/src/perfpilot_api/domain/transitions.py \
  services/api/tests
git diff --check
git add services/api/src/perfpilot_api services/api/pyproject.toml services/api/tests
git commit -m "feat: publish recoverable AI analysis reports"
git push
```

Expected: success, AI failure, retry, cancellation, lease expiry, duplicate delivery, concurrent finalization, and every injected recovery point converge on one immutable report.

### Task 9: Expose real stages, latest reports, and AI-only reruns through the API

**Files:**
- Modify: `contracts/v1/analyses/analysis-response.schema.json`
- Modify: `services/api/src/perfpilot_api/services/analyses.py`
- Modify: `services/api/src/perfpilot_api/api/analyses.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Modify: `services/api/tests/unit/test_analysis_contracts.py`
- Modify: `services/api/tests/unit/test_analysis_service.py`
- Modify: `services/api/tests/integration/test_analysis_api.py`
- Modify: `services/api/tests/integration/test_analysis_repository.py`

- [ ] **Step 1: Write RED stage response tests**

Require exactly four ordered stages for Trace analyses:

```json
[
  {"stage": "input_validation", "state": "completed", "failure": null},
  {"stage": "smartperfetto", "state": "completed", "failure": null},
  {"stage": "perfpilot_ai", "state": "running", "failure": null},
  {"stage": "report", "state": "pending", "failure": null}
]
```

Stage states are only `pending`, `running`, `completed`, `failed`, `canceled`, `not_requested`. Test created/uploading/analyzing/completed/partially-completed/failed/canceled, AI disabled, AI failed, manual rerun running with an old report, and no synthesis record. Never expose provider/model/endpoint or control IDs in analysis status.

- [ ] **Step 2: Write RED report selection and rerun API tests**

For `trace_upload`, `GET /v1/teams/{team_id}/analyses/{analysis_id}/report` must load the newest valid analysis-level `ReportVersion`, validate checksum and `AnalysisReport 1.1`, and return it even while a newer rerun is running. Device mode must keep current scenario assembly unchanged.

For `POST /v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs`, require one `Idempotency-Key`, team write access, same-origin/CSRF, a terminal Trace parent, latest valid core report, and an authoritative SmartPerfetto source. Return `201` with generation/state on first reservation and the same result on exact replay. Reject changed request hash with 409, cross-team IDs with 404, non-Trace parents with 422, and missing core reports with 409.

- [ ] **Step 3: Verify RED**

```bash
uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/unit/test_analysis_service.py \
  services/api/tests/integration/test_analysis_api.py -q
```

Expected: response schema has no stages, Trace report reads use device scenario assembly, and the rerun route is missing.

- [ ] **Step 4: Implement stage projection from authoritative rows**

Extend `AnalysisView` with `stages: tuple[AnalysisStageView, ...]`. Load the latest SmartPerfetto execution and latest synthesis generation in the control query; derive stages without reading private artifact content. `report_available` for Trace is true when a validated analysis-level report exists, independent of whether a later rerun is active.

Do not infer completed AI from the parent alone. A stage is completed only from a bound successful synthesis/report record; a failed synthesis section reports `failed` with its stable code.

- [ ] **Step 5: Split report loading by analysis mode**

Preserve `_assemble_report()` for `device`. Add `_load_trace_report()` that selects analysis-level rows with non-null `report`, newest `report_version` first, checks mutually exclusive metadata, recomputes checksum with canonical JSON, validates the report contract, verifies analysis ID/mode/version, and returns a defensive copy. Metadata-only rows are skipped; malformed content rows raise `AnalysisUnavailableError` rather than silently falling back.

- [ ] **Step 6: Implement the manual synthesis-run service and endpoint**

Add a no-body request route with `Idempotency-Key`. The service allocates the next generation through `SynthesisExecutionService`, publishes `analysis_synthesis_requested`, and leaves the current report and parent untouched. Return only:

```json
{
  "schema_version": "1.0",
  "analysis_id": "82000000-0000-4000-8000-000000000001",
  "generation": 2,
  "state": "queued"
}
```

- [ ] **Step 7: Wire production dependencies without changing test injection**

In `main.py`, build the synthesis-run service only when control sessions and artifact runtime are available. Continue allowing explicit fake services in tests. The HTTP app does not run the worker and never resolves the provider credential.

- [ ] **Step 8: Run GREEN, contract validation, lint, commit, and push**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/unit/test_analysis_contracts.py \
  services/api/tests/unit/test_analysis_service.py \
  services/api/tests/integration/test_analysis_api.py \
  services/api/tests/integration/test_analysis_repository.py -q
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src/perfpilot_api/api/analyses.py \
  services/api/src/perfpilot_api/services/analyses.py \
  services/api/src/perfpilot_api/main.py \
  services/api/tests
git diff --check
git add contracts/v1/analyses \
  services/api/src/perfpilot_api \
  services/api/tests
git commit -m "feat: expose AI reports and synthesis reruns"
git push
```

Expected: Trace status and report reads are tenant-scoped and real, old device behavior remains green, and users can rerun AI without rerunning SmartPerfetto.

### Task 10: Render the four stages and concise report in the existing web UI

**Files:**
- Modify: `app/lib/perfpilot-api.ts`
- Modify: `app/components/analysis-progress.tsx`
- Create: `app/components/analysis-report.tsx`
- Modify: `app/globals.css`
- Modify: `tests/perfpilot-api.test.ts`
- Modify: `tests/analysis-progress.test.tsx`
- Create: `tests/analysis-report.test.tsx`
- Modify: `tests/rendered-html.test.mjs`

- [ ] **Step 1: Write RED browser API tests**

Extend runtime parsing for the exact stage list and `AnalysisReport 1.1`. Add `client.report(teamId, analysisId)` and `client.createSynthesisRun(teamId, analysisId, idempotencyKey)`. Assert same-origin paths, CSRF, credentials, redirect rejection, response-size bounds, and idempotency headers. Reject unknown report fields and never retain a provider endpoint or signed URL.

- [ ] **Step 2: Write RED report rendering tests**

Render one completed report and assert visible executive summary, at most five top findings, P0/P1/P2 recommendations in priority order, retest plan, limitations, and collapsed provenance. Verify finding/evidence anchors use stable IDs.

Render a failed synthesis report and assert the core scenario/finding evidence remains visible, no recommendation section is fabricated, a partial-completion message appears, and `重新生成 AI 建议` invokes the supplied callback once. On report load failure, assert no text from fixtures or `/problems` appears.

- [ ] **Step 3: Verify RED**

```bash
npm run test:unit -- \
  tests/perfpilot-api.test.ts \
  tests/analysis-progress.test.tsx \
  tests/analysis-report.test.tsx
```

Expected: report types/component and four-stage UI do not exist.

- [ ] **Step 4: Add closed browser types and loader behavior**

Define:

```typescript
export type AnalysisStageState =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "canceled"
  | "not_requested";

export interface AnalysisStage {
  readonly stage: "input_validation" | "smartperfetto" | "perfpilot_ai" | "report";
  readonly state: AnalysisStageState;
  readonly failure: { readonly code: string; readonly message: string; readonly retryable: boolean } | null;
}
```

Validate stage count/order and the report's required top-level shape before casting nested data. The detail loader polls status until no stage is pending/running. Whenever `report_available` is true, load and retain the latest valid report, including while a manual rerun is active. Abort status, report, and rerun requests on route change.

- [ ] **Step 5: Replace the three inferred stages with four server stages**

Render the server's exact stage states and concise labels: 文件校验, SmartPerfetto, PerfPilot AI, 报告完成. Parent status remains the headline; stage failures show their stable public message. Do not infer `engineDone` from terminal parent state.

- [ ] **Step 6: Add the concise report component without redesigning the page**

Keep current cards, spacing, colors, and responsive hierarchy. Order sections:

1. 执行摘要;
2. 重点问题;
3. 优化建议;
4. 复测计划;
5. 限制与缺失证据;
6. 可折叠生成信息.

Show metric numbers only from scenario report facts. AI narrative renders as plain text, never HTML. Recommendation/retest buttons are not links to external content. The retry action creates a fresh UUID idempotency key, disables while submitting, then resumes polling.

- [ ] **Step 7: Run unit, accessibility/SSR, lint, build, commit, and push**

```bash
npm run test:unit -- \
  tests/perfpilot-api.test.ts \
  tests/analysis-progress.test.tsx \
  tests/analysis-report.test.tsx
npm run lint
npm run test:ssr
git diff --check
git add app tests
git commit -m "feat: show PerfPilot AI analysis reports"
git push
```

Expected: the existing detail page displays only API-backed progress and reports on desktop and mobile, and a failed API never falls back to demo findings.

### Task 11: Prove the full fake-provider path and close the release gate

**Files:**
- Create: `services/api/tests/fixtures/openai_compatible/synthesis-success.json`
- Create: `services/api/tests/fixtures/openai_compatible/synthesis-invalid-reference.json`
- Create: `services/api/tests/fixtures/openai_compatible/synthesis-refusal.json`
- Create: `services/api/tests/integration/test_trace_ai_report_pipeline.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/operations/ai-synthesis.md`

- [ ] **Step 1: Write the full RED integration test with local fakes**

Use the checked-in SmartPerfetto fixture and a local fake OpenAI-compatible HTTP service. Drive:

```text
trace_analysis_ready
-> SmartPerfetto canonical engine_result
-> engine_result_ready
-> synthesis generation 1
-> private projection
-> fake chat/completions
-> validated candidate
-> AnalysisReport 1.1
-> GET analysis/report
```

Assert one provider call, one projection artifact, one synthesis artifact, one report row, exact VersionId reads, completed stages, completed parent, and no private marker in provider request, logs, control rows, API JSON, or browser-renderable report.

Add cases for SmartPerfetto insufficient data, first-call 429 then success, two invalid-reference outputs, permanent authentication failure, duplicate source event, two concurrent synthesis workers, and manual generation 2 while generation 1 remains readable.

- [ ] **Step 2: Run the new integration test and verify RED**

```bash
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests/integration/test_trace_ai_report_pipeline.py -q
```

Expected: any remaining composition or state-machine gap is exposed before CI/documentation changes.

- [ ] **Step 3: Fix only integration gaps and keep all boundaries intact**

Wire fakes through dependency injection; do not weaken URL, credential, schema, reference, privacy, or checksum validation to satisfy the test. CI must never call a public provider or load a real token.

- [ ] **Step 4: Extend CI and operational documentation**

Keep the existing backend test command so the new tests run automatically. Add assertions in `test_ci_workflow.py` if CI filters need updating. Document every `PERFPILOT_AI_*`, worker ID/image digest, credential mount, and egress-host variable; startup refusal behavior; key rotation; metrics based only on non-sensitive audit rows; stable error codes; manual rerun; and recovery semantics.

Document the deployment gate explicitly:

- SmartPerfetto and Report Worker image digests are non-null and pinned.
- Provider hostname is on the outbound allowlist and TLS verification is enabled.
- Secret reference resolves from an owner-only mount; the value is absent from environment and databases.
- Migrations were exercised upgrade -> downgrade preflight -> upgrade on a staging snapshot.
- A private real startup Trace produced a valid report and privacy inspection found no raw/private fields.
- The exact tested commit SHA is the one deployed.

- [ ] **Step 5: Run the complete backend and web verification gate**

```bash
uv run --offline --locked --package perfpilot-api ruff check \
  services/api/src services/api/tests services/api/migrations
env PERFPILOT_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \
  PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
  uv run --offline --locked --package perfpilot-api pytest -p no:cacheprovider \
  services/api/tests -q
npm run lint
npm test
git diff --check
```

Expected: Ruff, every backend contract/unit/integration test, web unit tests, SSR build/render tests, and diff whitespace checks pass. Any skipped PostgreSQL test is a failure because `PERFPILOT_REQUIRE_POSTGRES_TESTS=1` is set.

- [ ] **Step 6: Audit the delivered diff for prohibited data and unfinished markers**

```bash
rg -n 'TO''DO|TB''D|place''holder|demo fallback|X-Amz-Signature|credential_reference|object_key|VersionId' \
  services/api/src/perfpilot_api/ai \
  services/api/src/perfpilot_api/reports \
  services/api/src/perfpilot_api/workers/synthesis_runtime.py \
  app/components app/lib/perfpilot-api.ts
git diff --stat main...HEAD
git log --oneline main..HEAD
```

Expected: matches are limited to deliberate rejection tests, private persistence internals, and configuration field names; no public model, prompt payload, log, or browser component exposes them. Every implementation task has its own pushed commit.

- [ ] **Step 7: Commit, push, and create the pull request**

```bash
git add .github/workflows/ci.yml .env.example README.md docs/operations \
  services/api/tests/fixtures/openai_compatible \
  services/api/tests/integration/test_trace_ai_report_pipeline.py
git commit -m "test: verify the Trace AI report pipeline"
git push
gh pr create \
  --base main \
  --head feature/perfpilot-ai-synthesis \
  --title "feat: synthesize SmartPerfetto analysis reports" \
  --body-file docs/superpowers/specs/2026-08-03-perfpilot-ai-synthesis-design.md
```

Expected: the PR contains the approved specification, this plan, all implementation commits, test evidence, and no deployment action.

- [ ] **Step 8: Keep production deployment blocked until private smoke evidence exists**

Do not deploy from this implementation task. After review and merge, configure real image digests, provider secret reference, egress allowlist, and production migrations in the deployment environment. Then run the documented private smoke with a non-customer Trace and record only report ID/checksum, stage outcome, latency, and pass/fail evidence—never the prompt, projection, candidate, or provider error body.

## Completion checklist

- [ ] New SmartPerfetto Trace completion automatically reaches a validated `AnalysisReport 1.1` when AI is enabled.
- [ ] SmartPerfetto remains the sole authority for every fact and referenceable entity.
- [ ] The provider sees only the bounded private projection and untrusted authoritative question.
- [ ] AI failure still yields a readable core report and `partially_completed` parent.
- [ ] Manual synthesis rerun creates a new immutable generation without rerunning SmartPerfetto.
- [ ] Replays, crashes, lease expiry, and concurrent finalizers converge without report overwrite.
- [ ] API and web remain tenant-scoped and never use static fallback.
- [ ] Device report behavior and old `AnalysisReport 1.0` fixtures remain green.
- [ ] Production runtime refuses unsafe/missing AI configuration.
- [ ] Every focused task, full backend suite, full web suite, and diff audit pass before PR creation.
