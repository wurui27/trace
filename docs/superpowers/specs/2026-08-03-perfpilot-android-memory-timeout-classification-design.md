# Android Memory Timeout Classification Design

## Context

The Android Memory worker currently persists a runtime timeout as the generic
worker state `failed`. The adapter maps every `failed` state to the terminal,
non-retryable code `engine_failed`. As a result, the execution service never
receives `engine_timeout` and cannot reserve the bounded retry required by the
approved Android Memory foundation design.

## Decision

Add `timed_out` to the worker's internal terminal-state contract. When the
worker's own deadline expires, it writes this exact state to `state.json` and
does not attach an exit code. The timeout event, not a process exit code,
therefore remains the source of truth.

The Android Memory adapter translates the internal state as follows:

| Worker state | Public execution state | Stable code | Retryable |
| --- | --- | --- | --- |
| `timed_out` | `failed` | `engine_timeout` | `true` |
| `failed` | `failed` | `engine_failed` | `false` |

`fetch_result()` raises a retryable `engine_timeout` for `timed_out`.
`cancel()` treats `timed_out` as an already-finished failed run. No public API,
database state, or migration changes.

## Persistence and Recovery

The strict worker-state parser accepts `timed_out` only with the common fields
`schema_version` and `state`. It rejects exit codes and all other fields. A new
worker instance pointed at the same run root must recover `timed_out`, so the
classification survives a service restart.

Older persisted `failed` states remain valid and continue to mean a normal,
non-retryable engine failure. An older adapter presented with the new state
fails closed as `worker_unavailable`, which still enters the same bounded
new-attempt path.

## Retry Boundary

The execution service already reserves a new attempt for `engine_timeout`.
`SQLAlchemyEngineExecutionRepository.reserve_retry()` enforces both the global
job deadline and `GlobalJob.max_retries`; this change does not add a second
retry counter.

## Verification

Tests must prove all of the following:

- Local and OCI worker runtime deadlines persist `timed_out` and clean up.
- A fresh worker instance recovers `timed_out` from the same run root.
- The strict parser rejects a `timed_out` state with an exit code or extra data.
- Adapter status and result paths map `timed_out` to retryable
  `engine_timeout`; cancellation reports a finished failed run.
- A real worker timeout flows through the real adapter into
  `EngineExecutionService`, which reserves attempt 2.
- The SQL repository stops creating attempts after `max_retries` is exhausted.

## Rejected Alternatives

Keeping `failed` and adding a separate failure-reason result field would force
the adapter to perform another read and would permit contradictory state and
reason combinations. Inferring timeouts from `-9`, `137`, or another exit code
is unsafe because signals, OCI termination, and timing races can produce the
same values.
