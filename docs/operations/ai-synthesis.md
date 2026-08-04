# AI synthesis operations

This guide covers the API and `perfpilot-synthesis-worker` production boundary. This implementation task does not deploy, configure a real provider, or load a real token.

## Runtime flow

When AI is enabled, the latest completed or `insufficient_data` SmartPerfetto execution emits `engine_result_ready`. The coordinator allocates automatic generation 1, then the worker builds a bounded projection, calls an OpenAI-compatible `/chat/completions` endpoint, validates the candidate, and publishes an immutable AnalysisReport 1.1.

The provider never receives a raw Trace, object key, signed URL, credential reference, or storage `VersionId`. It receives only the allowlisted projection and the packaged system prompt. A provider failure normally leaves the core report readable with `partially_completed` state.

## Environment variables

`Settings` uses the `PERFPILOT_` prefix. These AI settings apply to both API composition and the synthesis worker.

| Variable | Default and constraints | Purpose |
| --- | --- | --- |
| `PERFPILOT_AI_ENABLED` | `false` | Enables automatic synthesis and the manual rerun service. Keep it `false` in CI unless a test injects a local fake. |
| `PERFPILOT_AI_BASE_URL` | `http://127.0.0.1:4010/v1/` | OpenAI-compatible API root. Production requires HTTPS, a global host, a path ending in `/v1/`, and no user info, query, or fragment. |
| `PERFPILOT_AI_PROVIDER_NAME` | `development-fake`; 1-64 letters, digits, `.`, `_`, or `-` | Non-secret provider label stored with audit and report provenance. |
| `PERFPILOT_AI_MODEL` | `fake-json-model`; 1-128 characters | Non-secret model label stored with audit and report provenance. |
| `PERFPILOT_AI_CREDENTIAL_REFERENCE` | `development-only-ai-credential-reference` | Maps to `Settings.ai_credential_reference`. Production rejects an empty value and this development default. See the credential relationship below. |
| `PERFPILOT_AI_CONNECT_TIMEOUT_SECONDS` | `5`; greater than 0 and at most 120 | Provider connection timeout. |
| `PERFPILOT_AI_READ_TIMEOUT_SECONDS` | `60`; greater than 0 and at most 120 | Provider response-read timeout. |
| `PERFPILOT_AI_WRITE_TIMEOUT_SECONDS` | `30`; greater than 0 and at most 120 | Provider request-write timeout. |
| `PERFPILOT_AI_POOL_TIMEOUT_SECONDS` | `5`; greater than 0 and at most 120 | Provider connection-pool timeout. |
| `PERFPILOT_AI_MAX_PROJECTION_BYTES` | `262144`; 1024-262144 | Maximum private projection passed to the provider. |
| `PERFPILOT_AI_MAX_RESPONSE_BYTES` | `131072`; 1024-131072 | Maximum provider response body; oversized responses become `ai_protocol_invalid`. |

The API and worker also consume these synthesis runtime variables.

| Variable | Requirement | Purpose |
| --- | --- | --- |
| `PERFPILOT_APP_ENV` | Exactly `production` for the worker | Prevents a development or test process from starting the production worker. |
| `PERFPILOT_SYNTHESIS_WORKER_ID` | Required; 1-128 letters, digits, `.`, `_`, `:`, or `-` | Stable claim-consumer identity. Give each live worker a distinct value. |
| `PERFPILOT_REPORT_WORKER_IMAGE_DIGEST` | Required by both API and worker when AI is enabled; `sha256:` plus 64 lowercase hex digits | Pins report provenance to the deployed worker image. A tag such as `latest` is rejected. |
| `PERFPILOT_AI_CREDENTIAL_FILE` | Required absolute, unambiguous file path | Points to the mounted provider token. It is not a `Settings` field and must never contain the token itself. |
| `PERFPILOT_AI_EGRESS_ALLOWLIST` | Required comma-separated hostnames, with no whitespace or empty entries | Must contain the normalized hostname from `PERFPILOT_AI_BASE_URL`. This is a startup check; enforce the same restriction with network policy. |
| `PERFPILOT_ENGINE_LOCK_FILE` | Required absolute, unambiguous file path | Engine lock. Worker startup requires every lock entry to have a valid image digest. |
| `PERFPILOT_ENGINE_LOCK_SCHEMA_FILE` | Required absolute, unambiguous file path | JSON Schema used to validate the engine lock. |

