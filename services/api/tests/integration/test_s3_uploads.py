import asyncio
import threading
from collections.abc import Mapping
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from perfpilot_api.storage.base import (
    ArtifactAuthorizationError,
    ArtifactMetadataError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    GetAuthorization,
    MultipartPart,
    ObjectLocation,
    PutAuthorization,
    StoredObjectMetadata,
)
from perfpilot_api.storage.s3 import S3ArtifactStore


BUCKET = "pp-team-000000000000000000000001"
OBJECT_KEY = "raw/analysis-id/upload-id.apk"
VERSION_ID = "version-id-that-must-not-leak"
CHECKSUM_SHA256_B64 = "iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E="
CONTENT_TYPE = "application/vnd.android.package-archive"
SIGNED_URL = "https://signed.example.test/private-query"


class RecordingS3Client:
    def __init__(
        self,
        *,
        presigned_url: object = SIGNED_URL,
        head_response: object | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.presigned_url = presigned_url
        self.head_response = head_response
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.worker_thread_ids: list[int] = []

    def generate_presigned_url(self, **kwargs: Any) -> object:
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("generate_presigned_url", kwargs))
        if self.failure is not None:
            raise self.failure
        return self.presigned_url

    def head_object(self, **kwargs: Any) -> object:
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("head_object", kwargs))
        if self.failure is not None:
            raise self.failure
        return self.head_response

    def create_multipart_upload(self, **kwargs: Any) -> object:
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("create_multipart_upload", kwargs))
        if self.failure is not None:
            raise self.failure
        return {"UploadId": "private-storage-upload-id"}

    def complete_multipart_upload(self, **kwargs: Any) -> object:
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("complete_multipart_upload", kwargs))
        if self.failure is not None:
            raise self.failure
        return {"VersionId": VERSION_ID}

    def abort_multipart_upload(self, **kwargs: Any) -> object:
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("abort_multipart_upload", kwargs))
        if self.failure is not None:
            raise self.failure
        return {}


def _valid_head_response(*, version_id: str = VERSION_ID) -> dict[str, object]:
    return {
        "ContentLength": 4,
        "ContentType": CONTENT_TYPE,
        "ChecksumSHA256": CHECKSUM_SHA256_B64,
        "VersionId": version_id,
        "DeleteMarker": False,
    }


def _boto_client() -> Any:
    return boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://s3.example.test",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )


def test_storage_dtos_hide_bucket_key_version_and_url_from_repr() -> None:
    location = ObjectLocation(bucket=BUCKET, key=OBJECT_KEY, version_id=VERSION_ID)
    put = PutAuthorization(
        location=location,
        url=SIGNED_URL,
        required_headers={"Content-Type": CONTENT_TYPE},
        expires_in_seconds=900,
    )
    metadata = StoredObjectMetadata(
        location=location,
        checksum_sha256_b64=CHECKSUM_SHA256_B64,
        content_type=CONTENT_TYPE,
        size_bytes=4,
    )
    get = GetAuthorization(
        location=location,
        url=SIGNED_URL,
        expires_in_seconds=300,
    )

    rendered = " ".join(repr(item) for item in (location, put, metadata, get))
    for sensitive_value in (BUCKET, OBJECT_KEY, VERSION_ID, SIGNED_URL):
        assert sensitive_value not in rendered


def test_authorize_put_signs_only_required_headers_and_runs_off_event_loop() -> None:
    client = RecordingS3Client()
    store = S3ArtifactStore(client=client)
    caller_thread_id = threading.get_ident()

    authorization = asyncio.run(
        store.authorize_put(
            location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY),
            content_type=CONTENT_TYPE,
            checksum_sha256_b64=CHECKSUM_SHA256_B64,
        )
    )

    assert client.calls == [
        (
            "generate_presigned_url",
            {
                "ClientMethod": "put_object",
                "Params": {
                    "Bucket": BUCKET,
                    "Key": OBJECT_KEY,
                    "ContentType": CONTENT_TYPE,
                    "ChecksumSHA256": CHECKSUM_SHA256_B64,
                },
                "ExpiresIn": 900,
                "HttpMethod": "PUT",
            },
        )
    ]
    assert client.worker_thread_ids[0] != caller_thread_id
    assert authorization.location == ObjectLocation(bucket=BUCKET, key=OBJECT_KEY)
    assert authorization.url == SIGNED_URL
    assert authorization.expires_in_seconds == 900
    assert dict(authorization.required_headers) == {
        "Content-Type": CONTENT_TYPE,
        "x-amz-checksum-sha256": CHECKSUM_SHA256_B64,
    }


