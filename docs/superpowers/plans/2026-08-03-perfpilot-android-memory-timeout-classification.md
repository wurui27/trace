# Android Memory Timeout Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Android Memory runtime timeouts across worker restarts and route them into the execution service's bounded new-attempt policy.

**Architecture:** The worker records an internal `timed_out` terminal state at the source of the timeout. The adapter converts it to the existing public `engine_timeout` error contract, while the current repository remains the single owner of deadline and retry-count enforcement.

**Tech Stack:** Python 3.12, asyncio, pytest, SQLAlchemy, PostgreSQL

---

### Task 1: Persist a distinct worker timeout state

**Files:**

- Modify: `services/api/src/perfpilot_api/engines/android_memory_worker.py`
- Test: `services/api/tests/unit/test_android_memory_worker.py`

- [ ] **Step 1: Write failing worker tests**

Change the Local and OCI timeout expectations to `timed_out`. Add a restart test
that constructs a second worker with the same run root and expects the same
state. Add strict-schema cases that reject an exit code or unknown key:

```python
assert await _terminal(worker, "memory-run-1") == "timed_out"

recovered = _local(tmp_path, FakeProcessFactory())
assert await recovered.status("memory-run-1") == "timed_out"

@pytest.mark.parametrize(
    "extra",
    [{"exit_code": -9}, {"reason": "timeout"}],
)
async def test_timed_out_state_rejects_extra_fields(
    tmp_path: Path,
    extra: dict[str, object],
) -> None:
    run_dir = tmp_path / "runs/memory-run-1"
    run_dir.mkdir(parents=True)
    state = {"schema_version": "1.0", "state": "timed_out", **extra}
    (run_dir / "state.json").write_text(json.dumps(state))

    with pytest.raises(AndroidMemoryWorkerError):
        await _local(tmp_path, FakeProcessFactory()).status("memory-run-1")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd services/api
uv run pytest \
  tests/unit/test_android_memory_worker.py::test_timeout_kills_process_marks_timed_out_and_cleans \
  tests/unit/test_android_memory_worker.py::test_oci_timeout_uses_runtime_kill_then_reaps_run_before_cleanup \
  tests/unit/test_android_memory_worker.py::test_timed_out_state_survives_worker_restart \
  tests/unit/test_android_memory_worker.py::test_timed_out_state_rejects_extra_fields -q
```

Expected: failures because `timed_out` is not yet a valid `WorkerState` and the
monitor still writes `failed`.

- [ ] **Step 3: Implement the minimal worker contract**

Extend the internal literal and split timeout handling from process failure:

```python
WorkerState = Literal[
    "running", "completed", "failed", "timed_out", "canceled", "lost"
]

if timed_out:
    persisted = _PersistedState("timed_out")
elif exit_code not in (0, 1, 2) or exit_code == 1:
    persisted = _PersistedState("failed", exit_code)
```

In `_read_state()`, accept `timed_out` only when the decoded key set equals
`{"schema_version", "state"}`.

- [ ] **Step 4: Run the complete worker unit file**

Run:

```bash
cd services/api
uv run pytest tests/unit/test_android_memory_worker.py -q
uv run ruff check \
  src/perfpilot_api/engines/android_memory_worker.py \
  tests/unit/test_android_memory_worker.py
```

Expected: all tests pass and Ruff reports no errors.

### Task 2: Map the timeout through Adapter and Orchestrator

**Files:**

- Modify: `services/api/src/perfpilot_api/engines/android_memory.py`
- Test: `services/api/tests/unit/test_android_memory_adapter.py`
- Test: `services/api/tests/unit/test_engine_execution_service.py`

- [ ] **Step 1: Write failing adapter contract tests**

Add `timed_out` to the status table and add explicit result and cancellation
tests:

```python
("timed_out", -1, "failed", "engine_timeout", True),

with pytest.raises(EngineAdapterError) as caught:
    await _adapter(worker=FakeWorker(state="timed_out")).fetch_result(_run_ref())
assert caught.value.stable_code == "engine_timeout"
assert caught.value.retryable is True

assert await _adapter(
    worker=FakeWorker(state="timed_out")
).cancel(_run_ref()) == "failed"
```

