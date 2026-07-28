from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest


TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("20000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("30000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("40000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)
CHECKSUM = "iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E="
OTHER_CHECKSUM = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_DEFAULT = object()


def test_request_hash_uses_one_canonical_descriptor_encoding() -> None:
    from perfpilot_api.services.uploads import (
        UploadDescriptor,
        canonical_upload_request_hash,
    )

    request_hash = canonical_upload_request_hash(
        analysis_id=ANALYSIS_ID,
        descriptor=UploadDescriptor(
            artifact_kind="apk",
            mime="application/vnd.android.package-archive",
            size=4,
            sha256_b64=CHECKSUM,
        ),
    )

    assert request_hash == "9a87edfeb1cecfc03d1b77d2e4a582ef132ce055e51698a19833b56ded906eb5"


class RecordingRepository:
    def __init__(self, stored: Any | None = None) -> None:
        self.events: list[str] = []
        self.stored = stored
        self.received: dict[str, object] = {}
        self.finalize_received: dict[str, object] = {}
        self.finalize_result: Any = _DEFAULT
        self.load_results: list[Any] = []

    async def reserve_slot(self, **kwargs: object) -> Any:
        from perfpilot_api.services.uploads import StoredUpload

        self.events.append("persisted")
        self.received = kwargs
        return self.stored or StoredUpload(
            artifact_id=ARTIFACT_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            artifact_kind="apk",
            mime="application/vnd.android.package-archive",
            size=4,
            sha256_b64=CHECKSUM,
            object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/apk/{UPLOAD_ID}",
            state="pending",
            expires_at=NOW + timedelta(minutes=15),
            version=1,
            version_id=None,
            finalized_at=None,
        )

    async def load_upload(self, **kwargs: object) -> Any:
        self.events.append("loaded")
        self.received = kwargs
        if self.load_results:
            return self.load_results.pop(0)
        return self.stored or _stored_upload()

    async def finalize_upload(self, **kwargs: object) -> Any:
        self.events.append("cas")
        self.finalize_received = kwargs
        if self.finalize_result is not _DEFAULT:
            return self.finalize_result
        return _stored_upload(
            state="finalized",
            version=2,
            version_id=str(kwargs["storage_version_id"]),
            finalized_at=kwargs["finalized_at"],
            expires_at=kwargs["expires_at"],
        )

    async def load_download(self, **kwargs: object) -> Any:
        self.events.append("download_loaded")
        self.received = kwargs
        return self.stored or _stored_upload(
            state="finalized",
            version=2,
            version_id="download-fixed-version",
            finalized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(days=29),
        )


class RecordingStore:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.events: list[str] = []
        self.received: dict[str, object] = {}
        self.failure: Exception | None = None
        self.head_metadata: Any | None = None

    async def authorize_put(self, **kwargs: object) -> Any:
        from perfpilot_api.storage.base import PutAuthorization

        assert self.repository.events == ["persisted"]
        self.events.append("presigned")
        self.received = kwargs
        if self.failure is not None:
            raise self.failure
        return PutAuthorization(
            location=kwargs["location"],
            url="https://objects.example/signed-secret",
            required_headers={
                "Content-Type": "application/vnd.android.package-archive",
                "x-amz-checksum-sha256": CHECKSUM,
            },
            expires_in_seconds=900,
        )

    async def head(self, **kwargs: object) -> Any:
        from perfpilot_api.storage.base import StoredObjectMetadata

        self.events.append("head")
        self.received = kwargs
        if self.failure is not None:
            raise self.failure
        return self.head_metadata or StoredObjectMetadata(
            location=kwargs["location"],
            checksum_sha256_b64=CHECKSUM,
            content_type="application/vnd.android.package-archive",
            size_bytes=4,
        )

    async def authorize_get(self, **kwargs: object) -> Any:
        from perfpilot_api.storage.base import GetAuthorization

        self.events.append("get_presigned")
        self.received = kwargs
        if self.failure is not None:
            raise self.failure
        return GetAuthorization(
            location=kwargs["location"],
            url="https://objects.example/download-secret",
            expires_in_seconds=int(kwargs["expires_in_seconds"]),
        )


class FixedBucketResolver:
    async def active_for_team(self, team_id: UUID) -> Any:
        from perfpilot_api.services.uploads import TenantBucket

        assert team_id == TEAM_ID
        return TenantBucket(
            team_id=TEAM_ID,
            bucket="pp-team-a",
            resource_version=1,
        )


def _stored_upload(**changes: object) -> Any:
    from dataclasses import replace

    from perfpilot_api.services.uploads import StoredUpload

    stored = StoredUpload(
        artifact_id=ARTIFACT_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        artifact_kind="apk",
        mime="application/vnd.android.package-archive",
        size=4,
        sha256_b64=CHECKSUM,
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/apk/{UPLOAD_ID}",
        state="pending",
        expires_at=NOW + timedelta(minutes=15),
        version=1,
        version_id=None,
        finalized_at=None,
    )
    return replace(stored, **changes)


def _service(repository: RecordingRepository, store: RecordingStore) -> Any:
    from perfpilot_api.services.uploads import UploadService

    return UploadService(
        repository=repository,
        artifact_store=store,
        bucket_resolver=FixedBucketResolver(),
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
    )


async def _create_slot(service: Any) -> Any:
    return await service.create_slot(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="upload-apk-1",
        artifact_kind="apk",
        mime="application/vnd.android.package-archive",
        size=4,
        sha256_b64=CHECKSUM,
    )


@pytest.mark.asyncio
async def test_live_slot_presign_uses_only_the_remaining_absolute_lifetime() -> None:
    repository = RecordingRepository(_stored_upload(expires_at=NOW + timedelta(minutes=5)))
    store = RecordingStore(repository)

    slot = await _create_slot(_service(repository, store))

    assert slot.expires_at == NOW + timedelta(minutes=5)
    assert store.received["expires_in_seconds"] == 300


@pytest.mark.asyncio
async def test_finalized_idempotent_slot_never_returns_another_put() -> None:
    repository = RecordingRepository(
        _stored_upload(
            state="finalized",
            expires_at=NOW + timedelta(days=30),
            version_id="private-version-id",
            finalized_at=NOW - timedelta(seconds=1),
        )
    )
    store = RecordingStore(repository)

    slot = await _create_slot(_service(repository, store))

    assert slot.state == "finalized"
    assert slot.put_url is None
    assert slot.required_headers == {}
    assert store.events == []
    assert "private-version-id" not in repr(slot)


@pytest.mark.asyncio
async def test_repository_returning_an_expired_slot_fails_without_presigning() -> None:
    from perfpilot_api.services.uploads import UploadExpiredError

    repository = RecordingRepository(_stored_upload(expires_at=NOW))
    store = RecordingStore(repository)

    with pytest.raises(UploadExpiredError):
        await _create_slot(_service(repository, store))

    assert store.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_layer", ["resolver", "repository", "storage"])
async def test_dependency_failure_is_mapped_without_a_secret_exception_chain(
    failure_layer: str,
) -> None:
    from perfpilot_api.services.uploads import UploadService, UploadUnavailableError

    class FailingRepository(RecordingRepository):
        async def reserve_slot(self, **kwargs: object) -> Any:
            raise RuntimeError("signed-secret and private bucket leaked here")

    class FailingBucketResolver(FixedBucketResolver):
        async def active_for_team(self, team_id: UUID) -> Any:
            raise RuntimeError("signed-secret and private bucket leaked here")

    repository = FailingRepository() if failure_layer == "repository" else RecordingRepository()
    store = RecordingStore(repository)
    if failure_layer == "storage":
        store.failure = RuntimeError("signed-secret and private bucket leaked here")
    service = UploadService(
        repository=repository,
        artifact_store=store,
        bucket_resolver=(
            FailingBucketResolver() if failure_layer == "resolver" else FixedBucketResolver()
        ),
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
    )

    with pytest.raises(UploadUnavailableError) as captured:
        await _create_slot(service)

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert "signed-secret" not in rendered
    assert "private bucket" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_error_name", ["metadata", "not_found"])
async def test_finalize_maps_storage_metadata_errors_to_a_redacted_mismatch(
    storage_error_name: str,
) -> None:
    from perfpilot_api.services.uploads import UploadMismatchError
    from perfpilot_api.storage.base import ArtifactMetadataError, ArtifactNotFoundError

    repository = RecordingRepository(_stored_upload())
    store = RecordingStore(repository)
    store.failure = (
        ArtifactMetadataError() if storage_error_name == "metadata" else ArtifactNotFoundError()
    )

    with pytest.raises(UploadMismatchError) as captured:
        await _service(repository, store).finalize(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            caller_sha256_b64=CHECKSUM,
            caller_size=4,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "artifact object" not in str(captured.value)


@pytest.mark.asyncio
async def test_finalize_compares_metadata_then_cas_saves_the_exact_storage_version() -> None:
    from perfpilot_api.storage.base import ObjectLocation, StoredObjectMetadata

    repository = RecordingRepository(_stored_upload())
    store = RecordingStore(repository)
    store.head_metadata = StoredObjectMetadata(
        location=ObjectLocation(
            bucket="pp-team-a",
            key=f"raw/analyses/{ANALYSIS_ID}/inputs/apk/{UPLOAD_ID}",
            version_id="fixed-storage-version",
        ),
        checksum_sha256_b64=CHECKSUM,
        content_type="application/vnd.android.package-archive",
        size_bytes=4,
    )

    artifact = await _service(repository, store).finalize(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        caller_sha256_b64=CHECKSUM,
        caller_size=4,
    )

    assert repository.events == ["loaded", "cas"]
    assert store.events == ["head"]
    assert repository.finalize_received["expected_version"] == 1
    assert repository.finalize_received["storage_version_id"] == "fixed-storage-version"
    assert artifact.state == "finalized"
    assert artifact.version_id == "fixed-storage-version"
    assert "fixed-storage-version" not in repr(artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller_size", "caller_checksum", "metadata_size", "metadata_checksum", "metadata_mime"),
    [
        (5, CHECKSUM, 4, CHECKSUM, "application/vnd.android.package-archive"),
        (4, OTHER_CHECKSUM, 4, CHECKSUM, "application/vnd.android.package-archive"),
        (4, CHECKSUM, 5, CHECKSUM, "application/vnd.android.package-archive"),
        (4, CHECKSUM, 4, OTHER_CHECKSUM, "application/vnd.android.package-archive"),
        (4, CHECKSUM, 4, CHECKSUM, "application/octet-stream"),
    ],
)
async def test_finalize_rejects_any_caller_database_or_storage_mismatch(
    caller_size: int,
    caller_checksum: str,
    metadata_size: int,
    metadata_checksum: str,
    metadata_mime: str,
) -> None:
    from perfpilot_api.services.uploads import UploadMismatchError
    from perfpilot_api.storage.base import ObjectLocation, StoredObjectMetadata

    repository = RecordingRepository(_stored_upload())
    store = RecordingStore(repository)
    store.head_metadata = StoredObjectMetadata(
        location=ObjectLocation(
            bucket="pp-team-a",
            key=f"raw/analyses/{ANALYSIS_ID}/inputs/apk/{UPLOAD_ID}",
            version_id="private-version",
        ),
        checksum_sha256_b64=metadata_checksum,
        content_type=metadata_mime,
        size_bytes=metadata_size,
    )

    with pytest.raises(UploadMismatchError):
        await _service(repository, store).finalize(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            caller_sha256_b64=caller_checksum,
            caller_size=caller_size,
        )

    assert "cas" not in repository.events


@pytest.mark.asyncio
async def test_repeated_finalize_returns_the_fixed_version_without_another_head() -> None:
    repository = RecordingRepository(
        _stored_upload(
            state="finalized",
            version=2,
            version_id="first-fixed-version",
            finalized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(days=29),
        )
    )
    store = RecordingStore(repository)

    artifact = await _service(repository, store).finalize(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        caller_sha256_b64=CHECKSUM,
        caller_size=4,
    )

    assert artifact.version_id == "first-fixed-version"
    assert repository.events == ["loaded"]
    assert store.events == []


@pytest.mark.asyncio
async def test_finalize_rejects_an_expired_pending_slot_before_storage_head() -> None:
    from perfpilot_api.services.uploads import UploadExpiredError

    repository = RecordingRepository(_stored_upload(expires_at=NOW))
    store = RecordingStore(repository)

    with pytest.raises(UploadExpiredError):
        await _service(repository, store).finalize(
            team_id=TEAM_ID,
            analysis_id=ANALYSIS_ID,
            upload_id=UPLOAD_ID,
            caller_sha256_b64=CHECKSUM,
            caller_size=4,
        )

    assert repository.events == ["loaded"]
    assert store.events == []


@pytest.mark.asyncio
async def test_losing_finalize_cas_reloads_and_returns_the_winning_fixed_version() -> None:
    repository = RecordingRepository()
    repository.load_results = [
        _stored_upload(),
        _stored_upload(
            state="finalized",
            version=2,
            version_id="winning-fixed-version",
            finalized_at=NOW,
            expires_at=NOW + timedelta(days=30),
        ),
    ]
    repository.finalize_result = None
    store = RecordingStore(repository)
    from perfpilot_api.storage.base import ObjectLocation, StoredObjectMetadata

    store.head_metadata = StoredObjectMetadata(
        location=ObjectLocation(
            bucket="pp-team-a",
            key=f"raw/analyses/{ANALYSIS_ID}/inputs/apk/{UPLOAD_ID}",
            version_id="losing-observed-version",
        ),
        checksum_sha256_b64=CHECKSUM,
        content_type="application/vnd.android.package-archive",
        size_bytes=4,
    )

    artifact = await _service(repository, store).finalize(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        caller_sha256_b64=CHECKSUM,
        caller_size=4,
    )

    assert artifact.version_id == "winning-fixed-version"
    assert repository.events == ["loaded", "cas", "loaded"]
    assert store.events == ["head"]


@pytest.mark.asyncio
async def test_download_uses_only_the_finalized_fixed_version_for_five_minutes() -> None:
    repository = RecordingRepository(
        _stored_upload(
            state="finalized",
            version=2,
            version_id="download-fixed-version",
            finalized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(days=29),
        )
    )
    store = RecordingStore(repository)

    authorization = await _service(repository, store).download(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        artifact_id=ARTIFACT_ID,
    )

    location = store.received["location"]
    assert location.version_id == "download-fixed-version"
    assert store.received["expires_in_seconds"] == 300
    assert authorization.expires_at == NOW + timedelta(minutes=5)
    rendered = repr(authorization)
    assert "download-secret" not in rendered
    assert "download-fixed-version" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["finalize", "download"])
async def test_finalize_and_download_redact_storage_failure_context(operation: str) -> None:
    from perfpilot_api.services.uploads import UploadUnavailableError

    repository = RecordingRepository(
        _stored_upload(
            state="finalized" if operation == "download" else "pending",
            version=2 if operation == "download" else 1,
            version_id="download-version" if operation == "download" else None,
            finalized_at=NOW if operation == "download" else None,
            expires_at=NOW + timedelta(days=1),
        )
    )
    store = RecordingStore(repository)
    store.failure = RuntimeError("private-storage-marker")
    service = _service(repository, store)

    with pytest.raises(UploadUnavailableError) as captured:
        if operation == "finalize":
            await service.finalize(
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                upload_id=UPLOAD_ID,
                caller_sha256_b64=CHECKSUM,
                caller_size=4,
            )
        else:
            await service.download(
                team_id=TEAM_ID,
                analysis_id=ANALYSIS_ID,
                artifact_id=ARTIFACT_ID,
            )

    assert "private-storage-marker" not in str(captured.value)
    assert "private-storage-marker" not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"idempotency_key": "contains/slash"},
        {"artifact_kind": "arbitrary_customer_path"},
        {"mime": "Application/ZIP"},
        {"size": 0},
        {"size": True},
        {"size": 5 * 1024 * 1024 * 1024 + 1},
        {"sha256_b64": "not-a-canonical-sha256"},
    ],
)
async def test_create_slot_rejects_noncanonical_or_unapproved_descriptors(
    change: dict[str, object],
) -> None:
    from perfpilot_api.services.uploads import UploadInvalidRequestError, UploadService

    repository = RecordingRepository()
    service = UploadService(
        repository=repository,
        artifact_store=RecordingStore(repository),
        bucket_resolver=FixedBucketResolver(),
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
    )
    request: dict[str, object] = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "idempotency_key": "upload-apk-1",
        "artifact_kind": "apk",
        "mime": "application/vnd.android.package-archive",
        "size": 4,
        "sha256_b64": CHECKSUM,
    }
    request.update(change)

    with pytest.raises(UploadInvalidRequestError):
        await service.create_slot(**request)  # type: ignore[arg-type]

    assert repository.events == []


@pytest.mark.asyncio
async def test_create_slot_persists_before_returning_a_presigned_put() -> None:
    from perfpilot_api.services.uploads import UploadService

    repository = RecordingRepository()
    store = RecordingStore(repository)
    service = UploadService(
        repository=repository,
        artifact_store=store,
        bucket_resolver=FixedBucketResolver(),
        clock=lambda: NOW,
        uuid_source=iter((ARTIFACT_ID, UPLOAD_ID)).__next__,
    )

    slot = await service.create_slot(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        idempotency_key="upload-apk-1",
        artifact_kind="apk",
        mime="application/vnd.android.package-archive",
        size=4,
        sha256_b64=CHECKSUM,
    )

    assert repository.events == ["persisted"]
    assert store.events == ["presigned"]
    assert slot.upload_id == UPLOAD_ID
    assert slot.expires_at == NOW + timedelta(minutes=15)
    assert slot.required_headers == {
        "Content-Type": "application/vnd.android.package-archive",
        "x-amz-checksum-sha256": CHECKSUM,
    }
    assert "signed-secret" not in repr(slot)
