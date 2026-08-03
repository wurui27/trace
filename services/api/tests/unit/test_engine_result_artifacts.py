from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import boto3
import pytest
from botocore.stub import Stubber

from perfpilot_api.engines.canonical_results import (
    EngineResultValidationError,
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.services.engine_result_artifacts import (
    EngineResultArtifactRecord,
    EngineResultConflictError,
    EngineResultUnavailableError,
    S3EngineResultSink,
)
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")
ARTIFACT_ID = result_artifact_id(EXECUTION_ID)
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
BUCKET = "private-engine-results"
PUT_VERSION = "put-version-1"
WINNER_VERSION = "winner-version-2"
TENANT = TenantBucket(team_id=TEAM_ID, bucket=BUCKET, resource_version=7)
_AUTO = object()


def _payload() -> dict[str, object]:
    return {
        "reportId": "report-1",
        "report": {
            "reportId": "report-1",
            "summary": {"conclusion": "Main thread blocked"},
        },
    }


def _write(*, payload: dict[str, object] | None = None, **changes: object) -> EngineResultWrite:
    values: dict[str, object] = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "execution_id": EXECUTION_ID,
        "expected_execution_version": 3,
        "tenant_resource_version": 7,
        "artifact_id": ARTIFACT_ID,
        "engine_id": "smartperfetto",
        "adapter_version": "1.0.0",
        "engine_commit_sha": "1" * 40,
        "engine_image_digest": "sha256:" + "2" * 64,
        "attempt_number": 1,
        "input_manifest_hash": "3" * 64,
        "config_hash": "4" * 64,
        "result": EngineResult(
            contract="workspace-agent-v1",
            state="completed",
            payload=_payload() if payload is None else payload,
        ),
    }
    values.update(changes)
    return EngineResultWrite(**values)  # type: ignore[arg-type]


def _object_key() -> str:
    return f"raw/analyses/{ANALYSIS_ID}/internal/engine-results/{ARTIFACT_ID}.json"


def _record(
    request: EngineResultWrite,
    *,
    state: str = "pending",
    version_id: str | None = None,
) -> EngineResultArtifactRecord:
    canonical = canonicalize_engine_result(request)
    return EngineResultArtifactRecord(
        artifact_id=request.artifact_id,
        analysis_id=request.analysis_id,
        upload_id=request.artifact_id,
        idempotency_key=f"internal:engine_result:{request.execution_id}",
        request_hash=canonical.request_hash_hex,
        artifact_kind="engine_result",
        mime_type="application/json",
        size_bytes=len(canonical.canonical_bytes),
        sha256_b64=canonical.checksum_sha256_b64,
        object_key=_object_key(),
        state=state,
        expires_at=NOW + timedelta(days=30),
        version=2 if state == "finalized" else 1,
        version_id=version_id if state == "finalized" else None,
    )


class FakeRepository:
    def __init__(self, record: EngineResultArtifactRecord) -> None:
        self.record = record
        self.finalize_result: object = _AUTO
        self.reload_record: EngineResultArtifactRecord | None = None
        self.fence_failure_at: int | None = None
        self.fence_count = 0
        self.events: list[tuple[str, dict[str, object]]] = []

    async def reserve(self, **kwargs: object) -> EngineResultArtifactRecord:
        self.events.append(("reserve", kwargs))
        return self.record

    async def require_resource_version(self, tenant: TenantBucket) -> None:
        self.fence_count += 1
        self.events.append(("fence", {"tenant": tenant}))
        if self.fence_failure_at == self.fence_count:
            raise RuntimeError("private route rollover detail")

    async def finalize(self, **kwargs: object) -> EngineResultArtifactRecord | None:
        self.events.append(("finalize", kwargs))
        if self.finalize_result is _AUTO:
            version_id = kwargs["storage_version_id"]
            assert isinstance(version_id, str)
            return replace(
                self.record,
                state="finalized",
                version=2,
                version_id=version_id,
            )
        assert self.finalize_result is None or isinstance(
            self.finalize_result,
            EngineResultArtifactRecord,
        )
        return self.finalize_result

    async def reload(self, **kwargs: object) -> EngineResultArtifactRecord:
        self.events.append(("reload", kwargs))
        if self.reload_record is None:
            raise RuntimeError("private missing winner detail")
        return self.reload_record