def test_multipart_lifecycle_is_strictly_scoped_and_runs_off_event_loop() -> None:
    client = RecordingS3Client()
    store = S3ArtifactStore(client=client)
    location = ObjectLocation(bucket=BUCKET, key=OBJECT_KEY)
    caller_thread_id = threading.get_ident()

    created = asyncio.run(store.create_multipart(location=location, content_type=CONTENT_TYPE))
    authorized = asyncio.run(
        store.authorize_part(
            location=location,
            storage_upload_id=created.storage_upload_id,
            part_number=2,
            expires_in_seconds=900,
        )
    )
    completed = asyncio.run(
        store.complete_multipart(
            location=location,
            storage_upload_id=created.storage_upload_id,
            parts=(MultipartPart(part_number=1, etag='"etag-1"'),),
        )
    )
    asyncio.run(
        store.abort_multipart(
            location=location,
            storage_upload_id=created.storage_upload_id,
        )
    )

    assert created.location == location
    assert authorized.part_number == 2
    assert authorized.expires_in_seconds == 900
    assert completed.location.version_id == VERSION_ID
    assert all(thread_id != caller_thread_id for thread_id in client.worker_thread_ids)
    assert client.calls == [
        (
            "create_multipart_upload",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "ContentType": CONTENT_TYPE,
                "ChecksumAlgorithm": "SHA256",
            },
        ),
        (
            "generate_presigned_url",
            {
                "ClientMethod": "upload_part",
                "Params": {
                    "Bucket": BUCKET,
                    "Key": OBJECT_KEY,
                    "UploadId": "private-storage-upload-id",
                    "PartNumber": 2,
                },
                "ExpiresIn": 900,
                "HttpMethod": "PUT",
            },
        ),
        (
            "complete_multipart_upload",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "UploadId": "private-storage-upload-id",
                "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": '"etag-1"'}]},
            },
        ),
        (
            "abort_multipart_upload",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "UploadId": "private-storage-upload-id",
            },
        ),
    ]


@pytest.mark.parametrize("expires_in_seconds", [0, -1, 901, True])
def test_authorize_put_rejects_ttl_outside_one_to_900_seconds(
    expires_in_seconds: int,
) -> None:
    client = RecordingS3Client()

    with pytest.raises(ArtifactAuthorizationError):
        asyncio.run(
            S3ArtifactStore(client=client).authorize_put(
                location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY),
                content_type=CONTENT_TYPE,
                checksum_sha256_b64=CHECKSUM_SHA256_B64,
                expires_in_seconds=expires_in_seconds,
            )
        )

    assert client.calls == []


def test_authorize_put_redacts_sdk_failure_and_discards_exception_chain() -> None:
    leaked = "sdk failure mentions secret-query-token"
    client = RecordingS3Client(failure=RuntimeError(leaked))

    with pytest.raises(ArtifactAuthorizationError) as captured:
        asyncio.run(
            S3ArtifactStore(client=client).authorize_put(
                location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY),
                content_type=CONTENT_TYPE,
                checksum_sha256_b64=CHECKSUM_SHA256_B64,
            )
        )

    assert leaked not in str(captured.value)
    assert leaked not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_head_requests_enabled_checksum_and_returns_versioned_metadata() -> None:
    client = _boto_client()
    store = S3ArtifactStore(client=client)
    with Stubber(client) as stubber:
        stubber.add_response(
            "head_object",
            _valid_head_response(),
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "ChecksumMode": "ENABLED",
            },
        )

        metadata = asyncio.run(store.head(location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY)))

    assert metadata == StoredObjectMetadata(
        location=ObjectLocation(
            bucket=BUCKET,
            key=OBJECT_KEY,
            version_id=VERSION_ID,
        ),
        checksum_sha256_b64=CHECKSUM_SHA256_B64,
        content_type=CONTENT_TYPE,
        size_bytes=4,
    )


def test_head_binds_requested_version_and_requires_matching_response_version() -> None:
    client = _boto_client()
    store = S3ArtifactStore(client=client)
    with Stubber(client) as stubber:
        stubber.add_response(
            "head_object",
            _valid_head_response(),
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "VersionId": VERSION_ID,
                "ChecksumMode": "ENABLED",
            },
        )

        metadata = asyncio.run(
            store.head(
                location=ObjectLocation(
                    bucket=BUCKET,
                    key=OBJECT_KEY,
                    version_id=VERSION_ID,
                )
            )
        )

    assert metadata.location.version_id == VERSION_ID