- [ ] **Step 2: Write the failing cross-layer timeout test**

In `test_engine_execution_service.py`, use a real
`LocalAndroidMemoryWorker` with a blocked fake process, wait for the worker to
reach `timed_out`, inject that worker into the real `AndroidMemoryAdapter`, and
register the adapter in `EngineExecutionService`. Add these focused test doubles
beside the existing execution-service fakes:

```python
class TimeoutReader:
    async def read(self, _size: int = -1) -> bytes:
        await asyncio.sleep(0)
        return b""


class TimeoutProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = TimeoutReader()
        self.stderr = TimeoutReader()
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self._done.set()


class TimeoutProcessFactory:
    async def __call__(self, *_args: object, **_kwargs: object) -> TimeoutProcess:
        return TimeoutProcess()


@dataclass
class TimeoutStaged:
    input_dir: Path
    cleanup_calls: int = 0

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


async def timeout_commit(_root: Path) -> str:
    return "c" * 40
```

Start from a running Android Memory execution record, call `step()`, and assert:

```python
@pytest.mark.asyncio
async def test_real_memory_worker_timeout_reserves_new_attempt(tmp_path: Path) -> None:
    repository_root = tmp_path / "checkout"
    repository_root.mkdir()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    run_id = f"memory-{EXECUTION_ID.hex}"
    staged = TimeoutStaged(input_dir)
    worker = LocalAndroidMemoryWorker(
        python_binary=Path("/usr/local/bin/python3"),
        repository_root=repository_root,
        run_root=tmp_path / "runs",
        runtime_commit="c" * 40,
        max_output_bytes=1024,
        process_factory=TimeoutProcessFactory(),
        commit_resolver=timeout_commit,
    )
    await worker.start(
        run_id=run_id,
        staged=staged,  # type: ignore[arg-type]
        question=None,
        timeout_seconds=0.001,  # type: ignore[arg-type]
    )
    deadline = asyncio.get_running_loop().time() + 5
    while await worker.status(run_id) == "running":
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("worker did not reach a terminal state")
        await asyncio.sleep(0.001)
    assert await worker.status(run_id) == "timed_out"

    repository = FakeRepository()
    repository.record = _record(
        engine_id="android_memory",
        state="running",
        external_run_id=run_id,
        started_at=NOW,
        version=2,
    )
    adapter = AndroidMemoryAdapter(
        stager=object(),  # type: ignore[arg-type]
        worker=worker,
        max_timeout_seconds=900,
        now=lambda: NOW,
    )
    service = EngineExecutionService(
        repository=repository,  # type: ignore[arg-type]
        workspace_service=FakeWorkspaceService(),  # type: ignore[arg-type]
        registry=AdapterRegistry((adapter,)),
        engine_lock=_lock(),
        result_sink=FakeSink(),
        now=lambda: NOW,
    )

    outcome = await service.step(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=EXECUTION_ID,
    )

assert outcome.retry is not None
assert outcome.retry.mode == "new_attempt"
assert outcome.retry.stable_error_code == "engine_timeout"
assert outcome.retry.attempt_number == 2
assert repository.record.stable_error_code == "engine_timeout"
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
cd services/api
uv run pytest \
  tests/unit/test_android_memory_adapter.py::test_status_strictly_maps_worker_states \
  tests/unit/test_android_memory_adapter.py::test_fetch_result_maps_timeout_to_retryable_engine_timeout \
  tests/unit/test_android_memory_adapter.py::test_cancel_treats_timeout_as_finished_failure \
  tests/unit/test_engine_execution_service.py::test_real_memory_worker_timeout_reserves_new_attempt -q
```

Expected: failures because the adapter currently treats `timed_out` as an
unknown lost worker.

- [ ] **Step 4: Implement the adapter mappings**

Accept `timed_out` in `_read_worker_state()` and add explicit mappings:

```python
if worker_state == "timed_out":
    return EngineStatus(run_ref, "failed", "engine_timeout", True)
```

In `fetch_result()`, raise `_error("engine_timeout", retryable=True)` before
the generic failed/canceled branch. In `cancel()`, return `failed` for either
`failed` or `timed_out`.

- [ ] **Step 5: Run the Adapter and execution-service unit files**

Run:

```bash
cd services/api
uv run pytest \
  tests/unit/test_android_memory_adapter.py \
  tests/unit/test_engine_execution_service.py -q
uv run ruff check \
  src/perfpilot_api/engines/android_memory.py \
  tests/unit/test_android_memory_adapter.py \
  tests/unit/test_engine_execution_service.py
```

Expected: all tests pass and Ruff reports no errors.

### Task 3: Prove the retry cap and run regression checks

**Files:**

- Test: `services/api/tests/integration/test_engine_execution_repository.py`

- [ ] **Step 1: Write the retry-limit integration test**

Reserve `engine_timeout` retries until the fixture's `max_retries=2` is
exhausted. Mark each next attempt running before reserving again. Assert that
the first two reservations create attempts 2 and 3, the third creates no next
attempt, and `GlobalJob.retry_count` remains 2.

```python
@pytest.mark.asyncio
async def test_engine_timeout_retries_stop_at_job_limit(
    execution_database: ExecutionDatabase,
) -> None:
    current = await _running(execution_database)
    created_attempts: list[int] = []

    for attempt_number in (2, 3):
        reservation = await execution_database.repository.reserve_retry(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=current.id,
            stable_error_code="engine_timeout",
            now=NOW,
            deadline_seconds=1800,
        )
        assert reservation.next_attempt is not None
        created_attempts.append(reservation.next_attempt.attempt_number)
        pending = reservation.next_attempt
        current = await execution_database.repository.mark_submitted(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=pending.id,
            expected_version=pending.version,
            run_ref=_run_ref(run_id=f"run-{attempt_number}"),
            now=NOW,
        )

    exhausted = await execution_database.repository.reserve_retry(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        execution_id=current.id,
        stable_error_code="engine_timeout",
        now=NOW,
        deadline_seconds=1800,
    )
    async with execution_database.sessions() as session:
        job = await session.get(GlobalJob, ANALYSIS_ID)

    assert created_attempts == [2, 3]
    assert exhausted.next_attempt is None
    assert job is not None and job.retry_count == job.max_retries == 2
```

- [ ] **Step 2: Run the integration test with PostgreSQL**

Run:

```bash
cd services/api
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
uv run pytest \
  tests/integration/test_engine_execution_repository.py::test_engine_timeout_retries_stop_at_job_limit -q
```

Expected: one passing test with no skips.

- [ ] **Step 3: Run all API tests and static checks**

Run:

```bash
cd services/api
PERFPILOT_TEST_POSTGRES_URL="$PERFPILOT_TEST_POSTGRES_URL" \
PERFPILOT_REQUIRE_POSTGRES_TESTS=1 \
PERFPILOT_REQUIRE_ANDROID_MEMORY_UPSTREAM_TESTS=1 \
uv run pytest -q
uv run ruff check src tests
```

Expected: the complete API suite passes, required PostgreSQL and upstream tests
do not skip, and Ruff reports no errors.

- [ ] **Step 4: Commit the implementation**

```bash
git add \
  services/api/src/perfpilot_api/engines/android_memory_worker.py \
  services/api/src/perfpilot_api/engines/android_memory.py \
  services/api/tests/unit/test_android_memory_worker.py \
  services/api/tests/unit/test_android_memory_adapter.py \
  services/api/tests/unit/test_engine_execution_service.py \
  services/api/tests/integration/test_engine_execution_repository.py
git commit -m "fix: preserve Android memory timeouts"
```

- [ ] **Step 5: Push and verify required GitHub checks**

```bash
git push origin feature/perfpilot-android-memory-adapter
gh pr checks 1 --watch
```

Expected: `python-quality`, `python-tests`, `web`, and `ci-gate` all pass.