The worker builds the shared artifact runtime, so its production `Settings` must also provide valid control PostgreSQL, Redis, S3, tenant-cluster, secret-keyring, secret-store, origin, and platform-secret configuration. In particular, production requires PostgreSQL `sslmode=verify-full`, `rediss`, an HTTPS S3 endpoint, tenant `verify-full`, and absolute keyring and secret-store paths. The platform-wide settings validation runs before worker composition even when a shared value is not used directly by synthesis.

### Credential reference and mounted token

`PERFPILOT_AI_CREDENTIAL_REFERENCE` and `PERFPILOT_AI_CREDENTIAL_FILE` serve different purposes:

- `Settings.ai_credential_reference` is an opaque deployment reference. It proves that production did not retain the development default. The application does not dereference it or store it in synthesis audit rows.
- `PERFPILOT_AI_CREDENTIAL_FILE` is the worker's path to the token bytes. The worker reads it once during startup and keeps the token only in memory.
- The deployment system must bind both values to the same managed secret version. The code does not cross-check that relationship.

Mount the credential as a regular, read-only file owned by the worker's effective UID. Its exact mode must be `0400` or `0600`. The reader refuses a symlink where `O_NOFOLLOW` is available, a foreign owner, another mode, an unreadable file, or an invalid token. After removing one optional trailing newline, a token must contain 16-4096 printable ASCII characters without spaces.

Rotate credentials with a new secret version and an atomic mount update. Update the reference and mount together, then roll the workers because they read the file only at startup. Verify successful private requests before revoking the old version. Roll back both values if authentication failures rise. Never write the token to an environment variable, command line, log, database, or report.

## Startup refusal and network controls

The process refuses startup before it claims work or calls the provider when any required boundary fails:

- Production `Settings` rejects an unsafe AI URL, an empty or development credential reference, insecure shared service URLs, development secrets, or missing artifact-runtime settings. Production also rejects SmartPerfetto enabled without AI.
- The API rejects AI-enabled startup unless `PERFPILOT_REPORT_WORKER_IMAGE_DIGEST` has the exact SHA-256 form.
- The worker rejects non-production mode, disabled AI, an invalid worker ID or report-worker digest, unsafe paths, a provider host absent from the egress allowlist, an invalid engine lock, or an unreadable credential.
- Runtime composition fails closed if the prompt, databases, tenant routes, secret store, S3 client, or provider client cannot initialize. Cleanup runs in reverse order.

Production provider traffic uses certificate verification, refuses redirects, ignores proxy environment variables, and applies separate bounded connect, read, write, and pool timeouts. Allow only the provider hostname at both startup and the workload's DNS/firewall egress layer. Deny direct Internet egress to every other host. The allowlist checks hostnames, not resolved IP ranges, so network policy and DNS controls remain mandatory.

CI sets AI disabled, a loopback fake URL, the development-only reference, and empty worker credential, digest, and egress values. Tests that need provider behavior inject an in-process or loopback fake. CI must never expose a public provider endpoint or a real token.

## Stable failures and retry policy

Logs, audit rows, reports, and API responses use stable codes. Do not record provider error bodies or rejected candidate bytes.

| Stable code | Automatic behavior | Result after exhaustion |
| --- | --- | --- |
| `ai_timeout` | Retry once in the same generation. | Publish the core report as `partially_completed`. |
| `ai_rate_limited` | Retry once in the same generation. | Publish the core report as `partially_completed`. |
| `ai_provider_unavailable` | Retry once in the same generation. | Publish the core report as `partially_completed`. |
| `ai_output_invalid` | Retry once. The second request includes only this code, not the rejected output. | Publish the core report as `partially_completed`. |
| `ai_authentication_failed` | Do not retry automatically. | Publish the core report as `partially_completed`; check the mount and rotation. |
| `ai_protocol_invalid` | Do not retry automatically. | Publish the core report as `partially_completed`; check endpoint compatibility and bounds. |
| `ai_projection_invalid` | Do not call the provider or retry automatically. | Publish the core report as `partially_completed`; check the projection limit, authoritative question, and privacy-safe core shape. |
| `core_report_invalid` | Do not call or retry the provider. | Fail the synthesis without publishing a new report. |
| `report_integrity_failed` | Stop publication and alert. | Publish no new report; any prior immutable report stays unchanged. |
| `synthesis_failed` | Defensive fallback for a terminal synthesis missing its report. | Fail the parent without inventing a report. |

`insufficient_data` is a core-analysis limitation rather than a provider error. It yields a limited `partially_completed` report. The rerun API returns the platform envelope codes `service_unavailable`, `request_validation_failed`, `resource_not_found`, or `idempotency_conflict` as appropriate; clients should branch on codes, not localized messages.

