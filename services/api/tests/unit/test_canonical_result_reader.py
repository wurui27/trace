from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.services.canonical_result_reader import (
    CanonicalResultIntegrityError,
    CanonicalResultReader,
    CanonicalResultUnavailableError,
)
from perfpilot_api.services.engine_result_artifacts import EngineResultArtifactRecord
from perfpilot_api.services.uploads import TenantBucket


TEAM_ID = UUID("91000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("92000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("93000000-0000-4000-8000-000000000001")
ARTIFACT_ID = result_artifact_id(EXECUTION_ID)
TENANT = TenantBucket(TEAM_ID, "tenant-private-a", 7)


class Execution:
    team_id = TEAM_ID
    analysis_id = ANALYSIS_ID
    id = EXECUTION_ID
    raw_result_artifact_id = ARTIFACT_ID
    tenant_resource_version = 7
    engine_id = "smartperfetto"
    state = "completed"
    adapter_version = "1.0.0"
    engine_commit_sha = "1" * 40
    engine_image_digest = "sha256:" + "2" * 64
    attempt_number = 1


def _canonical() -> object:
    return canonicalize_engine_result(
        EngineResultWrite(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            execution_id=EXECUTION_ID,
            expected_execution_version=1,
            tenant_resource_version=7,
            artifact_id=ARTIFACT_ID,
            engine_id="smartperfetto",
            adapter_version="1.0.0",
            engine_commit_sha="1" * 40,
            engine_image_digest="sha256:" + "2" * 64,
            attempt_number=1,
            input_manifest_hash="3" * 64,
            config_hash="4" * 64,
            result=EngineResult(
                contract="workspace-agent-v1",
                state="completed",
                payload={"reportId": "report-1", "report": {"reportId": "report-1"}},
            ),
        )
    )


def _record() -> EngineResultArtifactRecord:
    canonical = _canonical()
    return EngineResultArtifactRecord(
        artifact_id=ARTIFACT_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=ARTIFACT_ID,
        idempotency_key=f"internal:engine_result:{EXECUTION_ID}",
        request_hash=canonical.request_hash_hex,  # type: ignore[attr-defined]
        artifact_kind="engine_result",
        mime_type="application/json",
        size_bytes=len(canonical.canonical_bytes),  # type: ignore[attr-defined]
        sha256_b64=canonical.checksum_sha256_b64,  # type: ignore[attr-defined]
        object_key=f"raw/analyses/{ANALYSIS_ID}/internal/engine-results/{ARTIFACT_ID}.json",
        state="finalized",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        version=2,
        version_id="immutable-engine-result-v1",
    )


class Body:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False
        self.amount: int | None = None

    def read(self, amount: int = -1) -> bytes:
        self.amount = amount
        return self.payload

    def close(self) -> None:
        self.closed = True


class Client:
    def __init__(self, record: EngineResultArtifactRecord) -> None:
        self.calls: list[dict[str, object]] = []
        canonical = _canonical()
        self.body = Body(canonical.canonical_bytes)  # type: ignore[attr-defined]
        self.response: dict[str, object] = {
            "VersionId": record.version_id,
            "ChecksumSHA256": record.sha256_b64,
            "ContentType": "application/json",
            "ContentLength": record.size_bytes,
            "DeleteMarker": False,
            "Body": self.body,
        }

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


class Resolver:
    def __init__(self, tenant: TenantBucket = TENANT) -> None:
        self.tenant = tenant

    async def active_for_team(self, team_id: UUID) -> TenantBucket:
        assert team_id == TEAM_ID
        return self.tenant


class Repository:
    def __init__(self, record: EngineResultArtifactRecord) -> None:
        self.record = record
        self.fences = 0

    async def require_resource_version(self, tenant: TenantBucket) -> None:
        self.fences += 1
        assert tenant == TENANT

    async def reload(
        self, *, tenant: TenantBucket, analysis_id: UUID, artifact_id: UUID
    ) -> EngineResultArtifactRecord:
        assert tenant == TENANT
        assert analysis_id == ANALYSIS_ID
        assert artifact_id == ARTIFACT_ID
        return self.record


def _reader(record: EngineResultArtifactRecord | None = None) -> tuple[CanonicalResultReader, Client]:
    final_record = record or _record()
    client = Client(final_record)
    return (
        CanonicalResultReader(
            artifact_repository=Repository(final_record),  # type: ignore[arg-type]
            bucket_resolver=Resolver(),  # type: ignore[arg-type]
            client=client,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_reader_reads_the_exact_finalized_s3_version_and_closes_body() -> None:
    reader, client = _reader()

    loaded = await reader.read(Execution())

    assert loaded.analysis_id == ANALYSIS_ID
    assert client.calls == [
        {
            "Bucket": "tenant-private-a",
            "Key": f"raw/analyses/{ANALYSIS_ID}/internal/engine-results/{ARTIFACT_ID}.json",
            "VersionId": "immutable-engine-result-v1",
            "ChecksumMode": "ENABLED",
        }
    ]
    assert client.body.amount == _record().size_bytes + 1
    assert client.body.closed is True
    assert "tenant-private-a" not in repr(loaded)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"state": "pending"},
        {"artifact_kind": "trace"},
        {"analysis_id": UUID("92000000-0000-4000-8000-000000000002")},
        {"version_id": None},
    ],
)
async def test_reader_rejects_nonfinalized_or_mismatched_artifact(change: dict[str, object]) -> None:
    reader, _client = _reader(replace(_record(), **change))
    with pytest.raises(CanonicalResultIntegrityError, match="^canonical result integrity failure$"):
        await reader.read(Execution())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"VersionId": "other"},
        {"ChecksumSHA256": base64.b64encode(b"x" * 32).decode()},
        {"ContentType": "text/plain"},
        {"ContentLength": 1},
        {"DeleteMarker": True},
    ],
)
async def test_reader_rejects_immutable_object_metadata_drift(change: dict[str, object]) -> None:
    reader, client = _reader()
    client.response.update(change)
    with pytest.raises(CanonicalResultIntegrityError, match="^canonical result integrity failure$"):
        await reader.read(Execution())
    assert client.body.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["overrun", "noncanonical", "identity"])
async def test_reader_rejects_invalid_body_bytes(mutation: str) -> None:
    reader, client = _reader()
    if mutation == "overrun":
        client.body.payload += b"x"
    elif mutation == "noncanonical":
        client.body.payload = b'{ "not": "canonical" }'
        client.response["ContentLength"] = len(client.body.payload)
    else:
        client.body.payload = client.body.payload.replace(
            str(ANALYSIS_ID).encode(), b"92000000-0000-4000-8000-000000000099"
        )
    with pytest.raises(CanonicalResultIntegrityError, match="^canonical result integrity failure$"):
        await reader.read(Execution())
    assert client.body.closed is True


@pytest.mark.asyncio
async def test_reader_rejects_route_version_or_team_guesses_as_unavailable() -> None:
    reader = CanonicalResultReader(
        artifact_repository=Repository(_record()),  # type: ignore[arg-type]
        bucket_resolver=Resolver(TenantBucket(TEAM_ID, "tenant-private-a", 8)),  # type: ignore[arg-type]
        client=Client(_record()),
    )
    with pytest.raises(CanonicalResultUnavailableError, match="^canonical result is unavailable$"):
        await reader.read(Execution())