class FakeResolver:
    def __init__(self, result: object = TENANT) -> None:
        self.result = result
        self.failure: BaseException | None = None
        self.on_call: object = None
        self.calls: list[UUID] = []

    async def active_for_team(self, team_id: UUID) -> object:
        self.calls.append(team_id)
        if callable(self.on_call):
            self.on_call()
        if self.failure is not None:
            raise self.failure
        return self.result


class ClosingBody:
    def __init__(self, payload: bytes, *, failure: BaseException | None = None) -> None:
        self.payload = payload
        self.failure = failure
        self.closed = False

    def read(self) -> bytes:
        if self.failure is not None:
            raise self.failure
        return self.payload

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    def __init__(self, request: EngineResultWrite) -> None:
        canonical = canonicalize_engine_result(request)
        self.payload = canonical.canonical_bytes
        self.checksum = canonical.checksum_sha256_b64
        self.events: list[tuple[str, dict[str, object]]] = []
        self.failures: dict[str, BaseException] = {}
        self.put_response: object = {
            "VersionId": PUT_VERSION,
            "ChecksumSHA256": self.checksum,
        }
        self.head_response: object = {
            "VersionId": PUT_VERSION,
            "ChecksumSHA256": self.checksum,
            "ContentType": "application/json",
            "ContentLength": len(self.payload),
            "DeleteMarker": False,
        }
        self.body = ClosingBody(self.payload)
        self.get_response: object = {
            **self.head_response,  # type: ignore[arg-type]
            "Body": self.body,
        }

    def _result(self, operation: str, kwargs: dict[str, object], response: object) -> object:
        self.events.append((operation, kwargs))
        if operation in self.failures:
            raise self.failures[operation]
        return response

    def put_object(self, **kwargs: object) -> object:
        return self._result("put", kwargs, self.put_response)

    def head_object(self, **kwargs: object) -> object:
        return self._result("head", kwargs, self.head_response)

    def get_object(self, **kwargs: object) -> object:
        return self._result("get", kwargs, self.get_response)


def _sink(
    repository: FakeRepository,
    client: object,
    *,
    resolver: FakeResolver | None = None,
) -> S3EngineResultSink:
    return S3EngineResultSink(
        repository=repository,
        bucket_resolver=resolver or FakeResolver(),  # type: ignore[arg-type]
        client=client,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_validation_finishes_before_any_dependency_and_defensively_copies() -> None:
    payload = _payload()
    request = _write(payload=payload)
    canonical_before_await = canonicalize_engine_result(request)
    repository = FakeRepository(_record(request))
    resolver = FakeResolver()
    client = FakeS3Client(request)

    def mutate_payload() -> None:
        report = payload["report"]
        assert isinstance(report, dict)
        report["summary"] = {"conclusion": "mutated after validation"}

    resolver.on_call = mutate_payload
    assert await _sink(repository, client, resolver=resolver).write(request) == ARTIFACT_ID
    put = next(kwargs for event, kwargs in client.events if event == "put")
    assert put["Body"] == canonical_before_await.canonical_bytes

    invalid = replace(request, artifact_id=uuid4())
    empty_repository = FakeRepository(_record(request))
    empty_resolver = FakeResolver()
    empty_client = FakeS3Client(request)
    with pytest.raises(EngineResultValidationError):
        await _sink(empty_repository, empty_client, resolver=empty_resolver).write(invalid)
    assert empty_resolver.calls == []
    assert empty_repository.events == []
    assert empty_client.events == []


@pytest.mark.asyncio
async def test_pending_write_uses_exact_s3_version_and_metadata() -> None:
    request = _write()
    canonical = canonicalize_engine_result(request)
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)

    assert await _sink(repository, client).write(request) == ARTIFACT_ID

    assert [event for event, _ in client.events] == ["put", "head"]
    assert client.events[0][1] == {
        "Bucket": BUCKET,
        "Key": _object_key(),
        "Body": canonical.canonical_bytes,
        "ContentType": "application/json",
        "ChecksumSHA256": canonical.checksum_sha256_b64,
    }
    assert client.events[1][1] == {
        "Bucket": BUCKET,
        "Key": _object_key(),
        "VersionId": PUT_VERSION,
        "ChecksumMode": "ENABLED",
    }
    assert [event for event, _ in repository.events] == [
        "reserve",
        "fence",
        "fence",
        "fence",
        "finalize",
        "fence",
    ]
    finalize = repository.events[-2][1]
    assert finalize["storage_version_id"] == PUT_VERSION
    assert finalize["expires_at"] == NOW + timedelta(days=30)


