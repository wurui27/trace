# PerfPilot Canonical Engine Results Design

## Status

Approved direction for the delivery after the CI gate. This design establishes
the durable result boundary shared by SmartPerfetto and Android Memory. It does
not generate a public performance report or call an AI provider.

## Goal

Persist every successful external-engine result as deterministic, immutable,
tenant-routed JSON. A crash, duplicate delivery, concurrent finalizer, or tenant
resource rollover must never overwrite bytes, attach a result to another
execution, or expose storage coordinates. Later Normalizer and AI workers will
read this artifact by platform identity and exact object version.

## Decision

PerfPilot will use two report layers:

1. `canonical-engine-result` preserves the adapter-validated engine payload and
   authoritative execution provenance.
2. A later Normalizer converts that artifact into public `AnalysisReport` data
   and a bounded AI projection.

The sink will not write the current `ReportVersion` model. That model requires a
`ScenarioResult` and encodes device/trace-specific fields that do not apply to a
manual memory analysis. Forcing both engines into it now would lose source data
and create fake scenario identities.

Opaque JSON alone is also rejected. Without a platform envelope, consumers could
not prove execution identity, source contract, pinned engine version, or payload
integrity.

## Canonical contract

The new JSON Schema is
`contracts/v1/engines/canonical-engine-result.schema.json`. It accepts this closed
shape:

```json
{
  "schema_version": "1.0",
  "result_type": "canonical-engine-result",
  "artifact_id": "00000000-0000-0000-0000-000000000000",
  "analysis_id": "00000000-0000-0000-0000-000000000000",
  "execution_id": "00000000-0000-0000-0000-000000000000",
  "tenant_resource_version": 1,
  "engine": {
    "engine_id": "android_memory",
    "adapter_version": "1.0.0",
    "source_contract": "android-memory-ai-context-1.2",
    "source_commit_sha": "d5514972ced78c3faa7fc17589c1ea9231645056",
    "image_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "attempt": {
    "number": 1,
    "input_manifest_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "config_hash": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "result": {
    "state": "completed",
    "payload_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "payload": {}
  }
}
```

The schema applies these rules:

- every object uses `additionalProperties: false`, except the engine payload;
- `engine_id=smartperfetto` requires `workspace-agent-v1`;
- `engine_id=android_memory` requires `android-memory-ai-context-1.2`;
- only `completed` and `insufficient_data` produce result artifacts;
- commit SHA, image digest, hashes, UUIDs, versions, and attempt numbers use
  strict formats and positive bounds;
- `team_id`, bucket, object key, VersionId, signed URL, external error text, and
  sink timestamps never enter the document.

The payload is exactly the adapter-validated stable result:

- SmartPerfetto stores `{"reportId": ..., "report": <sanitized report>}`;
- Android Memory stores `AndroidMemoryContext.model_dump(mode="json")`.

Canonical JSON uses UTF-8, sorted keys, compact separators, and rejects NaN and
Infinity. `payload_sha256` hashes the canonical payload bytes. The artifact
request hash covers the entire canonical envelope. The envelope contains no
wall-clock sink time, so one execution always produces the same bytes.

## Authoritative write request

`EngineExecutionService` constructs an immutable `EngineResultWrite` from the
claimed `EngineExecutionRecord`; the adapter cannot supply provenance:

```text
team_id
analysis_id
execution_id
expected_execution_version
tenant_resource_version
artifact_id
engine_id
adapter_version
engine_commit_sha
engine_image_digest
attempt_number
input_manifest_hash
config_hash
EngineResult
```

`tenant_resource_version` becomes a required `EngineExecution` field. Memory
execution preparation passes the version it used to authorize every input.
Network-engine preparation must do the same before production composition enables
SmartPerfetto execution.

## Deterministic identity

- `artifact_id` remains `UUIDv5(result namespace, execution_id)` through the
  existing `result_artifact_id()` function.
- `Artifact.artifact_kind` is `engine_result`.
- `Artifact.upload_id` equals `artifact_id`.
- `idempotency_key` is `internal:engine_result:<execution_id>`.
- `request_hash` is the lower-case SHA-256 hex digest of the envelope bytes.
- the server derives
  `raw/analyses/<analysis_id>/internal/engine-results/<artifact_id>.json`.

External report IDs and run IDs never become tenant database keys.

## Sink transaction and object flow

`S3EngineResultSink.write(request)` performs these steps:

1. Validate identities, engine-contract pairing, terminal state, payload shape,
   payload size, canonical hashes, and privacy invariants.
2. Resolve the tenant bucket and require the request's resource version.
3. Enter the routed tenant session and require the same resource version.
4. Prove the Analysis belongs to the routed tenant and matches the engine mode.
5. Reserve the deterministic Artifact row with `INSERT ... ON CONFLICT DO NOTHING`.
6. Re-read an existing row and compare every identity and request hash.
7. Recheck the tenant resource version before object I/O.
8. PUT canonical bytes with JSON MIME and checksum metadata.
9. Require a non-empty immutable S3 VersionId; HEAD that exact version and verify
   MIME, length, checksum, and absence of a delete marker.