## Generations, recovery, and idempotency

Automatic generation 1 starts only from the latest authoritative SmartPerfetto result. The `(analysis_id, source_execution_id, generation)` uniqueness rule and deterministic outbox event ID make duplicate source events converge on the same execution.

After a terminal Trace analysis has an authoritative core report and generation 1 report, an authorized team writer may request generation 2:

```http
POST /v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs
Idempotency-Key: <unique-operation-key>
```

The request has no body. It returns `201` with `state: queued`. Reusing the same key returns the same generation; changing its authority produces `idempotency_conflict`. Later manual reruns allocate generation 3 and above. The worker uses the same authoritative core artifact. It preserves every earlier immutable report and changes the current report only after the new version publishes. A successful generation 2 may remediate a `partially_completed` parent to `completed`; it never demotes an already completed parent.

The production queue uses a 30-second renewable lease and a 10-second heartbeat. An expired or lost claim becomes available to another worker. The pipeline persists each boundary: projection artifact, projection binding, invocation audit, candidate artifact, candidate binding, report timestamp, immutable report, source pointer, and terminal state. On restart it reloads those markers and resumes at the next boundary. Deterministic IDs, checksums, versioned artifact reads, mutation fences, and uniqueness constraints turn duplicate delivery into replay or a visible integrity conflict instead of overwrite.

A temporary tenant-route failure delays only that source event and does not prevent the worker from claiming already-created synthesis work. If generation 1 committed before its work event, the coordinator reloads that exact generation and recreates the deterministic event without applying newer prompt, model, image, or inference settings to the in-flight generation.

Provider calls remain at least once. A crash after the provider accepts a request but before the invocation result commits can cause another call, so do not promise exactly-once billing. Each generation permits at most two provider attempts. Unexpected worker errors return the event to the durable queue; temporary canonical or artifact reads reschedule without discarding committed progress.

## Non-sensitive audit metrics

Build operational metrics only from the protected `synthesis_executions` and `ai_invocations` metadata rows. Suitable aggregates include:

- pending and running age, terminal counts, completion duration, and generations per analysis;
- invocation success rate, attempts per execution, and retry rate;
- latency and prompt, completion, and total token histograms;
- counts by stable error code, provider label, model label, prompt version, and worker image digest;
- AI-partial proxy counts (`failed` synthesis rows that have a report version) and successful generation-2 remediation after a failed generation 1.

Keep labels bounded. Do not export team IDs, analysis IDs, execution IDs, artifact IDs, checksums, request fingerprints, questions, prompts, projections, candidates, provider bodies, endpoints, credential references, tokens, headers, object keys, signed URLs, or storage version IDs. Restrict the control tables themselves even though their synthesis columns contain non-secret metadata.

## Deployment gate

Block release until every item has recorded evidence:

- Build the external engines and report worker from the exact reviewed commit. Replace every `null` engine-lock image with a verified `sha256:` digest, confirm the SmartPerfetto digest, and set `PERFPILOT_REPORT_WORKER_IMAGE_DIGEST` to the deployed report-worker image digest. Tags alone fail the gate.
- Confirm the provider's exact hostname appears in `PERFPILOT_AI_EGRESS_ALLOWLIST`; verify its certificate chain and HTTPS `/v1/` endpoint. Prove workload network policy blocks all unapproved egress and that redirects remain disabled.
- Resolve a new `PERFPILOT_AI_CREDENTIAL_REFERENCE` to an owner-only `0400` or `0600` read-only mount at `PERFPILOT_AI_CREDENTIAL_FILE`. Prove the token is absent from environment variables, databases, logs, reports, and build artifacts.
- On a disposable staging snapshot, exercise control and tenant migration upgrade, the guarded downgrade preflight, and upgrade again. Preserve the expected refusal when synthesis audit or AI report rows would be lost; never bypass the guard on production data.
- Run a private smoke with a non-customer real startup Trace. Require a valid report, expected stage outcomes, and a privacy inspection of provider traffic, logs, control rows, API JSON, and browser output. Record only report ID/checksum, stage outcome, latency, and pass/fail evidence.
- Record the tested Git commit SHA. Build from that detached SHA, attach it to image provenance, deploy only the resulting digests, and verify the running revision matches the same SHA.

This task performs none of those deployment actions. The checked-in engine lock still contains `null` image digests, the example values contain no deployable credential, and all deployment gates remain closed until an authorized release run completes them.