@pytest.mark.parametrize(
    ("response", "requested_version"),
    [
        ({**_valid_head_response(), "ChecksumSHA256": "not-a-sha256"}, None),
        ({**_valid_head_response(), "VersionId": ""}, None),
        ({**_valid_head_response(), "VersionId": "null"}, None),
        ({**_valid_head_response(), "ContentType": ""}, None),
        ({**_valid_head_response(), "ContentLength": -1}, None),
        ({**_valid_head_response(), "ContentLength": True}, None),
        ({**_valid_head_response(), "DeleteMarker": True}, None),
        (_valid_head_response(version_id="different-version"), VERSION_ID),
    ],
)
def test_head_rejects_invalid_or_ambiguous_object_metadata(
    response: Mapping[str, object],
    requested_version: str | None,
) -> None:
    client = RecordingS3Client(head_response=dict(response))

    with pytest.raises(ArtifactMetadataError) as captured:
        asyncio.run(
            S3ArtifactStore(client=client).head(
                location=ObjectLocation(
                    bucket=BUCKET,
                    key=OBJECT_KEY,
                    version_id=requested_version,
                )
            )
        )

    assert BUCKET not in str(captured.value)
    assert OBJECT_KEY not in str(captured.value)
    assert VERSION_ID not in str(captured.value)


@pytest.mark.parametrize(
    "code",
    ["404", "NoSuchBucket", "NoSuchKey", "NoSuchVersion", "NotFound"],
)
def test_head_maps_absent_objects_to_stable_not_found_error(code: str) -> None:
    client = _boto_client()
    store = S3ArtifactStore(client=client)
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "head_object",
            service_error_code=code,
            service_message="backend leaked secret marker",
            expected_params={
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "ChecksumMode": "ENABLED",
            },
        )

        with pytest.raises(ArtifactNotFoundError) as captured:
            asyncio.run(store.head(location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY)))

    assert str(captured.value) == "artifact object was not found"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_head_redacts_non_not_found_sdk_errors() -> None:
    leaked = "access denied for secret bucket"
    client = RecordingS3Client(failure=RuntimeError(leaked))

    with pytest.raises(ArtifactStorageError) as captured:
        asyncio.run(
            S3ArtifactStore(client=client).head(
                location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY)
            )
        )

    assert type(captured.value) is ArtifactStorageError
    assert str(captured.value) == "artifact storage operation failed"
    assert leaked not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_authorize_get_is_version_bound_and_forces_safe_download_headers() -> None:
    client = RecordingS3Client()
    store = S3ArtifactStore(client=client)
    location = ObjectLocation(
        bucket=BUCKET,
        key=OBJECT_KEY,
        version_id=VERSION_ID,
    )
    caller_thread_id = threading.get_ident()

    authorization = asyncio.run(store.authorize_get(location=location))

    assert client.calls == [
        (
            "generate_presigned_url",
            {
                "ClientMethod": "get_object",
                "Params": {
                    "Bucket": BUCKET,
                    "Key": OBJECT_KEY,
                    "VersionId": VERSION_ID,
                    "ResponseContentDisposition": "attachment",
                    "ResponseContentType": "application/octet-stream",
                    "ResponseCacheControl": "private, no-store",
                },
                "ExpiresIn": 300,
                "HttpMethod": "GET",
            },
        )
    ]
    assert client.worker_thread_ids[0] != caller_thread_id
    assert authorization == GetAuthorization(
        location=location,
        url=SIGNED_URL,
        expires_in_seconds=300,
    )


@pytest.mark.parametrize("expires_in_seconds", [0, -1, 301, True])
def test_authorize_get_requires_version_and_ttl_at_most_five_minutes(
    expires_in_seconds: int,
) -> None:
    client = RecordingS3Client()

    with pytest.raises(ArtifactAuthorizationError):
        asyncio.run(
            S3ArtifactStore(client=client).authorize_get(
                location=ObjectLocation(
                    bucket=BUCKET,
                    key=OBJECT_KEY,
                    version_id=VERSION_ID,
                ),
                expires_in_seconds=expires_in_seconds,
            )
        )
    with pytest.raises(ArtifactAuthorizationError):
        asyncio.run(
            S3ArtifactStore(client=client).authorize_get(
                location=ObjectLocation(bucket=BUCKET, key=OBJECT_KEY)
            )
        )
    with pytest.raises(ArtifactAuthorizationError):
        asyncio.run(
            S3ArtifactStore(client=client).authorize_get(
                location=ObjectLocation(
                    bucket=BUCKET,
                    key=OBJECT_KEY,
                    version_id="null",
                )
            )
        )

    assert client.calls == []