10. Recheck the tenant resource version and finalize the Artifact row by versioned
    compare-and-swap.
11. If another writer won, reload the row, read its exact S3 VersionId, and compare
    the bytes in constant time.
12. Recheck the tenant resource version before returning the artifact UUID.

A finalized identical object is success. A pending identical reservation may be
repaired. The sink never overwrites a finalized artifact and never treats a new
S3 version as an idempotent retry.

## Crash and concurrency semantics

`EngineExecutionService` keeps its existing order:

```text
claim deterministic artifact marker
→ fetch adapter result
→ write immutable result artifact
→ CAS execution to terminal state
```

If the process exits after the sink succeeds, a later finalizer writes the same
bytes, receives the same UUID, and retries the terminal CAS. Concurrent
finalizers converge on one Artifact and one exact VersionId. Same identity with
different bytes is an integrity failure.

## Errors

The sink exposes only typed, redacted errors:

- `EngineResultValidationError`: invalid contract, identity, state, size, hash,
  JSON, or privacy; terminal `invalid_output`.
- `EngineResultConflictError`: deterministic identity points to different bytes,
  metadata, owner, or object version; terminal `result_integrity_mismatch`.
- `EngineResultUnavailableError`: temporary database, route, PUT, HEAD, or read
  failure; retryable `result_persistence_failed` until the execution deadline.

Cancellation, `KeyboardInterrupt`, and `SystemExit` propagate. Other exceptions
are translated without their original text. Logs may contain team, analysis,
execution, and artifact UUIDs; they must omit URLs, object coordinates, VersionId,
payload text, upstream responses, and credentials.

## Payload limits and privacy

The envelope has a 2 MiB canonical byte limit. Traversal limits depth, collection
size, key length, string length, and total nodes before serialization. It rejects
bytes, non-string mapping keys, non-finite numbers, credentials, signed URLs,
object-store URIs, absolute POSIX paths, Windows drive paths, and path traversal.

The Android payload must retain both privacy flags as actual `false` values. The
SmartPerfetto payload must pass its existing sanitized-report validator. The sink
revalidates both contracts; it never assumes an adapter object remained unchanged.

## Later Normalizer and AI boundary

After terminal execution CAS, a later durable worker will publish an
`engine_result_ready` event. A Normalizer will read the exact Artifact version and
produce public report rows. It will preserve observed facts, deterministic
calculations, hypotheses, and recommendations as separate claim types.

The AI worker will receive a smaller, versioned projection, not this complete
artifact. It will exclude SmartPerfetto conversation history, query history,
analysis notes, external session IDs, and unverified hypotheses. It will exclude
Android folder inventory, paths, derived-report bodies, raw sources, and the
engine-echoed user question. The platform will inject the authoritative user
question separately.

## Files

Create:

- `contracts/v1/engines/canonical-engine-result.schema.json`;
- two valid examples, one per engine;
- `services/api/src/perfpilot_api/engines/canonical_results.py`;
- `services/api/src/perfpilot_api/services/engine_result_artifacts.py`;
- unit, contract, PostgreSQL, and S3-version tests for these boundaries.

Modify:

- control migration and `EngineExecution` model for tenant resource version;
- `memory_executions.py` to preserve the input authorization route version;
- `engine_executions.py` for `EngineResultWrite` and typed sink error mapping;
- `runtime/artifacts.py` to compose the production sink;
- `main.py` only when a real sink and engine lock are both available.

The Artifact table and current ReportVersion table require no change in this
delivery.

## Testing

Tests must prove:

- both engine examples validate and every cross-pairing fails;
- canonical bytes and hashes are deterministic;
- unknown fields, invalid states, unsafe JSON, privacy markers, and oversized
  payloads fail before storage;
- another tenant or Analysis cannot reserve or replay an artifact;
- tenant resource rollover fails at every database/object boundary;
- PUT/HEAD/checksum/MIME/VersionId/delete-marker rules use the exact object version;
- duplicate and concurrent identical writes converge;
- conflicting bytes never overwrite or create an accepted new version;
- a sink-success/terminal-CAS crash recovers with the same artifact ID;
- typed errors map to the required stable execution states without data leakage.

The PostgreSQL test suite will use two routed tenant databases. A later live S3
test will use a versioned MinIO bucket; unit tests use a version-aware fake and
botocore Stubber.

## Acceptance criteria

1. SmartPerfetto and Android Memory terminal results produce byte-stable canonical
   artifacts with authoritative provenance.
2. Repeated and concurrent finalization cannot duplicate or mutate a result.
3. Tenant routing and immutable object versions remain fenced during rollover.
4. Validation and conflict errors become terminal stable codes; transient storage
   errors remain bounded retries.
5. No public report, AI text, storage coordinate, or credential is introduced.
6. Full API, PostgreSQL, Redis, Web, Ruff, contract, and CI checks pass.

## Deferred work

This delivery does not implement Redis Streams workers, public report
normalization, `memory_upload` report reads, AI providers, Web rendering, release
images, or deployment. Each remains a separate, testable package built on the
canonical artifact.