@pytest.mark.parametrize(
    "resolved",
    [
        object(),
        TenantBucket(uuid4(), BUCKET, 7),
        TenantBucket(TEAM_ID, BUCKET, 8),
        TenantBucket(TEAM_ID, "", 7),
        TenantBucket(TEAM_ID, "bad\nbucket", 7),
    ],
)
@pytest.mark.asyncio
async def test_resolver_identity_version_and_bucket_fail_closed(resolved: object) -> None:
    request = _write()
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)
    with pytest.raises(EngineResultUnavailableError):
        await _sink(repository, client, resolver=FakeResolver(resolved)).write(request)
    assert repository.events == []
    assert client.events == []


@pytest.mark.parametrize(
    ("fence_failure", "winner", "client_events"),
    [
        (1, False, []),
        (2, False, []),
        (3, False, ["put", "head"]),
        (4, False, ["put", "head"]),
        (4, True, ["put", "head"]),
        (5, True, ["put", "head", "get"]),
    ],
)
@pytest.mark.asyncio
async def test_every_resource_version_fence_fails_before_crossing_its_boundary(
    fence_failure: int,
    winner: bool,
    client_events: list[str],
) -> None:
    request = _write()
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)
    if winner:
        repository.finalize_result = None
        repository.reload_record = _record(
            request,
            state="finalized",
            version_id=WINNER_VERSION,
        )
        assert isinstance(client.get_response, dict)
        client.get_response["VersionId"] = WINNER_VERSION
    repository.fence_failure_at = fence_failure

    with pytest.raises(EngineResultUnavailableError) as caught:
        await _sink(repository, client).write(request)
    assert [event for event, _ in client.events] == client_events
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("version_id", [None, "", "null", "bad\nversion", "x" * 1025])
@pytest.mark.asyncio
async def test_put_requires_a_safe_nonempty_immutable_version(version_id: object) -> None:
    request = _write()
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)
    client.put_response = {"VersionId": version_id, "ChecksumSHA256": client.checksum}
    with pytest.raises(EngineResultUnavailableError):
        await _sink(repository, client).write(request)
    assert [event for event, _ in client.events] == ["put"]
    assert all(event != "finalize" for event, _ in repository.events)


@pytest.mark.asyncio
async def test_put_checksum_mismatch_is_an_integrity_conflict() -> None:
    request = _write()
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)
    client.put_response = {"VersionId": PUT_VERSION, "ChecksumSHA256": "x" * 44}
    with pytest.raises(EngineResultConflictError):
        await _sink(repository, client).write(request)


@pytest.mark.parametrize(
    "change",
    [
        {"VersionId": "other-version"},
        {"ChecksumSHA256": "x" * 44},
        {"ContentType": "text/plain"},
        {"ContentLength": 1},
        {"DeleteMarker": True},
    ],
)
@pytest.mark.asyncio
async def test_head_must_verify_the_exact_put_version(change: dict[str, object]) -> None:
    request = _write()
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)
    assert isinstance(client.head_response, dict)
    client.head_response.update(change)
    with pytest.raises(EngineResultConflictError):
        await _sink(repository, client).write(request)
    assert all(event != "finalize" for event, _ in repository.events)


@pytest.mark.asyncio
async def test_finalized_replay_reads_exact_version_and_always_closes_body() -> None:
    request = _write()
    record = _record(request, state="finalized", version_id=WINNER_VERSION)
    repository = FakeRepository(record)
    client = FakeS3Client(request)
    assert isinstance(client.get_response, dict)
    client.get_response["VersionId"] = WINNER_VERSION

    assert await _sink(repository, client).write(request) == ARTIFACT_ID
    assert [event for event, _ in client.events] == ["get"]
    assert client.events[0][1]["VersionId"] == WINNER_VERSION
    assert client.body.closed is True


@pytest.mark.parametrize("failure_kind", ["read", "metadata", "bytes"])
@pytest.mark.asyncio
async def test_finalized_replay_closes_body_on_every_failure(failure_kind: str) -> None:
    request = _write()
    record = _record(request, state="finalized", version_id=WINNER_VERSION)
    repository = FakeRepository(record)
    client = FakeS3Client(request)
    assert isinstance(client.get_response, dict)
    client.get_response["VersionId"] = WINNER_VERSION
    expected_error: type[Exception]
    if failure_kind == "read":
        client.body.failure = RuntimeError("private read error")
        expected_error = EngineResultUnavailableError
    elif failure_kind == "metadata":
        client.get_response["ChecksumSHA256"] = "x" * 44
        expected_error = EngineResultConflictError
    else:
        client.body.payload = b"different canonical bytes"
        expected_error = EngineResultConflictError

    with pytest.raises(expected_error) as caught:
        await _sink(repository, client).write(request)
    assert client.body.closed is True
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_pending_crash_repair_and_concurrent_winner_paths() -> None:
    request = _write()
    pending = _record(request)
    repository = FakeRepository(pending)
    client = FakeS3Client(request)
    assert await _sink(repository, client).write(request) == ARTIFACT_ID

    repository = FakeRepository(pending)
    repository.finalize_result = None
    repository.reload_record = _record(
        request,
        state="finalized",
        version_id=WINNER_VERSION,
    )
    client = FakeS3Client(request)
    assert isinstance(client.get_response, dict)
    client.get_response["VersionId"] = WINNER_VERSION
    assert await _sink(repository, client).write(request) == ARTIFACT_ID
    assert [event for event, _ in client.events] == ["put", "head", "get"]
    assert client.events[-1][1]["VersionId"] == WINNER_VERSION
    assert client.body.closed is True


@pytest.mark.parametrize(
    "changes",
    [
        {"upload_id": uuid4()},
        {"request_hash": "f" * 64},
        {"mime_type": "text/plain"},
        {"size_bytes": 1},
        {"sha256_b64": "x" * 44},
        {"object_key": "private/object/key"},
        {"state": "expired"},
        {"expires_at": NOW},
        {"version": True},
    ],
)
@pytest.mark.asyncio
async def test_conflicting_reserved_metadata_never_reaches_s3(
    changes: dict[str, object],
) -> None:
    request = _write()
    repository = FakeRepository(replace(_record(request), **changes))  # type: ignore[arg-type]
    client = FakeS3Client(request)
    with pytest.raises(EngineResultConflictError) as caught:
        await _sink(repository, client).write(request)
    assert client.events == []
    assert "private/object/key" not in repr(caught.value)


@pytest.mark.parametrize(
    "failure",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
)
@pytest.mark.asyncio
async def test_process_control_exceptions_propagate(failure: BaseException) -> None:
    request = _write()
    repository = FakeRepository(_record(request))
    resolver = FakeResolver()
    resolver.failure = failure
    client = FakeS3Client(request)
    with pytest.raises(type(failure)):
        await _sink(repository, client, resolver=resolver).write(request)


@pytest.mark.asyncio
async def test_dependency_details_and_storage_coordinates_are_fully_redacted() -> None:
    request = _write()
    canonical = canonicalize_engine_result(request)
    marker = f"{BUCKET} {_object_key()} {PUT_VERSION} {canonical.canonical_bytes!r}"
    repository = FakeRepository(_record(request))
    client = FakeS3Client(request)
    client.failures["put"] = RuntimeError(marker)
    with pytest.raises(EngineResultUnavailableError) as caught:
        await _sink(repository, client).write(request)
    for secret in (BUCKET, _object_key(), PUT_VERSION, "Main thread blocked"):
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_botocore_stubber_enforces_exact_put_and_head_requests() -> None:
    request = _write()
    canonical = canonicalize_engine_result(request)
    repository = FakeRepository(_record(request))
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {"VersionId": PUT_VERSION, "ChecksumSHA256": canonical.checksum_sha256_b64},
            {
                "Bucket": BUCKET,
                "Key": _object_key(),
                "Body": canonical.canonical_bytes,
                "ContentType": "application/json",
                "ChecksumSHA256": canonical.checksum_sha256_b64,
            },
        )
        stubber.add_response(
            "head_object",
            {
                "VersionId": PUT_VERSION,
                "ChecksumSHA256": canonical.checksum_sha256_b64,
                "ContentType": "application/json",
                "ContentLength": len(canonical.canonical_bytes),
                "DeleteMarker": False,
            },
            {
                "Bucket": BUCKET,
                "Key": _object_key(),
                "VersionId": PUT_VERSION,
                "ChecksumMode": "ENABLED",
            },
        )
        assert await _sink(repository, client).write(request) == ARTIFACT_ID
        stubber.assert_no_pending_responses()
